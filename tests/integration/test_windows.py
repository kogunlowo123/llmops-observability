"""Reading and writing windows, against real files."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, timedelta
from pathlib import Path

import pytest

from llmops.errors import WindowError
from llmops.telemetry import window as window_module
from llmops.telemetry.window import (
    MAX_LINE_BYTES,
    build_window,
    is_compressed,
    read_window,
)
from tests.conftest import ORIGIN, WINDOWS, make_span, spread

pytestmark = pytest.mark.integration


class TestRoundTrip:
    def test_a_window_survives_being_written_and_read(self, tmp_path: Path):
        window = build_window(spread(50, over=timedelta(hours=1)))
        path = window.write(tmp_path / "w.jsonl")

        recovered = read_window(path)

        assert recovered.total == 50
        assert recovered.digest() == window.digest()
        assert recovered.start == window.start
        assert recovered.end == window.end

    def test_metadata_survives(self, tmp_path: Path):
        window = build_window(
            [make_span()],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
            metadata={"scenario": "test"},
        )
        window.write(tmp_path / "w.jsonl")
        assert read_window(tmp_path / "w.jsonl").metadata["scenario"] == "test"

    def test_the_file_is_json_lines(self, tmp_path: Path):
        # One header, then one span per line. Every line is a complete JSON
        # document, which is what makes a truncated file lose one span rather
        # than all of them.
        build_window(spread(3, over=timedelta(minutes=3))).write(tmp_path / "w.jsonl")
        lines = (tmp_path / "w.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 4
        assert json.loads(lines[0])["llmops_window"] == 1
        assert all(json.loads(line)["span_id"] for line in lines[1:])


class TestDamagedFiles:
    def _write(self, path: Path, *lines: str) -> Path:
        header = json.dumps(
            {
                "llmops_window": 1,
                "start": ORIGIN.isoformat(),
                "end": (ORIGIN + timedelta(hours=1)).isoformat(),
            }
        )
        path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
        return path

    def test_a_truncated_last_line_loses_one_span_and_says_so(self, tmp_path: Path):
        # A telemetry file is written by a process that can be killed mid-line.
        # Refusing the whole window throws away the other 99.99% of the
        # evidence; losing it silently makes every number quietly low.
        good = json.dumps(make_span(index=1).as_dict())
        path = self._write(tmp_path / "w.jsonl", good, good[: len(good) // 2])

        window = read_window(path)

        assert window.total == 1
        assert len(window.unreadable) == 1
        assert "not valid JSON" in window.unreadable[0][1]

    def test_an_invalid_span_is_counted_not_fatal(self, tmp_path: Path):
        path = self._write(
            tmp_path / "w.jsonl",
            json.dumps(make_span().as_dict()),
            json.dumps({"span_id": "bad", "started_at": "not a date"}),
        )
        window = read_window(path)
        assert window.total == 1
        assert len(window.unreadable) == 1

    def test_the_reason_names_the_field(self, tmp_path: Path):
        # A complete span apart from one bad field, so the reason names that
        # field rather than the first thing missing.
        payload = make_span().as_dict() | {"duration_ms": -5}
        path = self._write(tmp_path / "w.jsonl", json.dumps(payload))
        assert "duration_ms" in read_window(path).unreadable[0][1]

    def test_an_enormous_line_is_refused_without_being_parsed(self, tmp_path: Path):
        path = self._write(tmp_path / "w.jsonl", '{"x": "' + "y" * MAX_LINE_BYTES + '"}')
        assert "longer than" in read_window(path).unreadable[0][1]

    def test_a_missing_header_is_fatal(self, tmp_path: Path):
        # Without it there is no interval, and every report would be over an
        # interval the reader guessed.
        path = tmp_path / "w.jsonl"
        path.write_text(json.dumps({"span_id": "a"}) + "\n", encoding="utf-8")
        with pytest.raises(WindowError, match="no window header"):
            read_window(path)

    def test_a_non_json_first_line_is_fatal(self, tmp_path: Path):
        path = tmp_path / "w.jsonl"
        path.write_text("this is not a window\n", encoding="utf-8")
        with pytest.raises(WindowError, match="does not start with a window header"):
            read_window(path)

    def test_an_empty_file_is_fatal(self, tmp_path: Path):
        path = tmp_path / "w.jsonl"
        path.write_text("", encoding="utf-8")
        with pytest.raises(WindowError, match="is empty"):
            read_window(path)

    def test_a_newer_schema_is_refused_rather_than_misread(self, tmp_path: Path):
        path = tmp_path / "w.jsonl"
        path.write_text(
            json.dumps(
                {
                    "llmops_window": 99,
                    "start": ORIGIN.isoformat(),
                    "end": (ORIGIN + timedelta(hours=1)).isoformat(),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(WindowError, match="newer version"):
            read_window(path)

    def test_a_missing_file_names_the_command_that_makes_one(self, tmp_path: Path):
        with pytest.raises(WindowError) as caught:
            read_window(tmp_path / "absent.jsonl")
        assert "llmops synth" in caught.value.remedy


class TestIntervals:
    def test_the_interval_is_half_open(self, tmp_path: Path):
        # A span starting exactly at `end` belongs to the next window. Closed
        # intervals double-count a boundary span, which shows up as a spend
        # report that does not reconcile by one call.
        end = ORIGIN + timedelta(hours=1)
        window = build_window(
            [make_span(index=0, at=ORIGIN), make_span(index=1, at=end)],
            start=ORIGIN,
            end=end + timedelta(hours=1),
        )
        assert window.slice(ORIGIN, end).total == 1

    def test_a_window_that_ends_before_it_starts_is_refused(self):
        with pytest.raises(WindowError, match="ends at or before it starts"):
            build_window([make_span()], start=ORIGIN, end=ORIGIN - timedelta(hours=1))

    def test_an_inferred_end_includes_the_last_span(self):
        # Inferring the end as the last instant would exclude the very span
        # that defined it, because the interval is half-open.
        window = build_window([make_span(duration_ms=1000)])
        assert window.slice(window.start, window.end).total == 1

    def test_an_empty_window_cannot_infer_its_interval(self):
        with pytest.raises(WindowError, match="cannot infer"):
            build_window([])

    def test_an_empty_window_is_fine_with_an_explicit_interval(self):
        window = build_window([], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        assert window.total == 0
        assert window.succeeded == 0

    def test_ending_at_takes_the_last_stretch(self):
        window = build_window(spread(60, over=timedelta(hours=1), ending_at=ORIGIN))
        assert window.ending_at(ORIGIN, timedelta(minutes=10)).total == 10

    def test_spans_are_ordered_however_they_arrive(self):
        late = make_span(index=1, at=ORIGIN + timedelta(minutes=5))
        early = make_span(index=0, at=ORIGIN)
        window = build_window([late, early])
        assert [span.span_id for span in window] == ["s000000", "s000001"]


class TestDigests:
    def test_the_digest_ignores_line_order(self, tmp_path: Path):
        spans = spread(10, over=timedelta(minutes=10))
        assert build_window(spans).digest() == build_window(list(reversed(spans))).digest()

    def test_the_digest_ignores_the_metadata(self):
        one = build_window([make_span()], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        two = build_window(
            [make_span()],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
            metadata={"note": "hello"},
        )
        assert one.digest() == two.digest()

    def test_the_digest_changes_when_a_span_does(self):
        one = build_window([make_span(output_tokens=10)])
        two = build_window([make_span(output_tokens=11)])
        assert one.digest() != two.digest()


class TestCounts:
    def test_a_refusal_counts_as_a_success(self):
        window = build_window(
            [make_span(index=0, outcome="refusal"), make_span(index=1, at=ORIGIN)],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        assert window.succeeded == 2
        assert window.failed == 0

    def test_models_are_listed_once_and_sorted(self):
        window = build_window(
            [
                make_span(index=0, model="b"),
                make_span(index=1, model="a"),
                make_span(index=2, model="a"),
            ],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        assert window.models() == (("openai", "a"), ("openai", "b"))


class TestTheShippedCorpus:
    def test_every_shipped_window_reads_cleanly(self, example_window_path: Path):
        window = read_window(example_window_path)
        assert window.total > 1000
        # Not one unreadable line. A committed fixture with a damaged line
        # would make every number in the README quietly wrong.
        assert window.unreadable == ()

    def test_it_says_it_was_generated(self, example_window_path: Path):
        # The corpus is synthesised, and the file says so rather than leaving
        # a reader to assume it was captured from a real service.
        metadata = read_window(example_window_path).metadata
        assert metadata["generated"] == "true"
        assert "Synthesised" in metadata["note"]

    def test_it_covers_three_days(self, example_window_path: Path):
        assert read_window(example_window_path).duration == timedelta(days=3)

    def test_its_timestamps_are_all_aware(self, example_window_path: Path):
        window = read_window(example_window_path)
        assert all(span.started_at.tzinfo is not None for span in window)
        assert window.start.tzinfo is UTC or window.start.utcoffset() == timedelta(0)

    def test_it_contains_no_span_whose_timestamps_fall_outside_the_interval(
        self, example_window_path: Path
    ):
        window = read_window(example_window_path)
        assert all(window.start <= span.started_at < window.end for span in window)


class TestCompression:
    """Windows ending ``.gz`` are gzipped, and the corpus in this repository is.

    Telemetry compresses by roughly a factor of eleven — every line repeats the
    same dozen keys and the same handful of model names — so the alternative was
    seven megabytes of fixtures in a repository whose point is that they can be
    regenerated in two seconds.
    """

    def test_a_gzipped_window_survives_the_round_trip(self, tmp_path: Path):
        window = build_window(spread(50, over=timedelta(hours=1)))
        path = window.write(tmp_path / "w.jsonl.gz")

        recovered = read_window(path)

        assert recovered.total == 50
        assert recovered.digest() == window.digest()

    def test_the_file_really_is_gzip(self, tmp_path: Path):
        build_window(spread(5, over=timedelta(minutes=5))).write(tmp_path / "w.jsonl.gz")
        assert (tmp_path / "w.jsonl.gz").read_bytes()[:2] == b"\x1f\x8b"

    def test_compression_does_not_change_the_content_address(self, tmp_path: Path):
        # The digest is over the spans, so where and how a window is stored is
        # not something a drift check should ever notice.
        window = build_window(spread(20, over=timedelta(hours=1)))
        window.write(tmp_path / "plain.jsonl")
        window.write(tmp_path / "packed.jsonl.gz")

        assert read_window(tmp_path / "plain.jsonl").digest() == (
            read_window(tmp_path / "packed.jsonl.gz").digest()
        )

    def test_writing_the_same_window_twice_gives_the_same_bytes(self, tmp_path: Path):
        # gzip stamps the current time into its header by default, which would
        # make every regenerated corpus differ from the committed one for a
        # reason no reader could see, and turn the drift check into noise.
        window = build_window(spread(20, over=timedelta(hours=1)))
        first = window.write(tmp_path / "a.jsonl.gz").read_bytes()
        second = window.write(tmp_path / "b.jsonl.gz").read_bytes()
        assert first == second

    def test_it_is_the_suffix_that_decides_and_not_the_content(self, tmp_path: Path):
        # A tool that sniffs the magic bytes accepts gzip named .jsonl, which
        # teaches people to ship one, and then something downstream that reads
        # the file by name gets bytes it cannot parse.
        assert is_compressed("w.jsonl.gz")
        assert not is_compressed("w.jsonl")
        assert not is_compressed("w.gz.jsonl")

    def test_a_decompression_bomb_is_refused_rather_than_swallowing_the_runner(
        self, tmp_path: Path
    ):
        # 5 MB of zeroes compresses to a few kilobytes. With the limit lowered
        # to something a test can reach, this is the same shape as the file that
        # would otherwise expand until the CI runner is killed — a failure that
        # reaches an operator as "the gate is flaky".
        path = tmp_path / "bomb.jsonl.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(b"0" * (5 * 1024 * 1024))

        with pytest.MonkeyPatch.context() as patch:
            # Above the compressed size on disk and far below the expanded
            # size, so it is the stream guard that fires and not the cheap
            # stat() check the plain path already had.
            patch.setattr(window_module, "MAX_WINDOW_BYTES", 64 * 1024)
            with pytest.raises(WindowError, match="expands to more than"):
                read_window(path)

    def test_the_committed_corpus_is_compressed(self):
        # Guards the decision itself: someone regenerating the corpus to
        # ``.jsonl`` would silently put seven megabytes back into the tree.
        assert sorted(path.name for path in WINDOWS.iterdir()) == [
            "cost-spike.jsonl.gz",
            "outage.jsonl.gz",
            "slowdown.jsonl.gz",
            "steady.jsonl.gz",
        ]
