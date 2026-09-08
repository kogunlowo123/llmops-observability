"""The gate's own negative controls.

A gate that has only ever been run against a healthy platform proves nothing
about whether it would notice an unhealthy one. Every test here breaks exactly
one thing about a shipped, green setup and asserts the build goes red — and one
control asserts the untouched setup is green, so the difference is the change
and not the harness.

**If this file is deleted, the claim on the front of the README stops being
supported by anything.**

These run the real command line as a subprocess for the same reason: the exit
code is the interface, and only a process has one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from llmops.errors import EXIT_BUDGET_BURNED, EXIT_ERROR, EXIT_OK
from llmops.telemetry.window import open_window_text
from tests.conftest import EXAMPLES, WINDOWS

pytestmark = pytest.mark.meta


def gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "llmops", "gate", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def breached(result: subprocess.CompletedProcess[str]) -> set[str]:
    """Which objectives the report says were breached."""
    document = json.loads(result.stdout)
    return {
        entry["objective"]["name"]
        for entry in document["evaluation"]["objectives"]
        if entry["breached"]
    }


HEALTHY = (
    "--window",
    str(WINDOWS / "steady.jsonl.gz"),
    "--objectives",
    str(EXAMPLES / "objectives.yaml"),
    "--pricebook",
    str(EXAMPLES / "prices.yaml"),
)


class TestTheControl:
    def test_the_healthy_setup_is_green(self):
        # The control. Every test below changes one thing from here, so if this
        # is red the others prove nothing.
        result = gate(*HEALTHY)
        assert result.returncode == EXIT_OK, result.stderr
        assert breached(result) == set()


class TestEachIncidentTurnsTheBuildRed:
    """One scenario, one objective, and nothing else.

    Three windows, each containing a different failure, each breaking exactly
    the objective that is supposed to see it. The "and nothing else" half is
    what shows the three objectives measure different things rather than three
    views of the same signal.
    """

    @pytest.mark.parametrize(
        ("scenario", "at", "expected"),
        [
            ("outage", "2026-09-01T22:40:00+00:00", "chat-availability"),
            ("slowdown", "2026-09-02T12:00:00+00:00", "chat-latency"),
            ("cost-spike", "2026-09-03T12:00:00+00:00", "monthly-spend"),
        ],
    )
    def test_it_breaks_its_objective_and_no_other(self, scenario, at, expected):
        result = gate(
            "--window",
            str(WINDOWS / f"{scenario}.jsonl.gz"),
            "--objectives",
            str(EXAMPLES / "objectives.yaml"),
            "--pricebook",
            str(EXAMPLES / "prices.yaml"),
            "--at",
            at,
        )
        assert result.returncode == EXIT_BUDGET_BURNED, result.stderr
        assert breached(result) == {expected}

    @pytest.mark.parametrize(
        "at",
        [
            "2026-09-01T22:40:00+00:00",
            "2026-09-02T12:00:00+00:00",
            "2026-09-03T12:00:00+00:00",
        ],
    )
    def test_the_healthy_window_is_green_at_the_same_instants(self, at: str):
        # Without this, each finding above could be a fact about the moment it
        # was measured rather than about the incident.
        assert gate(*HEALTHY, "--at", at).returncode == EXIT_OK

    def test_an_incident_that_has_ended_no_longer_fires(self):
        # The short window's whole purpose: the alert resets rather than
        # smouldering for the length of the long window. Gating the outage
        # window at its *end* is green, and that is correct.
        result = gate(
            "--window",
            str(WINDOWS / "outage.jsonl.gz"),
            "--objectives",
            str(EXAMPLES / "objectives.yaml"),
            "--pricebook",
            str(EXAMPLES / "prices.yaml"),
        )
        assert result.returncode == EXIT_OK, result.stderr


class TestBreakingTheObjectives:
    """Weaken or tighten the declarations and assert the verdict follows."""

    def _objectives(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "objectives.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_sparse_failure_signal_does_not_fire_however_tight_the_target(self, tmp_path: Path):
        # The other half of the conjunction, and the one worth writing down.
        #
        # The healthy window has about three failed calls in three days. Against
        # a 99.999% target that is a burn rate of fifty, and it still does not
        # fire — because the five-minute short window almost never contains one
        # of those three failures, and both windows have to agree.
        #
        # This is the tool working correctly, not a gap. An alert that fired
        # here would be paging somebody about a single failed call that happened
        # at some point this week. It is asserted so a future change to the
        # conjunction cannot quietly turn this into a page.
        objectives = self._objectives(
            tmp_path,
            "objectives:\n"
            "  - {name: very-tight, kind: availability, target: 0.99999, period: 30d}\n",
        )
        result = gate("--window", str(WINDOWS / "steady.jsonl.gz"), "--objectives", str(objectives))
        assert result.returncode == EXIT_OK
        # And the rules did reach a verdict. This is not the under-sampled case
        # dressed up as a pass.
        entry = json.loads(result.stdout)["evaluation"]["objectives"][0]
        assert any(item["evaluated"] for item in entry["rules"])

    def test_an_impossible_latency_threshold_turns_it_red(self, tmp_path: Path):
        objectives = self._objectives(
            tmp_path,
            "objectives:\n"
            "  - {name: impossible, kind: latency, target: 0.99,"
            " threshold_ms: 1, period: 30d}\n",
        )
        assert (
            gate(
                "--window", str(WINDOWS / "steady.jsonl.gz"), "--objectives", str(objectives)
            ).returncode
            == EXIT_BUDGET_BURNED
        )

    def test_a_tiny_spend_budget_turns_it_red(self, tmp_path: Path):
        objectives = self._objectives(
            tmp_path,
            "objectives:\n  - {name: broke, kind: spend, budget_usd: 0.01, period: 30d}\n",
        )
        assert (
            gate(
                "--window",
                str(WINDOWS / "steady.jsonl.gz"),
                "--objectives",
                str(objectives),
                "--pricebook",
                str(EXAMPLES / "prices.yaml"),
            ).returncode
            == EXIT_BUDGET_BURNED
        )

    def test_a_malformed_objectives_file_exits_three_not_two(self, tmp_path: Path):
        # Nothing was measured, so nothing was missed. A CI job that cannot
        # tell these apart retries a broken pipeline until it goes green.
        objectives = self._objectives(tmp_path, "objectives:\n  - not a mapping\n")
        result = gate("--window", str(WINDOWS / "steady.jsonl.gz"), "--objectives", str(objectives))
        assert result.returncode == EXIT_ERROR

    def test_an_objective_with_no_rules_that_can_run_is_not_reported_as_passing(
        self, tmp_path: Path
    ):
        # A rule that looks back further than the data has reached no verdict.
        # Reporting it as held would be the tool asserting a fact it does not
        # have — the failure mode this whole layer exists to catch.
        objectives = self._objectives(
            tmp_path,
            "objectives:\n"
            "  - name: unmeasurable\n"
            "    kind: availability\n"
            "    target: 0.99\n"
            "    rules:\n"
            "      - {name: month-long, long_window: 30d, burn_rate: 1}\n",
        )
        result = gate(
            "--window",
            str(WINDOWS / "steady.jsonl.gz"),
            "--objectives",
            str(objectives),
            "--markdown-out",
            str(tmp_path / "r.md"),
        )
        document = json.loads(result.stdout)
        entry = document["evaluation"]["objectives"][0]
        assert entry["rules"] == []
        assert entry["not_covered"][0]["rule"] == "month-long"
        # And the summary says so out loud rather than showing a green tick.
        assert "Not evaluated" in (tmp_path / "r.md").read_text(encoding="utf-8")


class TestBreakingTheMeasurement:
    """Damage the evidence rather than the platform."""

    def test_a_window_from_a_different_scenario_is_caught_by_the_drift_check(self, tmp_path: Path):
        # Not by the gate — the gate would happily evaluate it. This is what
        # `llmops check` is for, and without it a committed corpus could drift
        # from the scenario that documents it with nothing to notice.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "llmops",
                "synth",
                "--scenario",
                "steady",
                "--seed",
                "12345",
                "--out",
                str(tmp_path / "w.jsonl"),
            ],
            capture_output=True,
            check=True,
            timeout=300,
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "llmops",
                "check",
                "--scenario",
                "steady",
                "--window",
                str(tmp_path / "w.jsonl"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert result.returncode == EXIT_ERROR
        assert "no longer matches" in result.stderr

    def test_a_damaged_window_still_produces_a_verdict_that_says_it_is_damaged(
        self, tmp_path: Path
    ):
        # A verdict computed over 97% of the evidence should say so rather than
        # silently being a verdict about 97% of the evidence.
        with open_window_text(WINDOWS / "steady.jsonl.gz") as handle:
            source = handle.read().split("\n")
        damaged = tmp_path / "w.jsonl"
        damaged.write_text(
            "\n".join([*source[:100], '{"span_id": "truncated"', *source[100:]]),
            encoding="utf-8",
        )
        result = gate(
            "--window",
            str(damaged),
            "--objectives",
            str(EXAMPLES / "objectives.yaml"),
            "--pricebook",
            str(EXAMPLES / "prices.yaml"),
        )
        assert json.loads(result.stdout)["evaluation"]["unreadable_lines"] == 1

    def test_removing_the_price_book_does_not_silently_skip_the_spend_objective(self):
        # The failure that would make a spend gate decorative.
        result = gate(
            "--window",
            str(WINDOWS / "steady.jsonl.gz"),
            "--objectives",
            str(EXAMPLES / "objectives.yaml"),
        )
        assert result.returncode == EXIT_ERROR
        assert "spend" in result.stderr

    def test_an_unpriced_model_is_not_costed_at_zero(self, tmp_path: Path):
        # A spend report that silently omits a model is a number somebody will
        # put in a slide.
        prices = tmp_path / "prices.yaml"
        prices.write_text(
            "prices:\n  - {provider: openai, model: gpt-4o-mini,"
            " input_per_million: 1, output_per_million: 1}\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "llmops",
                "cost",
                "--window",
                str(WINDOWS / "steady.jsonl.gz"),
                "--pricebook",
                str(prices),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert result.returncode == EXIT_ERROR
        assert "unpriced model" in result.stderr


class TestTheShippedExamplesStillRun:
    @pytest.mark.parametrize("script", ["quickstart.py", "burnrate_demo.py", "cardinality_demo.py"])
    def test_each_example_runs(self, script: str):
        # They are documentation that executes. One that has stopped working is
        # a README that lies.
        result = subprocess.run(
            [sys.executable, str(EXAMPLES.parent / "examples" / script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr
