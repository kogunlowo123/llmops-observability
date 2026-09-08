"""The command line, driven in process.

This layer exists alongside ``tests/e2e/test_cli.py`` rather than instead of it,
because the two answer different questions. The end-to-end tests spawn a real
process and are the only thing that can show ``echo $?`` is 2 or that a cp1252
console does not crash the tool. They cannot show which branch ran, and nothing
they do is visible to a coverage run of the parent process — a subprocess is a
different interpreter.

So the dispatch itself is tested here: every command, both sides of every
decision the command line makes on its own, and the mapping from a raised error
to an exit code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from xml.etree import ElementTree

import pytest

from llmops.cli import main
from llmops.errors import EXIT_BUDGET_BURNED, EXIT_ERROR, EXIT_OK
from tests.conftest import EXAMPLES, WINDOWS

pytestmark = pytest.mark.integration

STEADY = ("--window", str(WINDOWS / "steady.jsonl.gz"))
OUTAGE = ("--window", str(WINDOWS / "outage.jsonl.gz"))
OBJECTIVES = ("--objectives", str(EXAMPLES / "objectives.yaml"))
PRICES = ("--pricebook", str(EXAMPLES / "prices.yaml"))
DURING_OUTAGE = "2026-09-01T22:40:00+00:00"


@pytest.fixture(autouse=True)
def _neutral_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test as if on a machine with no settings of its own.

    A developer with LLMOPS_* set would otherwise see different results from
    CI, which is the failure mode the settings module exists to avoid.
    """
    for name in tuple(os.environ):
        if name.startswith("LLMOPS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


class TestGate:
    def test_the_healthy_window_is_green(self, capsys: pytest.CaptureFixture[str]):
        assert main(["gate", *STEADY, *OBJECTIVES, *PRICES]) == EXIT_OK
        captured = capsys.readouterr()
        # The split is load-bearing: a build script parses stdout.
        assert json.loads(captured.out)["passed"] is True
        assert "HELD" in captured.err

    def test_an_incident_exits_two_and_names_the_objective(
        self, capsys: pytest.CaptureFixture[str]
    ):
        code = main(["gate", *OUTAGE, *OBJECTIVES, *PRICES, "--at", DURING_OUTAGE])
        assert code == EXIT_BUDGET_BURNED
        err = capsys.readouterr().err
        assert "chat-availability" in err
        assert "PAGE" in err

    def test_the_control_is_green_at_the_same_instant(self):
        assert main(["gate", *STEADY, *OBJECTIVES, *PRICES, "--at", DURING_OUTAGE]) == EXIT_OK

    def test_a_spend_objective_needs_a_price_book(self, capsys: pytest.CaptureFixture[str]):
        assert main(["gate", *STEADY, *OBJECTIVES]) == EXIT_ERROR
        assert "--pricebook" in capsys.readouterr().err

    def test_with_cost_needs_one_too(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        objectives = tmp_path / "o.yaml"
        objectives.write_text(
            "objectives: [{name: a, kind: availability, target: 0.99}]", encoding="utf-8"
        )
        code = main(["gate", *STEADY, "--objectives", str(objectives), "--with-cost"])
        assert code == EXIT_ERROR
        assert "--with-cost needs a price book" in capsys.readouterr().err

    def test_every_report_format_is_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            [
                "gate",
                *OUTAGE,
                *OBJECTIVES,
                *PRICES,
                "--at",
                DURING_OUTAGE,
                "--json-out",
                str(tmp_path / "r" / "r.json"),
                "--junit-out",
                str(tmp_path / "r" / "r.xml"),
                "--markdown-out",
                str(tmp_path / "r" / "r.md"),
            ]
        )
        assert code == EXIT_BUDGET_BURNED
        # A directory that did not exist is created, so a CI job need not mkdir.
        assert json.loads((tmp_path / "r" / "r.json").read_text("utf-8"))["passed"] is False
        assert ElementTree.fromstring((tmp_path / "r" / "r.xml").read_text("utf-8")).tag == (
            "testsuite"
        )
        assert (tmp_path / "r" / "r.md").read_text("utf-8").startswith("# Error budget")
        # With --json-out the report goes to the file and not also to stdout.
        assert capsys.readouterr().out == ""

    def test_a_naive_instant_is_refused(self, capsys: pytest.CaptureFixture[str]):
        code = main(["gate", *STEADY, *OBJECTIVES, *PRICES, "--at", "2026-09-01T22:40:00"])
        assert code == EXIT_ERROR
        assert "no timezone" in capsys.readouterr().err

    def test_an_unparseable_instant_is_refused(self, capsys: pytest.CaptureFixture[str]):
        code = main(["gate", *STEADY, *OBJECTIVES, *PRICES, "--at", "yesterday"])
        assert code == EXIT_ERROR
        assert "ISO 8601" in capsys.readouterr().err

    def test_a_missing_window_exits_three(self, capsys: pytest.CaptureFixture[str]):
        # Nothing was measured, so nothing was overspent.
        code = main(["gate", "--window", "absent.jsonl", *OBJECTIVES])
        assert code == EXIT_ERROR
        assert code != EXIT_BUDGET_BURNED
        assert "could not be opened" in capsys.readouterr().err

    def test_a_rule_that_reached_no_verdict_is_noted_on_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ):
        # "We did not look" is not "we looked and it was fine", and the reader
        # is told rather than left to infer it from a green line.
        main(["gate", *STEADY, *OBJECTIVES, *PRICES, "--at", DURING_OUTAGE])
        assert "not covered" in capsys.readouterr().err


