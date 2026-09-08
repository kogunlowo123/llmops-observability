"""Objective files: what they accept, and what they refuse to guess at."""

from __future__ import annotations

import textwrap
from datetime import timedelta
from decimal import Decimal

import pytest

from llmops.errors import ObjectiveError
from llmops.slo.burnrate import DEFAULT_MIN_SAMPLES, STANDARD_RULES
from llmops.slo.objective import ObjectiveSet, Selector, parse_objectives
from tests.conftest import make_span

pytestmark = pytest.mark.unit


def parse(text: str) -> ObjectiveSet:
    return parse_objectives(textwrap.dedent(text), source="<test>")


ONE = """
    objectives:
      - name: a
        kind: availability
        target: 0.999
    """


class TestDefaults:
    def test_an_objective_gets_the_conventional_rules(self):
        assert parse(ONE).objectives[0].rules == STANDARD_RULES

    def test_the_period_defaults_to_thirty_days(self):
        assert parse(ONE).objectives[0].period == timedelta(days=30)

    def test_the_error_budget_is_one_minus_the_target(self):
        assert parse(ONE).objectives[0].error_budget == pytest.approx(0.001)

    def test_a_file_level_minimum_reaches_every_standard_rule(self):
        # Rather than making the caller restate all four rules to change one
        # number.
        objective = parse(
            """
            objectives:
              - name: a
                kind: availability
                target: 0.999
                min_samples: 100
            """
        ).objectives[0]
        assert {rule.min_samples for rule in objective.rules} == {100}

    def test_without_one_the_default_applies(self):
        assert parse(ONE).objectives[0].rules[0].min_samples == DEFAULT_MIN_SAMPLES


class TestWhatIsRefused:
    def test_a_target_of_one_is_refused_with_an_explanation(self):
        # A 100% objective has a zero error budget: every failure is a total
        # burn and no rate is meaningful. Saying so at parse time is kinder
        # than saying it during an incident.
        with pytest.raises(ObjectiveError, match="between 0 and 1 exclusive"):
            parse("objectives: [{name: a, kind: availability, target: 1.0}]")

    def test_a_target_of_zero_is_refused(self):
        with pytest.raises(ObjectiveError):
            parse("objectives: [{name: a, kind: availability, target: 0}]")

    def test_a_latency_objective_without_a_threshold_is_refused(self):
        # Without it every call is slow and every window is an outage.
        with pytest.raises(ObjectiveError, match="threshold_ms"):
            parse("objectives: [{name: a, kind: latency, target: 0.99}]")

    def test_a_spend_objective_without_a_budget_is_refused(self):
        with pytest.raises(ObjectiveError, match="budget_usd"):
            parse("objectives: [{name: a, kind: spend}]")

    def test_a_zero_budget_is_refused(self):
        with pytest.raises(ObjectiveError, match="budget of 0"):
            parse("objectives: [{name: a, kind: spend, budget_usd: 0}]")

    def test_an_unknown_key_is_refused(self):
        # A misspelt threshold_ms on a latency objective would leave the
        # threshold at zero and make every window an outage.
        with pytest.raises(ObjectiveError, match="unknown key"):
            parse("objectives: [{name: a, kind: latency, target: 0.99, threshold_millis: 100}]")

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ObjectiveError, match="unknown kind"):
            parse("objectives: [{name: a, kind: throughput, target: 0.99}]")

    def test_two_objectives_with_one_name_are_refused(self):
        # Names identify findings in the report.
        with pytest.raises(ObjectiveError, match="two objectives are named"):
            parse(
                """
                objectives:
                  - {name: a, kind: availability, target: 0.99}
                  - {name: a, kind: availability, target: 0.98}
                """
            )

    def test_an_empty_file_is_refused(self):
        # A gate with nothing to check would pass unconditionally.
        with pytest.raises(ObjectiveError, match="declares no objectives"):
            parse("objectives: []")

    def test_yaml_is_loaded_safely(self):
        with pytest.raises(ObjectiveError):
            parse_objectives("!!python/object/apply:os.system ['echo pwned']")

    def test_a_short_window_longer_than_its_long_window_is_refused(self):
        with pytest.raises(ObjectiveError, match="not inside its long_window"):
            parse(
                """
                objectives:
                  - name: a
                    kind: availability
                    target: 0.99
                    rules:
                      - {name: r, long_window: 1h, short_window: 6h, burn_rate: 2}
                """
            )

    def test_a_selector_pattern_is_refused_because_there_is_no_pattern_syntax(self):
        # Exact equality only. A selector language is a place for a
        # catastrophically backtracking pattern to hide, in a file a CI job
        # reads from a repository.
        with pytest.raises(ObjectiveError, match="non-string selector"):
            parse(
                """
                objectives:
                  - name: a
                    kind: availability
                    target: 0.99
                    select:
                      model: [gpt-4o, gpt-4o-mini]
                """
            )


