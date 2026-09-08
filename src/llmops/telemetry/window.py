"""A window: the spans in a half-open interval, and the file they live in.

Everything this tool decides is decided over a window. It is the analogue of the
baseline in the evaluation harness elsewhere in this series — the artefact that
makes a verdict reproducible — and it has the same requirement: a report
computed from it today must be computable from it in a year, on another machine,
with no service running.

The format is **JSON Lines**: one header object, then one span per line. Chosen
over a single JSON document because a telemetry file is appended to by a process
that may be killed, and a truncated JSON array is unreadable in its entirety
while a truncated JSONL file loses exactly the last line. The reader says how
many lines it could not parse rather than silently returning fewer spans.

The interval is **half-open**, ``[start, end)``. A span starting exactly at
``end`` belongs to the next window. Closed intervals double-count a boundary
span, which shows up as a spend report that does not reconcile by one call and
takes an afternoon to find.

A path ending ``.gz`` is read and written **gzipped**, decided by the suffix and
not by a flag. Telemetry is the most compressible data a platform produces —
every line repeats the same dozen keys and the same handful of model names — and
these windows shrink by a factor of eleven. The example corpus in this
repository is committed in that form for exactly that reason. Nothing else in
the tool knows or cares: the digest is over the spans, so a window keeps its
content address whether it is stored compressed or not.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

from pydantic import ValidationError

from llmops.errors import WindowError
from llmops.telemetry.span import SPAN_SCHEMA_VERSION, Span

#: Windows are committed to repositories as fixtures and read into memory whole.
#:
#: Applied to the *decompressed* stream as well as to the file on disk. Checking
#: only the file would make a 200 KB gzip that expands to forty gigabytes a
#: perfectly acceptable input, which is the whole of the decompression-bomb
#: attack and costs one line to refuse.
MAX_WINDOW_BYTES = 256 * 1024 * 1024
#: A line longer than this is not a span; it is a file that is not a window.
MAX_LINE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class Window:
    """Spans over a half-open interval, with what could not be read."""

    start: datetime
    end: datetime
    spans: tuple[Span, ...]
    #: Lines the reader rejected, as (line number, reason). Reported, never
    #: silently dropped: a window that quietly lost 3% of its spans produces a
    #: spend report that is quietly 3% low.
    unreadable: tuple[tuple[int, str], ...] = ()
    source: str = "<memory>"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise WindowError(
                f"the window ends at or before it starts ({self.start} .. {self.end}).",
                remedy="A window is a half-open interval [start, end) and must be non-empty.",
            )

    def __len__(self) -> int:
        return len(self.spans)

    def __iter__(self) -> Iterator[Span]:
        return iter(self.spans)

    @property
    def duration(self) -> timedelta:
        """How long the window covers."""
        return self.end - self.start

    @property
    def total(self) -> int:
        """How many spans are in it."""
        return len(self.spans)

    @property
    def succeeded(self) -> int:
        """How many spans count as successful. See ``Span.succeeded``."""
        return sum(1 for span in self.spans if span.succeeded)

    @property
    def failed(self) -> int:
        """How many did not."""
        return self.total - self.succeeded

    def slice(self, start: datetime, end: datetime) -> Window:
        """Return the sub-window ``[start, end)``.

        The whole burn-rate calculation is built on this: a long window and a
        short one, evaluated over the same spans, with no re-reading.
        """
        kept = tuple(span for span in self.spans if start <= span.started_at < end)
        return Window(
            start=start,
            end=end,
            spans=kept,
            unreadable=self.unreadable,
            source=self.source,
            metadata=dict(self.metadata),
        )

    def ending_at(self, end: datetime, length: timedelta) -> Window:
        """Return the sub-window of *length* immediately before *end*."""
        return self.slice(end - length, end)

    def models(self) -> tuple[tuple[str, str], ...]:
        """Every (provider, model) pair present, sorted."""
        return tuple(sorted({(span.provider, span.model) for span in self.spans}))

    def digest(self) -> str:
        """Return a content address over what the spans bill and measure.

        Deliberately not a hash of the file: reordering the lines, reformatting
        the JSON or adding a comment to the header does not change what any
        report computes, and a drift check that fires on those is a check people
        learn to ignore.
        """
        material = "\n".join(sorted(span.fingerprint() for span in self.spans))
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def as_header(self) -> dict[str, Any]:
        """Return the first line of a window file."""
        return {
            "llmops_window": SPAN_SCHEMA_VERSION,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "spans": self.total,
            "digest": self.digest(),
            "metadata": dict(self.metadata),
        }

    def write(self, path: str | Path) -> Path:
        """Write this window as JSON Lines, gzipped when the path ends ``.gz``."""
        file = Path(path)
        payload = [self.as_header(), *(span.as_dict() for span in self.spans)]
        return write_window_lines(file, payload)


def build_window(  # noqa: PLR0913 - a window is an interval, its spans and its provenance
    spans: Iterable[Span],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    source: str = "<memory>",
    metadata: dict[str, Any] | None = None,
    unreadable: Sequence[tuple[int, str]] = (),
) -> Window:
    """Assemble a window, inferring the interval from the spans when not given.

    The inferred end is the last span's *end*, nudged forward by a microsecond,
    because the interval is half-open: inferring it as the last instant would
    exclude the very span that defined it.
    """
    ordered = tuple(sorted(spans, key=lambda span: (span.started_at, span.span_id)))
    if start is None or end is None:
        if not ordered:
            raise WindowError(
                "cannot infer an interval from no spans.",
                remedy="Pass start and end explicitly for an empty window.",
            )
        start = start if start is not None else ordered[0].started_at
        end = (
            end
            if end is not None
            else max(span.ended_at for span in ordered) + timedelta(microseconds=1)
        )
    return Window(
        start=start,
        end=end,
        spans=ordered,
        unreadable=tuple(unreadable),
        source=source,
        metadata=dict(metadata or {}),
    )


def write_window_lines(path: str | Path, lines: Sequence[dict[str, Any]]) -> Path:
    """Write a header line followed by one span per line, and return the path.

    Gzipped when the path ends ``.gz``. The gzip member carries a zero timestamp
    and no filename so that writing the same window twice produces the same
    bytes; the default header stamps the current time, which would make every
    regenerated corpus differ from the committed one for no reason a reader
    could see, and turn the drift check into noise.

    Both writers in this package come through here — ``Window.write`` and the
    JSONL exporter, which assembles its own lines because it redacts the whole
    document once before serialising it. Two writers meant two chances to
    disagree about the file format; there is now one.
    """
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in lines) + "\n"
    if is_compressed(file):
        with (
            file.open("wb") as sink,
            gzip.GzipFile(filename="", mode="wb", fileobj=sink, mtime=0) as raw,
        ):
            raw.write(body.encode("utf-8"))
    else:
        file.write_text(body, encoding="utf-8", newline="\n")
    return file


def is_compressed(path: str | Path) -> bool:
    """Is this path a gzipped window? Decided by the suffix, never by sniffing.

    Sniffing the magic bytes would be friendlier to a mislabelled file and worse
    for everything else: the name a window is written under is the name it is
    read back under, and a tool that quietly accepts a ``window.jsonl`` full of
    gzip teaches people to ship one.
    """
    return Path(path).suffix == ".gz"


class _Bounded(io.RawIOBase):
    """A read-only stream that refuses to yield more than *limit* bytes."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._seen = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._stream.read(len(buffer))
        self._seen += len(chunk)
        if self._seen > self._limit:
            raise WindowError(
                f"the window expands to more than {self._limit} bytes.",
                remedy=(
                    "A small file that decompresses without bound is a decompression "
                    "bomb, not a window. Split the window by time, or raise "
                    "MAX_WINDOW_BYTES deliberately."
                ),
            )
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        self._stream.close()
        super().close()