class TestCost:
    def test_it_reports_a_total(self, capsys: pytest.CaptureFixture[str]):
        assert main(["cost", *STEADY, *PRICES]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["total_usd"] > 0

    @pytest.mark.parametrize("dimension", ["model", "provider", "operation", "outcome"])
    def test_each_grouping_works(self, dimension: str, capsys: pytest.CaptureFixture[str]):
        assert main(["cost", *STEADY, *PRICES, "--by", dimension]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["dimension"] == dimension

    def test_a_ceiling_can_fail_the_build(self, capsys: pytest.CaptureFixture[str]):
        assert main(["cost", *STEADY, *PRICES, "--fail-over", "0.01"]) == EXIT_BUDGET_BURNED
        assert "over the" in capsys.readouterr().err

    def test_a_generous_ceiling_passes(self):
        assert main(["cost", *STEADY, *PRICES, "--fail-over", "1e9"]) == EXIT_OK

    def test_the_report_can_be_written_to_a_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        out = tmp_path / "cost.json"
        assert main(["cost", *STEADY, *PRICES, "--json-out", str(out)]) == EXIT_OK
        assert json.loads(out.read_text("utf-8"))["calls"] > 0
        assert capsys.readouterr().out == ""

    def test_an_unpriced_model_names_them_all_at_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        prices = tmp_path / "p.yaml"
        prices.write_text(
            "prices: [{provider: nobody, model: nothing,"
            " input_per_million: 1, output_per_million: 1}]",
            encoding="utf-8",
        )
        code = main(["cost", *STEADY, "--pricebook", str(prices)])
        assert code == EXIT_ERROR
        assert capsys.readouterr().err.count("openai/") >= 2

    def test_a_discrepancy_is_counted_on_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # A window with a provider-reported cost that disagrees.
        from datetime import timedelta

        from llmops.telemetry.window import build_window
        from tests.conftest import ORIGIN, make_span

        window = build_window(
            [
                make_span(
                    index=index,
                    at=ORIGIN,
                    input_tokens=1_000_000,
                    output_tokens=0,
                    reported_cost_usd=99.0,
                )
                for index in range(3)
            ],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        path = window.write(tmp_path / "w.jsonl")
        prices = tmp_path / "p.yaml"
        prices.write_text(
            "prices: [{provider: openai, model: gpt-4o-mini,"
            " input_per_million: 1, output_per_million: 1}]",
            encoding="utf-8",
        )

        assert main(["cost", "--window", str(path), "--pricebook", str(prices)]) == EXIT_OK
        assert "disagree with the provider" in capsys.readouterr().err


class TestSynthAndCheck:
    def test_synth_writes_a_window(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        out = tmp_path / "w.jsonl"
        assert main(["synth", "--scenario", "steady", "--out", str(out)]) == EXIT_OK
        assert json.loads(capsys.readouterr().out)["spans"] > 1000
        assert out.is_file()

    def test_a_seed_override_changes_the_window(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        main(["synth", "--scenario", "steady", "--out", str(tmp_path / "a.jsonl")])
        one = json.loads(capsys.readouterr().out)["digest"]
        main(["synth", "--scenario", "steady", "--seed", "7", "--out", str(tmp_path / "b.jsonl")])
        assert json.loads(capsys.readouterr().out)["digest"] != one

    def test_an_otlp_export_is_refused_offline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            [
                "synth",
                "--scenario",
                "steady",
                "--out",
                str(tmp_path / "w.jsonl"),
                "--export",
                "otlp",
                "--endpoint",
                "http://localhost:4318/v1/traces",
            ]
        )
        assert code == EXIT_ERROR
        assert "--allow-network" in capsys.readouterr().err

    @pytest.mark.parametrize("scenario", ["steady", "outage", "slowdown", "cost-spike"])
    def test_every_committed_window_matches_its_scenario(
        self, scenario: str, capsys: pytest.CaptureFixture[str]
    ):
        code = main(
            ["check", "--scenario", scenario, "--window", str(WINDOWS / f"{scenario}.jsonl.gz")]
        )
        assert code == EXIT_OK
        assert "matches scenario" in capsys.readouterr().out

    def test_a_drifted_window_is_reported(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        # The negative control. Without it the check above is a `true`.
        main(
            ["synth", "--scenario", "steady", "--seed", "4242", "--out", str(tmp_path / "w.jsonl")]
        )
        capsys.readouterr()
        code = main(["check", "--scenario", "steady", "--window", str(tmp_path / "w.jsonl")])
        assert code == EXIT_ERROR
        err = capsys.readouterr().err
        assert "no longer matches" in err
        assert "llmops synth --scenario steady" in err


class TestLabels:
    def test_bounded_labels_are_accepted(self, capsys: pytest.CaptureFixture[str]):
        assert main(["labels", "--metric", "m", "--label", "provider"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["accepted"] is True
        assert payload["max_series"] == 20

    def test_an_unbounded_label_is_refused(self, capsys: pytest.CaptureFixture[str]):
        assert main(["labels", "--metric", "m", "--label", "tenant_id"]) == EXIT_ERROR
        assert "tenant_tier" in capsys.readouterr().err

    def test_a_label_can_be_declared(self, capsys: pytest.CaptureFixture[str]):
        code = main(["labels", "--metric", "m", "--label", "tier", "--bound", "tier=6"])
        assert code == EXIT_OK
        assert json.loads(capsys.readouterr().out)["max_series"] == 6

    @pytest.mark.parametrize("declaration", ["tier", "tier=", "=6", "tier=many"])
    def test_a_malformed_bound_is_refused(
        self, declaration: str, capsys: pytest.CaptureFixture[str]
    ):
        code = main(["labels", "--metric", "m", "--label", "x", "--bound", declaration])
        assert code == EXIT_ERROR
        assert "NAME=COUNT" in capsys.readouterr().err

    def test_the_budget_can_be_overridden(self, capsys: pytest.CaptureFixture[str]):
        code = main(
            [
                "labels",
                "--metric",
                "m",
                "--label",
                "model",
                "--label",
                "route",
                "--series-budget",
                "100000",
            ]
        )
        assert code == EXIT_OK
        assert json.loads(capsys.readouterr().out)["series_budget"] == 100000


class TestDoctor:
    def test_it_self_checks_without_any_telemetry(self, capsys: pytest.CaptureFixture[str]):
        assert main(["doctor"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "self-check      ok" in out
        assert "scenarios" in out

    def test_it_can_inspect_a_window(self, capsys: pytest.CaptureFixture[str]):
        assert main(["doctor", *STEADY]) == EXIT_OK
        out = capsys.readouterr().out
        assert "digest          sha256:" in out
        assert "models          " in out


class TestGlobalOptions:
    def test_the_log_level_can_be_overridden_for_one_invocation(self):
        assert main(["--log-level", "DEBUG", "doctor"]) == EXIT_OK

    def test_settings_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLMOPS_GATE__SERIES_BUDGET", "3")
        assert main(["labels", "--metric", "m", "--label", "operation"]) == EXIT_ERROR

    def test_a_bad_setting_is_rendered_rather_than_raised(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        # Reading the settings happens inside main's try block, so a failure
        # there is reported like every other one.
        monkeypatch.setenv("LLMOPS_LOGS__LEVEL", "DEBUG")
        assert main(["doctor"]) == EXIT_ERROR
        assert "LLMOPS_LOGS__LEVEL" in capsys.readouterr().err