class TestDurations:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("30s", timedelta(seconds=30)),
            ("5m", timedelta(minutes=5)),
            ("6h", timedelta(hours=6)),
            ("30d", timedelta(days=30)),
        ],
    )
    def test_units(self, text: str, expected: timedelta):
        objective = parse(
            f"objectives: [{{name: a, kind: availability, target: 0.99, period: {text}}}]"
        ).objectives[0]
        assert objective.period == expected

    def test_a_bare_number_is_seconds(self):
        objective = parse(
            "objectives: [{name: a, kind: availability, target: 0.99, period: 600}]"
        ).objectives[0]
        assert objective.period == timedelta(seconds=600)

    def test_an_unknown_unit_is_refused(self):
        # No month or year unit: a month is not a fixed length, and an
        # objective that silently means 30 days on one file and 31 on another
        # is a bug waiting for February.
        with pytest.raises(ObjectiveError, match="unknown unit"):
            parse("objectives: [{name: a, kind: availability, target: 0.99, period: 1M}]")

    def test_an_unparseable_duration_is_refused(self):
        with pytest.raises(ObjectiveError, match="unparseable"):
            parse("objectives: [{name: a, kind: availability, target: 0.99, period: manyd}]")


class TestSelectors:
    def test_no_selector_matches_everything(self):
        assert Selector().matches(make_span()) is True

    def test_a_span_field_is_matched(self):
        assert Selector({"operation": "chat"}).matches(make_span(operation="chat")) is True
        assert Selector({"operation": "chat"}).matches(make_span(operation="embedding")) is False

    def test_an_attribute_is_matched(self):
        span = make_span(attributes={"environment": "prod"})
        assert Selector({"environment": "prod"}).matches(span) is True
        assert Selector({"environment": "staging"}).matches(span) is False

    def test_a_missing_attribute_does_not_match(self):
        assert Selector({"environment": "prod"}).matches(make_span()) is False

    def test_every_key_must_match(self):
        span = make_span(operation="chat", attributes={"environment": "prod"})
        assert Selector({"operation": "chat", "environment": "prod"}).matches(span) is True
        assert Selector({"operation": "chat", "environment": "dev"}).matches(span) is False


class TestWhatCountsAsBad:
    def test_availability_counts_unsuccessful_calls(self):
        objective = parse(ONE).objectives[0]
        assert objective.is_bad(make_span(outcome="error")) is True
        assert objective.is_bad(make_span(outcome="refusal")) is False

    def test_latency_counts_slow_successful_calls(self):
        objective = parse(
            "objectives: [{name: a, kind: latency, target: 0.99, threshold_ms: 1000}]"
        ).objectives[0]
        assert objective.is_bad(make_span(duration_ms=1500)) is True
        assert objective.is_bad(make_span(duration_ms=500)) is False

    def test_a_failed_call_is_not_a_slow_call(self):
        # Counting it as one means an outage silently consumes the latency
        # budget too, and the two objectives stop being independent
        # measurements of different things.
        objective = parse(
            "objectives: [{name: a, kind: latency, target: 0.99, threshold_ms: 1000}]"
        ).objectives[0]
        assert objective.is_bad(make_span(outcome="error", duration_ms=9000)) is False

    def test_spend_has_a_budget_rather_than_a_target(self):
        objective = parse("objectives: [{name: a, kind: spend, budget_usd: 100}]").objectives[0]
        assert objective.budget_usd == Decimal(100)
        assert objective.error_budget == 1.0


class TestDigests:
    def test_the_digest_changes_with_the_target(self):
        one = parse("objectives: [{name: a, kind: availability, target: 0.99}]")
        two = parse("objectives: [{name: a, kind: availability, target: 0.999}]")
        assert one.digest() != two.digest()

    def test_the_digest_changes_with_a_rule_threshold(self):
        one = parse(
            """
            objectives:
              - name: a
                kind: availability
                target: 0.99
                rules: [{name: r, long_window: 1h, burn_rate: 6}]
            """
        )
        two = parse(
            """
            objectives:
              - name: a
                kind: availability
                target: 0.99
                rules: [{name: r, long_window: 1h, burn_rate: 14.4}]
            """
        )
        assert one.digest() != two.digest()

    def test_the_digest_ignores_a_description(self):
        # Prose is not a measurement, and a report whose digest changes when
        # somebody improves a sentence is a report nobody trusts.
        one = parse("objectives: [{name: a, kind: availability, target: 0.99}]")
        two = parse("objectives: [{name: a, kind: availability, target: 0.99, description: hello}]")
        assert one.digest() == two.digest()

    def test_the_digest_ignores_objective_ordering(self):
        one = parse(
            """
            objectives:
              - {name: a, kind: availability, target: 0.99}
              - {name: b, kind: availability, target: 0.98}
            """
        )
        two = parse(
            """
            objectives:
              - {name: b, kind: availability, target: 0.98}
              - {name: a, kind: availability, target: 0.99}
            """
        )
        assert one.digest() == two.digest()