def open_window_text(path: str | Path) -> IO[str]:
    """Open a window for reading as text, transparently decompressing.

    The stream refuses to yield more than ``MAX_WINDOW_BYTES``. For a plain file
    that is redundant with the size check in ``read_window``; for a gzipped one
    it is the only thing between this tool and a 200 KB file that expands until
    the runner is killed, which reaches an operator as "the gate is flaky".
    """
    file = Path(path)
    if not is_compressed(file):
        return file.open(encoding="utf-8")
    bounded = _Bounded(gzip.GzipFile(file, mode="rb"), MAX_WINDOW_BYTES)
    return io.TextIOWrapper(io.BufferedReader(bounded), encoding="utf-8")


def read_window(path: str | Path) -> Window:
    """Read a window file.

    A malformed line is counted and reported, not fatal — a telemetry file is
    written by a process that can be killed mid-line, and refusing the whole
    window because of one truncated record throws away the other 99.99% of the
    evidence. A malformed *header* is fatal, because without it there is no
    interval and every report would be over an interval this function guessed.
    """
    file = Path(path)
    if not file.is_file():
        raise WindowError(
            f"the window {str(file)!r} could not be opened.",
            remedy="Check the path, or record one with 'llmops synth --out <path>'.",
        )
    size = file.stat().st_size
    if size > MAX_WINDOW_BYTES:
        raise WindowError(
            f"the window is {size} bytes; the limit is {MAX_WINDOW_BYTES}.",
            remedy="Split it by time and gate each part, or raise MAX_WINDOW_BYTES deliberately.",
        )

    spans: list[Span] = []
    unreadable: list[tuple[int, str]] = []
    header: dict[str, Any] | None = None

    with open_window_text(file) as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                unreadable.append((number, f"the line is longer than {MAX_LINE_BYTES} bytes"))
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if header is None:
                    raise WindowError(
                        f"{file.name} does not start with a window header: {exc}.",
                        remedy=(
                            "The first line must be the header object written by 'Window.write'."
                        ),
                    ) from exc
                unreadable.append((number, f"not valid JSON: {exc.msg}"))
                continue
            if not isinstance(payload, dict):
                unreadable.append((number, "not a JSON object"))
                continue
            if header is None:
                header = _validated_header(payload, name=file.name)
                continue
            try:
                spans.append(Span.model_validate(payload))
            except ValidationError as exc:
                unreadable.append((number, _first_problem(exc)))

    if header is None:
        raise WindowError(
            f"{file.name} is empty.",
            remedy="A window file has a header line even when it has no spans.",
        )

    return build_window(
        spans,
        start=datetime.fromisoformat(header["start"]).astimezone(UTC),
        end=datetime.fromisoformat(header["end"]).astimezone(UTC),
        source=str(file),
        metadata=dict(header.get("metadata") or {}),
        unreadable=unreadable,
    )


def _validated_header(payload: dict[str, Any], *, name: str) -> dict[str, Any]:
    version = payload.get("llmops_window")
    if version is None:
        raise WindowError(
            f"{name} has no window header.",
            remedy="The first line must carry 'llmops_window', 'start' and 'end'.",
        )
    if not isinstance(version, int) or version > SPAN_SCHEMA_VERSION:
        raise WindowError(
            f"{name} was written by a newer version (schema {version}).",
            remedy=f"This build understands schema {SPAN_SCHEMA_VERSION}. Upgrade llmops.",
        )
    for key in ("start", "end"):
        value = payload.get(key)
        if not isinstance(value, str):
            raise WindowError(
                f"{name} has no {key!r} in its header.",
                remedy="Without an interval every report would be over one this tool guessed.",
            )
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise WindowError(
                f"{name} has an unparseable {key!r}: {value!r}.",
                remedy="Use an ISO 8601 timestamp with an offset.",
            ) from exc
    return payload


def _first_problem(exc: ValidationError) -> str:
    """One line naming the first thing wrong, for the unreadable list."""
    errors = exc.errors()
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "invalid span"
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "span"
    return f"{where}: {first['msg']}"
