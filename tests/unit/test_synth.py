"""The corpus generator.

Determinism is the whole contract: a committed window can be regenerated and
checked rather than taken on trust, and that is only true if the same seed gives
the same spans on every machine, forever.

The rest of these are about the corpus being *shaped* enough to demonstrate what
it has to. A generator that produced a flat arrival rate and a constant latency
would make every burn-rate assertion below vacuous.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from llmops.corpus.synth import (
    DEFAULT_MODELS,
    DEFAULT_START,
    SCENARIOS,
    Incident,
    Scenario,
    generate,
)

pytestmark = pytest.mark.unit


SHORT = Scenario(name="short", duration=timedelta(hours=6), peak_calls_per_hour=60)


class TestDeterminism:
    def test_the_same_scenario_gives_the_same_spans(self):
        assert generate(SHORT).digest() == generate(SHORT).digest()

    def test_byte_for_byte_including_the_ids(self):
        one = [span.span_id for span in generate(SHORT)]
        two = [span.span_id for span in generate(SHORT)]
        assert one == two

    def test_a_different_seed_gives_different_spans(self):
        other = Scenario(
            name="short",
            duration=SHORT.duration,
            seed=SHORT.seed + 1,
            peak_calls_per_hour=SHORT.peak_calls_per_hour,
        )
        assert generate(SHORT).digest() != generate(other).digest()

    def test_the_start_is_fixed_rather_than_now(self):
        # Regenerating the corpus tomorrow has to produce the same file.
        assert generate(SHORT).start == DEFAULT_START


class TestShape:
    def test_the_window_covers_what_the_scenario_asked_for(self):
        window = generate(SHORT)
        assert window.duration == timedelta(hours=6)

    def test_every_span_falls_inside_the_interval(self):
        window = generate(SHORT)
        assert all(window.start <= span.started_at < window.end for span in window)

    def test_the_arrival_rate_follows_a_daily_cycle(self):
        # Not because real traffic is a cosine, but because a flat rate would
        # make the corpus useless for what it exists to demonstrate.
        window = generate(Scenario(name="day", duration=timedelta(days=1), peak_calls_per_hour=200))
        by_hour = [0] * 24
        for span in window:
            by_hour[span.started_at.hour] += 1
        assert max(by_hour) > 2 * min(by_hour)

    def test_latency_has_a_long_tail(self):
        # A normal distribution's tail is far too thin for request latency, and
        # a corpus with one would make every latency objective trivial.
        durations = sorted(span.duration_ms for span in generate(SHORT))
        median = durations[len(durations) // 2]
        p99 = durations[int(len(durations) * 0.99)]
        assert p99 > 2.5 * median

    def test_every_model_in_the_mix_appears(self):
        models = {model for _, model in generate(SHORT).models()}
        assert models == {mix.model for mix in DEFAULT_MODELS}

    def test_the_corpus_carries_only_bounded_attributes(self):
        # The corpus should not itself contain the cardinality mistake this
        # tool refuses.
        from llmops.cardinality.budget import DEFAULT_LABELS, LabelRegistry

        registry = LabelRegistry(DEFAULT_LABELS)
        keys = {key for span in generate(SHORT) for key in span.attributes}
        for key in keys:
            assert registry.describe(key).bounded, key

    def test_it_says_it_was_generated(self):
        metadata = generate(SHORT).metadata
        assert metadata["generated"] == "true"
        assert "Synthesised" in metadata["note"]

    def test_a_failed_call_produces_no_output_tokens(self):
        window = generate(
            Scenario(
                name="broken",
                duration=timedelta(hours=2),
                peak_calls_per_hour=200,
                incidents=(
                    Incident(
                        name="x",
                        starts_after=timedelta(0),
                        duration=timedelta(hours=2),
                        failure_rate=0.5,
                    ),
                ),
            )
        )
        assert all(span.output_tokens == 0 for span in window if not span.succeeded)

    def test_refusals_are_present_because_they_are_the_interesting_case(self):
        # A refusal distinguishes an availability objective that counts them as
        # outages from one that does not. A corpus with none cannot show it.
        window = generate(
            Scenario(name="long", duration=timedelta(days=1), peak_calls_per_hour=300)
        )
        assert any(span.outcome == "refusal" for span in window)


class TestIncidents:
    def _window(self, incident: Incident):
        return generate(
            Scenario(
                name="incident",
                duration=timedelta(hours=6),
                peak_calls_per_hour=200,
                incidents=(incident,),
            )
        )

    def test_failures_are_confined_to_the_incident_window(self):
        incident = Incident(
            name="outage",
            starts_after=timedelta(hours=2),
            duration=timedelta(hours=1),
            failure_rate=0.9,
        )
        window = self._window(incident)
        start = window.start + timedelta(hours=2)
        failed = [span.started_at for span in window if not span.succeeded]
        # The background failure rate is not zero, so this checks the density
        # rather than demanding purity.
        inside = sum(1 for at in failed if start <= at < start + timedelta(hours=1))
        assert inside > 0.9 * len(failed)

    def test_a_latency_incident_leaves_the_failure_rate_alone(self):
        # The scenario that breaks a latency objective and nothing else.
        incident = Incident(
            name="slow",
            starts_after=timedelta(hours=2),
            duration=timedelta(hours=1),
            latency_multiplier=5.0,
        )
        window = self._window(incident)
        during = [
            span
            for span in window
            if window.start + timedelta(hours=2)
            <= span.started_at
            < window.start + timedelta(hours=3)
        ]
        assert all(span.succeeded for span in during) or len(
            [span for span in during if not span.succeeded]
        ) < 0.01 * len(during)

    def test_a_cost_incident_leaves_latency_alone(self):
        # And the scenario that breaks a spend objective and nothing else.
        incident = Incident(
            name="leak",
            starts_after=timedelta(hours=2),
            duration=timedelta(hours=1),
            input_multiplier=8.0,
        )
        window = self._window(incident)
        inside = [
            span.duration_ms
            for span in window
            if window.start + timedelta(hours=2)
            <= span.started_at
            < window.start + timedelta(hours=3)
        ]
        outside = [
            span.duration_ms
            for span in window
            if span.started_at < window.start + timedelta(hours=2)
        ]
        assert abs(_mean(inside) - _mean(outside)) < 0.25 * _mean(outside)

    def test_an_incident_can_be_confined_to_one_model(self):
        incident = Incident(
            name="one-model",
            starts_after=timedelta(0),
            duration=timedelta(hours=6),
            failure_rate=0.9,
            model="gpt-4o-mini",
        )
        window = self._window(incident)
        failing_models = {span.model for span in window if span.outcome == "error"}
        assert failing_models == {"gpt-4o-mini"}


class TestTheShippedScenarios:
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_each_one_generates(self, name: str):
        window = generate(SCENARIOS[name])
        assert window.total > 1000
        assert window.duration == timedelta(days=3)

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_each_one_says_what_it_shows(self, name: str):
        # The metadata is the documentation a reader finds in the file itself.
        assert SCENARIOS[name].metadata["shows"]

    def test_each_one_has_a_different_seed(self):
        seeds = [scenario.seed for scenario in SCENARIOS.values()]
        assert len(set(seeds)) == len(seeds)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
