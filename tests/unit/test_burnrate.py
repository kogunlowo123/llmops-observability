"""The burn-rate arithmetic.

Tested directly, without a span or a window in sight. This is the part of the
tool that decides whether a build goes red, it is thirty lines of division, and
a test of it should not have to build telemetry to ask what 14.4 times a budget
is.

The conventional table's claims are pinned here as executable assertions. A
README that says "detects a 2% budget burn within an hour" and nothing that
computes it is a README nobody can check.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from llmops.slo.burnrate import (
    DEFAULT_MIN_SAMPLES,
    SHORT_WINDOW_FRACTION,
    STANDARD_RULES,
    BurnRule,
    WindowMeasurement,
    decide,
    detection_time,
    evaluate_rule,
    windows_for,
)

pytestmark = pytest.mark.unit

#: A 99.9% objective over 30 days: the case every number in the table is
#: calibrated against.
THREE_NINES = 0.001
THIRTY_DAYS = timedelta(days=30)

FAST = STANDARD_RULES[0]


class TestTheConventionalTable:
    def test_it_is_the_four_rules_in_the_documented_order(self):
        assert [rule.name for rule in STANDARD_RULES] == [
            "fast-burn",
            "moderate-burn",
            "slow-burn",
            "creeping-burn",
        ]
        assert [rule.burn_rate for rule in STANDARD_RULES] == [14.4, 6.0, 3.0, 1.0]

    @pytest.mark.parametrize(
        ("name", "expected_budget_fraction"),
        [
            # The last column of the table, computed rather than quoted. If
            # these drift the table has been edited without its arithmetic.
            ("fast-burn", 0.02),
            ("moderate-burn", 0.05),
            ("slow-burn", 0.10),
            ("creeping-burn", 0.10),
        ],
    )
    def test_each_rule_triggers_at_the_documented_budget_fraction(
        self, name: str, expected_budget_fraction: float
    ):
        rule = next(item for item in STANDARD_RULES if item.name == name)
        assert rule.budget_consumed(THIRTY_DAYS) == pytest.approx(
            expected_budget_fraction, rel=0.01
        )

    def test_pages_are_the_short_rules_and_tickets_the_long_ones(self):
        # The severity split is not decoration: a three-day burn is a ticket
        # because there is time to fix it, and paging for one trains people to
        # ignore pages.
        for rule in STANDARD_RULES:
            if rule.long_window <= timedelta(hours=6):
                assert rule.severity == "page"
            else:
                assert rule.severity == "ticket"

    def test_a_short_window_defaults_to_a_twelfth_of_the_long_one(self):
        rule = BurnRule(
            name="derived", severity="page", long_window=timedelta(hours=12), burn_rate=6.0
        )
        assert rule.short == timedelta(hours=12) / SHORT_WINDOW_FRACTION
        assert rule.short == timedelta(hours=1)

    def test_both_windows_end_at_the_same_instant(self):
        # A short window offset from the long one would measure a different
        # incident, and the conjunction would be comparing two moments.
        from datetime import UTC, datetime

        now = datetime(2026, 9, 1, tzinfo=UTC)
        (long_start, long_end), (short_start, short_end) = windows_for(FAST, now=now)
        assert long_end == short_end == now
        assert long_start < short_start


class TestTheAdvertisedDetection:
    def test_it_detects_a_two_percent_budget_burn_within_the_hour(self):
        # The claim on the front of the README, computed. At 14.4x the budget,
        # one hour spends 2% of a 30-day budget, and the fast rule is exactly
        # the rule that fires then.
        rate = 14.4 * THREE_NINES
        assert detection_time(FAST, budget=THREE_NINES, failure_rate=rate) == timedelta(hours=1)

    def test_a_worse_incident_is_detected_sooner(self):
        # A total outage burns at 1000x and the sliding window crosses the
        # threshold once 14.4/1000 of it has filled — about a minute.
        total = detection_time(FAST, budget=THREE_NINES, failure_rate=1.0)
        assert total is not None
        assert timedelta(seconds=30) < total < timedelta(minutes=2)

    def test_a_blip_is_never_detected_by_the_fast_rule(self):
        # 0.1% budget spent over an hour is a burn of 0.72x. Silence is the
        # correct answer, and it is the half of the claim that usually goes
        # unasserted.
        assert detection_time(FAST, budget=THREE_NINES, failure_rate=0.0007) is None

    def test_a_healthy_service_is_never_detected(self):
        assert detection_time(FAST, budget=THREE_NINES, failure_rate=0.0) is None

    def test_no_budget_means_no_meaningful_detection_time(self):
        assert detection_time(FAST, budget=0.0, failure_rate=0.5) is None


class TestTheTwoWindowConjunction:
    def _at(self, long_rate: float, short_rate: float, *, samples: int = 1000) -> bool:
        return evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=samples,
            long_bad=round(samples * long_rate),
            short_samples=samples,
            short_bad=round(samples * short_rate),
        ).fired

    def test_both_windows_over_the_threshold_fires(self):
        assert self._at(0.02, 0.02) is True

    def test_the_long_window_alone_does_not_fire(self):
        # The incident is over. This is the short window's entire purpose: the
        # alert resets rather than smouldering for another hour.
        assert self._at(0.02, 0.0) is False

    def test_the_short_window_alone_does_not_fire(self):
        # A one-minute blip. Firing here is how a pager gets ignored.
        assert self._at(0.0, 0.5) is False

    def test_neither_does_not_fire(self):
        assert self._at(0.0005, 0.0005) is False

    def test_exactly_at_the_threshold_fires(self):
        # The comparison is >=. A rule that needed one more failed call than
        # the threshold would be a rule whose documented trigger is wrong.
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=100_000,
            long_bad=1440,
            short_samples=100_000,
            short_bad=1440,
        )
        assert result.long.burn == pytest.approx(14.4)
        assert result.fired is True


class TestTheMinimumSampleGuard:
    def test_a_thin_window_does_not_fire_however_bad_it_looks(self):
        # Ten calls, all failed. A 1000x burn rate, and it is a quiet Sunday.
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=10,
            long_bad=10,
            short_samples=10,
            short_bad=10,
        )
        assert result.fired is False

    def test_and_it_is_reported_as_not_evaluated_rather_than_as_passing(self):
        # The distinction the whole report is built on. "We did not look" is
        # not "we looked and it was fine".
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=10,
            long_bad=10,
            short_samples=10,
            short_bad=10,
        )
        assert result.under_sampled is True
        assert result.evaluated is False
        assert "not evaluated" in result.summary()

    def test_the_guard_is_on_the_long_window_only(self):
        # The short window is meant to be small; that is what makes the reset
        # fast. Applying a minimum to it would defeat its purpose.
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=DEFAULT_MIN_SAMPLES,
            long_bad=DEFAULT_MIN_SAMPLES,
            short_samples=2,
            short_bad=2,
        )
        assert result.fired is True

    def test_exactly_at_the_minimum_is_evaluated(self):
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=DEFAULT_MIN_SAMPLES,
            long_bad=0,
            short_samples=DEFAULT_MIN_SAMPLES,
            short_bad=0,
        )
        assert result.evaluated is True


class TestMeasurements:
    def test_an_empty_window_burns_nothing(self):
        measurement = WindowMeasurement.from_events(
            length=timedelta(hours=1), samples=0, bad=0, budget=THREE_NINES
        )
        assert measurement.burn == 0.0
        assert measurement.failure_rate == 0.0

    def test_a_zero_budget_makes_any_failure_an_infinite_burn(self):
        # A 100% objective. Objectives refuse to parse one, and the arithmetic
        # still has to answer honestly for a caller who builds it directly.
        measurement = WindowMeasurement.from_events(
            length=timedelta(hours=1), samples=100, bad=1, budget=0.0
        )
        assert measurement.burn == float("inf")

    def test_a_zero_budget_with_no_failures_burns_nothing(self):
        measurement = WindowMeasurement.from_events(
            length=timedelta(hours=1), samples=100, bad=0, budget=0.0
        )
        assert measurement.burn == 0.0

    def test_an_infinite_burn_is_finite_in_the_report(self):
        # json.dumps writes `Infinity`, which is not valid JSON and which half
        # the parsers downstream of a CI job reject.
        measurement = WindowMeasurement.from_events(
            length=timedelta(hours=1), samples=100, bad=1, budget=0.0
        )
        assert measurement.as_dict()["burn_rate"] == 1e9

    def test_spend_is_measured_against_a_pro_rata_share(self):
        # Spending exactly the window's share burns at 1.0 — the same meaning
        # the availability rules give the number, which is what makes one
        # engine enough for both.
        measurement = WindowMeasurement.from_spend(
            length=timedelta(hours=1), samples=100, spent_usd=1.0, pro_rata_usd=1.0
        )
        assert measurement.burn == 1.0
        assert measurement.unit == "usd"

    def test_spend_over_the_share_burns_proportionally(self):
        measurement = WindowMeasurement.from_spend(
            length=timedelta(hours=1), samples=100, spent_usd=3.0, pro_rata_usd=1.0
        )
        assert measurement.burn == 3.0

    def test_a_spend_report_carries_dollars_not_a_failure_rate(self):
        payload = WindowMeasurement.from_spend(
            length=timedelta(hours=1), samples=100, spent_usd=3.0, pro_rata_usd=1.0
        ).as_dict()
        assert payload["spent_usd"] == 3.0
        assert "failure_rate" not in payload

    def test_a_zero_budget_share_makes_any_spend_infinite(self):
        measurement = WindowMeasurement.from_spend(
            length=timedelta(hours=1), samples=1, spent_usd=0.01, pro_rata_usd=0.0
        )
        assert measurement.burn == float("inf")


class TestDecide:
    def test_it_is_the_one_place_the_verdict_is_reached(self):
        # Both objective kinds route through here, so a change to the
        # conjunction cannot apply to one and not the other.
        long = WindowMeasurement.from_spend(
            length=timedelta(days=1), samples=500, spent_usd=10.0, pro_rata_usd=1.0
        )
        short = WindowMeasurement.from_spend(
            length=timedelta(hours=2), samples=50, spent_usd=10.0, pro_rata_usd=1.0
        )
        rule = BurnRule(
            name="spend",
            severity="page",
            long_window=timedelta(days=1),
            short_window=timedelta(hours=2),
            burn_rate=2.0,
        )
        assert decide(rule, long, short).fired is True

    def test_a_summary_names_both_windows_and_the_threshold(self):
        result = evaluate_rule(
            FAST,
            budget=THREE_NINES,
            long_samples=1000,
            long_bad=20,
            short_samples=1000,
            short_bad=20,
        )
        summary = result.summary()
        assert "FIRING" in summary
        assert "1h" in summary
        assert "5m" in summary
        assert "14.4" in summary
