"""The command line, driven as a real process.

These run ``llmops`` through ``python -m`` so that argument parsing, exit codes,
the stdout/stderr split and the shipped corpus are all exercised together. A
library test cannot show that ``echo $?`` is 2, and it cannot show that a
Windows console does not crash the tool — the previous repository in this series
shipped a ``UnicodeEncodeError`` that only this layer could have caught.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

from llmops.errors import EXIT_BUDGET_BURNED, EXIT_ERROR, EXIT_OK
from tests.conftest import EXAMPLES, WINDOWS

pytestmark = pytest.mark.e2e


def llmops(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "llmops", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=None if env is None else {**os.environ, **env},
        check=False,
        timeout=300,
    )


STEADY = ("--window", str(WINDOWS / "steady.jsonl.gz"))
OUTAGE = ("--window", str(WINDOWS / "outage.jsonl.gz"))
OBJECTIVES = ("--objectives", str(EXAMPLES / "objectives.yaml"))
PRICES = ("--pricebook", str(EXAMPLES / "prices.yaml"))

#: The instant the outage scenario's incident was live. Each incident is over by
#: the end of its window, which is correct and is the short window's whole
#: purpose, so a gate at the window's end sees a platform that has recovered.
DURING_OUTAGE = "2026-09-01T22:40:00+00:00"


class TestBasics:
    def test_version_is_reported(self):
        result = llmops("--version")
        assert result.returncode == EXIT_OK
        assert result.stdout.startswith("llmops ")

    def test_no_subcommand_is_a_usage_error(self):
        assert llmops().returncode != EXIT_OK

    def test_doctor_self_checks_without_any_telemetry(self):
        result = llmops("doctor")
        assert result.returncode == EXIT_OK
        assert "self-check      ok" in result.stdout

    def test_doctor_can_inspect_a_window(self):
        result = llmops("doctor", *STEADY)
        assert result.returncode == EXIT_OK
        assert "digest          sha256:" in result.stdout
        assert "unreadable      0 line(s)" in result.stdout


class TestBadConfiguration:
    """A bad environment variable, seen from where a CI job sees it.

    The unit tests call `load()` directly, so they pass whether or not the
    command line ever renders what it raises. This is the layer that can tell.
    """

    @pytest.mark.parametrize(
        "variable",
        [
            # A misspelt section: pydantic never builds the key, so
            # extra="forbid" has nothing to reject.
            "LLMOPS_LOGS__LEVEL",
            # A misspelt field: pydantic raises, but ValidationError is not one
            # of this tool's exceptions.
            "LLMOPS_GATE__MIN_SAMPLE",
        ],
    )
    def test_a_misspelt_variable_is_reported_rather_than_dumped(self, variable: str):
        result = llmops("doctor", env={variable: "5"})

        assert result.returncode == EXIT_ERROR
        assert "Traceback" not in result.stderr
        assert "LLMOPS_<SECTION>__<FIELD>" in result.stderr


class TestGate:
    def test_the_healthy_window_is_green(self):
        result = llmops("gate", *STEADY, *OBJECTIVES, *PRICES)
        assert result.returncode == EXIT_OK, result.stderr
        assert json.loads(result.stdout)["passed"] is True

    def test_an_incident_exits_two(self):
        # The whole point of the project, asserted at the process boundary.
        result = llmops("gate", *OUTAGE, *OBJECTIVES, *PRICES, "--at", DURING_OUTAGE)
        assert result.returncode == EXIT_BUDGET_BURNED
        assert "chat-availability" in result.stderr

    def test_the_control_is_green_at_the_same_instant(self):
        # Without this, the finding above could be a fact about 22:40 rather
        # than about the incident.
        result = llmops("gate", *STEADY, *OBJECTIVES, *PRICES, "--at", DURING_OUTAGE)
        assert result.returncode == EXIT_OK, result.stderr

    def test_the_report_goes_to_stdout_and_logs_to_stderr(self):
        # A log line in the middle of the report is an unparseable report.
        result = llmops("gate", *STEADY, *OBJECTIVES, *PRICES)
        json.loads(result.stdout)
        assert "HELD" in result.stderr

    def test_a_missing_window_exits_three_not_two(self):
        # Nothing was measured, so nothing was overspent.
        result = llmops("gate", "--window", "absent.jsonl", *OBJECTIVES)
        assert result.returncode == EXIT_ERROR
        assert "could not be opened" in result.stderr

    def test_the_error_carries_its_remedy(self):
        result = llmops("gate", "--window", "absent.jsonl", *OBJECTIVES)
        assert "llmops synth" in result.stderr

    def test_a_spend_objective_without_a_price_book_exits_three(self):
        # Not a silent pass: an objective that could not be checked has not
        # been checked.
        result = llmops("gate", *STEADY, *OBJECTIVES)
        assert result.returncode == EXIT_ERROR
        assert "--pricebook" in result.stderr

    def test_a_naive_instant_is_refused(self):
        result = llmops("gate", *STEADY, *OBJECTIVES, *PRICES, "--at", "2026-09-01T22:40:00")
        assert result.returncode == EXIT_ERROR
        assert "no timezone" in result.stderr

    def test_reports_are_written_where_asked(self, tmp_path: Path):
        result = llmops(
            "gate",
            *OUTAGE,
            *OBJECTIVES,
            *PRICES,
            "--at",
            DURING_OUTAGE,
            "--json-out",
            str(tmp_path / "r.json"),
            "--junit-out",
            str(tmp_path / "r.xml"),
            "--markdown-out",
            str(tmp_path / "r.md"),
        )
        assert result.returncode == EXIT_BUDGET_BURNED
        assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["passed"] is False
        parsed = ElementTree.fromstring((tmp_path / "r.xml").read_text(encoding="utf-8"))
        assert parsed.tag == "testsuite"
        assert (tmp_path / "r.md").read_text(encoding="utf-8").startswith("# Error budget")
        # With --json-out the report goes to the file and not also to stdout.
        assert result.stdout == ""

    def test_a_breached_objective_is_a_failing_junit_test_case(self, tmp_path: Path):
        # So it appears next to the unit tests, with the same red mark.
        llmops(
            "gate",
            *OUTAGE,
            *OBJECTIVES,
            *PRICES,
            "--at",
            DURING_OUTAGE,
            "--junit-out",
            str(tmp_path / "r.xml"),
        )
        suite = ElementTree.fromstring((tmp_path / "r.xml").read_text(encoding="utf-8"))
        failing = [case for case in suite if case.tag == "testcase" and list(case)]
        assert any(case.get("name") == "chat-availability" for case in failing)

    def test_with_cost_adds_a_spend_breakdown(self):
        result = llmops("gate", *STEADY, *OBJECTIVES, *PRICES, "--with-cost")
        assert result.returncode == EXIT_OK
        assert json.loads(result.stdout)["cost"]["calls"] > 0


class TestCost:
    def test_it_costs_the_shipped_window(self):
        result = llmops("cost", *STEADY, *PRICES)
        assert result.returncode == EXIT_OK, result.stderr
        assert json.loads(result.stdout)["total_usd"] > 0

    def test_it_says_there_was_nothing_to_reconcile(self):
        # "0 discrepancies" over 0 comparisons is not a clean bill of health.
        result = llmops("cost", *STEADY, *PRICES)
        assert "no provider-reported costs" in result.stderr

    @pytest.mark.parametrize("dimension", ["model", "provider", "operation", "outcome"])
    def test_every_grouping_works(self, dimension: str):
        result = llmops("cost", *STEADY, *PRICES, "--by", dimension)
        assert result.returncode == EXIT_OK
        assert json.loads(result.stdout)["dimension"] == dimension

    def test_a_ceiling_can_fail_the_build(self):
        result = llmops("cost", *STEADY, *PRICES, "--fail-over", "0.01")
        assert result.returncode == EXIT_BUDGET_BURNED
        assert "over the" in result.stderr

    def test_a_generous_ceiling_passes(self):
        assert llmops("cost", *STEADY, *PRICES, "--fail-over", "1000000").returncode == EXIT_OK

    def test_an_unpriced_model_names_every_one_at_once(self, tmp_path: Path):
        empty = tmp_path / "prices.yaml"
        empty.write_text(
            "prices: [{provider: nobody, model: nothing,"
            " input_per_million: 1, output_per_million: 1}]",
            encoding="utf-8",
        )
        result = llmops("cost", *STEADY, "--pricebook", str(empty))
        assert result.returncode == EXIT_ERROR
        # All of them, not one round trip per model.
        assert result.stderr.count("openai/") >= 2


class TestSynthAndCheck:
    def test_synth_writes_a_window_that_reads_back(self, tmp_path: Path):
        out = tmp_path / "w.jsonl"
        result = llmops("synth", "--scenario", "steady", "--out", str(out))
        assert result.returncode == EXIT_OK
        assert json.loads(result.stdout)["spans"] > 1000
        assert out.is_file()

    def test_it_is_deterministic(self, tmp_path: Path):
        one = json.loads(
            llmops("synth", "--scenario", "outage", "--out", str(tmp_path / "a.jsonl")).stdout
        )
        two = json.loads(
            llmops("synth", "--scenario", "outage", "--out", str(tmp_path / "b.jsonl")).stdout
        )
        assert one["digest"] == two["digest"]

    def test_a_different_seed_gives_a_different_window(self, tmp_path: Path):
        one = json.loads(
            llmops("synth", "--scenario", "steady", "--out", str(tmp_path / "a.jsonl")).stdout
        )
        two = json.loads(
            llmops(
                "synth", "--scenario", "steady", "--seed", "1", "--out", str(tmp_path / "b.jsonl")
            ).stdout
        )
        assert one["digest"] != two["digest"]

    @pytest.mark.parametrize("scenario", ["steady", "outage", "slowdown", "cost-spike"])
    def test_every_committed_window_still_matches_its_scenario(self, scenario: str):
        # The drift check. Compares digests rather than files: a window header
        # carries a span count and a digest, and a check that regenerates and
        # diffs would be red forever the moment any field were a timestamp.
        result = llmops(
            "check", "--scenario", scenario, "--window", str(WINDOWS / f"{scenario}.jsonl.gz")
        )
        assert result.returncode == EXIT_OK, result.stderr
        assert "matches scenario" in result.stdout

    def test_a_drifted_window_is_reported_with_the_command_to_fix_it(self, tmp_path: Path):
        # The negative control. Without it the check above is a `true`.
        llmops("synth", "--scenario", "steady", "--seed", "999", "--out", str(tmp_path / "w.jsonl"))
        result = llmops("check", "--scenario", "steady", "--window", str(tmp_path / "w.jsonl"))
        assert result.returncode == EXIT_ERROR
        assert "no longer matches" in result.stderr
        assert "llmops synth --scenario steady" in result.stderr

    def test_an_otlp_export_is_refused_without_the_flag(self, tmp_path: Path):
        # Nothing leaves the machine by default. A telemetry tool is exactly
        # the component that should not surprise anyone by sending somewhere.
        result = llmops(
            "synth",
            "--scenario",
            "steady",
            "--out",
            str(tmp_path / "w.jsonl"),
            "--export",
            "otlp",
            "--endpoint",
            "http://localhost:4318/v1/traces",
        )
        assert result.returncode == EXIT_ERROR
        assert "offline" in result.stderr
        assert "--allow-network" in result.stderr


class TestLabels:
    def test_bounded_labels_are_accepted(self):
        result = llmops("labels", "--metric", "m", "--label", "provider", "--label", "operation")
        assert result.returncode == EXIT_OK
        assert json.loads(result.stdout)["max_series"] == 80

    def test_an_unbounded_label_is_refused_with_the_alternative(self):
        result = llmops("labels", "--metric", "m", "--label", "tenant_id")
        assert result.returncode == EXIT_ERROR
        assert "tenant_tier" in result.stderr

    def test_a_label_can_be_declared_bounded(self):
        result = llmops(
            "labels", "--metric", "m", "--label", "tenant_tier", "--bound", "tenant_tier=4"
        )
        assert result.returncode == EXIT_OK
        assert json.loads(result.stdout)["max_series"] == 4

    def test_a_malformed_bound_is_reported(self):
        result = llmops("labels", "--metric", "m", "--label", "x", "--bound", "x")
        assert result.returncode == EXIT_ERROR
        assert "NAME=COUNT" in result.stderr

    def test_too_many_series_is_refused(self):
        result = llmops(
            "labels",
            "--metric",
            "m",
            "--label",
            "model",
            "--label",
            "route",
            "--label",
            "provider",
        )
        assert result.returncode == EXIT_ERROR
        assert "series" in result.stderr


class TestTheConsoleEncoding:
    def test_a_report_containing_a_non_ascii_character_does_not_crash(self, tmp_path: Path):
        # The failure the previous repository in this series shipped: stdout on
        # a Windows console defaults to cp1252, and a single non-ASCII byte in
        # a report raises UnicodeEncodeError *after* the whole evaluation has
        # been done. Only a real process can show it.
        objectives = tmp_path / "o.yaml"
        objectives.write_text(
            'objectives: [{name: "latency ≥ 3s — μ", kind: availability, target: 0.99}]',
            encoding="utf-8",
        )
        result = llmops("gate", *STEADY, "--objectives", str(objectives))
        assert result.returncode in {EXIT_OK, EXIT_BUDGET_BURNED}
        assert "UnicodeEncodeError" not in result.stderr
