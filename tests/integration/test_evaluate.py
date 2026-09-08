"""Objectives applied to windows: the join between the arithmetic and the data."""

from __future__ import annotations

import textwrap
from datetime import timedelta

import pytest

from llmops.cost.pricebook import PriceBook, parse_pricebook
from llmops.errors import ObjectiveError
from llmops.slo.evaluate import evaluate
from llmops.slo.objective import ObjectiveSet, parse_objectives
from llmops.telemetry.window import build_window
from tests.conftest import ORIGIN, spread

pytestmark = pytest.mark.integration


def objectives(text: str) -> ObjectiveSet:
    return parse_objectives(textwrap.dedent(text), source="<test>")


AVAILABILITY = """
    objectives:
      - name: availability
        kind: availability
        target: 0.99
        period: 30d
        min_samples: 10
        rules:
          - name: fast-burn
            severity: page
            long_window: 1h
            short_window: 5m
            burn_rate: 14.4
    """


class TestAvailability:
    def _window(self, *, bad: int, count: int = 600):
        # An hour of traffic ending at ORIGIN, with the failures at the end so
        # both windows see them.
        return build_window(
            spread(count, over=timedelta(hours=1), ending_at=ORIGIN, bad=bad),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )

    def test_a_healthy_window_holds(self):
        result = evaluate(self._window(bad=0), objectives(AVAILABILITY))
        assert result.passed is True
        assert result.breached == ()

    def test_a_bad_window_breaches(self):
        # 100 of 600 failed: a 16.7% error rate against a 1% budget is a burn
        # of 16.7x, over the 14.4x threshold, and the failures are recent
        # enough that the short window sees them too.
        result = evaluate(self._window(bad=100), objectives(AVAILABILITY))
        assert result.passed is False
        assert [item.objective.name for item in result.breached] == ["availability"]

    def test_the_severity_comes_from_the_rule(self):
        result = evaluate(self._window(bad=100), objectives(AVAILABILITY))
        assert result.breached[0].worst_severity == "page"

    def test_a_recovered_incident_does_not_fire(self):
        # Failures at the start of the hour, none recently. The long window
        # still sees them; the short one does not, and the alert has reset.
        spans = spread(
            60, over=timedelta(minutes=5), ending_at=ORIGIN - timedelta(minutes=50), bad=60
        )
        spans += spread(540, over=timedelta(minutes=50), ending_at=ORIGIN, start_index=1000)
        window = build_window(spans, start=ORIGIN - timedelta(hours=1), end=ORIGIN)

        assert evaluate(window, objectives(AVAILABILITY)).passed is True

    def test_a_refusal_does_not_consume_the_availability_budget(self):
        window = build_window(
            spread(600, over=timedelta(hours=1), ending_at=ORIGIN, outcome="refusal"),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        assert evaluate(window, objectives(AVAILABILITY)).passed is True


class TestSelectors:
    SCOPED = """
        objectives:
          - name: chat-only
            kind: availability
            target: 0.99
            min_samples: 10
            select:
              operation: chat
            rules:
              - {name: r, severity: page, long_window: 1h, short_window: 5m, burn_rate: 14.4}
        """

    def test_out_of_scope_failures_are_invisible(self):
        # Every embedding fails, and the chat objective holds. Without this the
        # selector is decorative.
        spans = spread(300, over=timedelta(hours=1), ending_at=ORIGIN, operation="chat")
        spans += spread(
            300,
            over=timedelta(hours=1),
            ending_at=ORIGIN,
            bad=300,
            operation="embedding",
            start_index=1000,
        )
        window = build_window(spans, start=ORIGIN - timedelta(hours=1), end=ORIGIN)

        result = evaluate(window, objectives(self.SCOPED))

        assert result.passed is True
        assert result.results[0].in_scope == 300

    def test_in_scope_failures_are_counted(self):
        spans = spread(300, over=timedelta(hours=1), ending_at=ORIGIN, bad=60, operation="chat")
        # 60 of 300 is a 20% error rate: a burn of 20x, over the threshold.
        window = build_window(spans, start=ORIGIN - timedelta(hours=1), end=ORIGIN)
        assert evaluate(window, objectives(self.SCOPED)).passed is False


class TestLatency:
    LATENCY = """
        objectives:
          - name: latency
            kind: latency
            target: 0.99
            threshold_ms: 1000
            min_samples: 10
            rules:
              - {name: r, severity: page, long_window: 1h, short_window: 5m, burn_rate: 5}
        """

    def test_slow_calls_burn_the_budget(self):
        spans = spread(500, over=timedelta(hours=1), ending_at=ORIGIN, duration_ms=200.0)
        spans += spread(
            100,
            over=timedelta(minutes=5),
            ending_at=ORIGIN,
            duration_ms=5000.0,
            start_index=1000,
        )
        window = build_window(spans, start=ORIGIN - timedelta(hours=1), end=ORIGIN)
        assert evaluate(window, objectives(self.LATENCY)).passed is False

    def test_fast_calls_do_not(self):
        window = build_window(
            spread(600, over=timedelta(hours=1), ending_at=ORIGIN, duration_ms=200.0),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        assert evaluate(window, objectives(self.LATENCY)).passed is True

    def test_an_outage_does_not_also_burn_the_latency_budget(self):
        # The two objectives measure different things, and a failed call is not
        # a slow call however long it took to fail.
        window = build_window(
            spread(600, over=timedelta(hours=1), ending_at=ORIGIN, bad=300, duration_ms=9000.0),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        assert evaluate(window, objectives(self.LATENCY)).passed is True


class TestSpend:
    SPEND = """
        objectives:
          - name: spend
            kind: spend
            budget_usd: 720
            period: 30d
            min_samples: 10
            rules:
              - {name: r, severity: page, long_window: 1h, short_window: 5m, burn_rate: 2}
        """

    @pytest.fixture
    def prices(self) -> PriceBook:
        # $1 per million input tokens, nothing for output: a span with a
        # million input tokens costs exactly one dollar.
        return parse_pricebook(
            "prices: [{provider: openai, model: gpt-4o-mini,"
            " input_per_million: 1.0, output_per_million: 0.0}]",
            source="<test>",
        )

    def _window(self, dollars: int):
        # `dollars` spans of a million input tokens each, in the last hour.
        return build_window(
            spread(
                dollars,
                over=timedelta(hours=1),
                ending_at=ORIGIN,
                input_tokens=1_000_000,
                output_tokens=0,
            ),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )

    def test_spending_the_pro_rata_share_burns_at_one(self, prices: PriceBook):
        # $720 over 30 days is $1 an hour. Twelve spans is $12 an hour, a burn
        # of 12x — over the 2x threshold.
        result = evaluate(self._window(12), objectives(self.SPEND), pricebook=prices)
        assert result.passed is False

    def test_spending_under_the_share_holds(self, prices: PriceBook):
        # Fewer than 30 calls would trip the minimum-samples guard, so this
        # uses cheap calls rather than few of them.
        window = build_window(
            spread(
                60,
                over=timedelta(hours=1),
                ending_at=ORIGIN,
                input_tokens=1000,
                output_tokens=0,
            ),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        assert evaluate(window, objectives(self.SPEND), pricebook=prices).passed is True

    def test_a_spend_objective_without_a_price_book_is_an_error(self):
        # Not a silent pass. A spend objective that cannot be checked has not
        # been checked.
        with pytest.raises(ObjectiveError, match="no price book"):
            evaluate(self._window(12), objectives(self.SPEND))

    def test_the_report_carries_dollars_for_a_spend_rule(self, prices: PriceBook):
        result = evaluate(self._window(12), objectives(self.SPEND), pricebook=prices)
        window = result.results[0].results[0].as_dict()["long_window"]
        assert window["unit"] == "usd"
        assert window["spent_usd"] == pytest.approx(12.0)


class TestCoverage:
    def test_a_rule_longer_than_the_data_is_not_evaluated(self):
        # A three-day rule over a six-hour window is being asked a question the
        # data cannot answer. Reporting it as `ok` would be the tool asserting
        # a fact it does not have.
        window = build_window(
            spread(600, over=timedelta(hours=6), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=6),
            end=ORIGIN,
        )
        result = evaluate(
            window,
            parse_objectives(
                textwrap.dedent(
                    """
            objectives:
              - name: a
                kind: availability
                target: 0.99
                rules:
                  - {name: three-day, long_window: 3d, burn_rate: 1}
            """
                )
            ),
        )
        assert result.results[0].results == ()
        assert result.results[0].not_covered[0][0] == "three-day"
        assert "3d" in result.results[0].not_covered[0][1]

    def test_an_under_sampled_rule_is_reported_as_such(self):
        window = build_window(
            spread(5, over=timedelta(hours=1), ending_at=ORIGIN, bad=5),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        result = evaluate(
            window, objectives(AVAILABILITY.replace("min_samples: 10", "min_samples: 100"))
        )
        assert result.passed is True
        assert result.results[0].results[0].evaluated is False

    def test_unreadable_lines_are_carried_into_the_report(self):
        window = build_window(
            spread(600, over=timedelta(hours=1), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        from dataclasses import replace

        damaged = replace(window, unreadable=((7, "not valid JSON"),))
        # A verdict computed over 97% of the evidence should say so.
        assert evaluate(damaged, objectives(AVAILABILITY)).unreadable == 1


class TestTheEvaluationInstant:
    def test_every_rule_measures_intervals_ending_at_the_same_moment(self):
        # Taking the clock separately per rule would introduce a skew that is
        # invisible in a test and real in production.
        window = build_window(
            spread(600, over=timedelta(hours=1), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        result = evaluate(window, objectives(AVAILABILITY))
        assert result.evaluated_at == ORIGIN

    def test_an_explicit_instant_is_used(self):
        window = build_window(
            spread(600, over=timedelta(hours=2), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=2),
            end=ORIGIN,
        )
        earlier = ORIGIN - timedelta(minutes=30)
        assert evaluate(window, objectives(AVAILABILITY), now=earlier).evaluated_at == earlier
