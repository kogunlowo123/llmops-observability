"""Generating a telemetry window nobody has to trust.

Every other tool in this series gates on an artefact recorded from something
real. This one gates on a *window*, and a window has to come from a running
platform — which a reader of this repository does not have, and which would make
every test depend on a service.

So the corpus is synthesised, deterministically, by this module, and that is
stated plainly rather than left for someone to discover: **the windows in
``examples/`` were generated, not captured.** They are shaped like real traffic —
diurnal arrival rate, a long-tailed latency distribution, a mix of models, a
small background failure rate — and none of it happened.

That is the right trade for what this repository has to demonstrate. A generated
window can contain an incident of a known size at a known time, which is what
makes it possible to assert that a burn-rate rule fires *at the right moment*
rather than merely that it fires. A captured window could not do that without
waiting for a real outage.

Determinism is the whole contract. The same seed gives the same spans, byte for
byte, on any machine, forever — so a committed window can be regenerated and
checked rather than taken on trust.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from llmops.telemetry.span import Operation, Outcome, Span
from llmops.telemetry.window import Window, build_window

DEFAULT_SEED = 20260908

#: How often a chat call is refused on safety grounds. Deliberately present in
#: the corpus: it is the case that distinguishes an availability objective which
#: counts refusals as outages from one that does not.
REFUSAL_RATE = 0.004

#: How often a call gets any prompt-cache benefit at all. Providers cache on a
#: prefix, so a call either hits or it does not; a uniform partial hit rate
#: would understate the variance a cost report has to survive.
CACHE_HIT_CHANCE = 0.5

#: The instant the shipped example window starts. Fixed rather than "now", so
#: regenerating the corpus tomorrow produces the same file.
DEFAULT_START = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ModelMix:
    """One model in the traffic, and how it behaves."""

    provider: str
    model: str
    #: Relative share of traffic.
    weight: float
    operation: Operation = "chat"
    #: Median latency. The distribution is log-normal around it, which is what
    #: request latency actually looks like: a floor, a hump, and a long tail.
    median_latency_ms: float = 900.0
    #: Spread of the log-normal, in natural-log units. 0.5 gives a p99 around
    #: three and a half times the median, which matches observed model latency
    #: better than a normal distribution, whose tail is far too thin.
    latency_sigma: float = 0.5
    mean_input_tokens: int = 600
    mean_output_tokens: int = 220
    #: Fraction of this model's calls served from the provider's prompt cache.
    cache_hit_rate: float = 0.0
    #: Background failure rate, outside any injected incident.
    base_failure_rate: float = 0.0005


@dataclass(frozen=True, slots=True)
class Incident:
    """A period of degraded behaviour, injected at a known time.

    This is the reason the corpus is generated. An incident of a known size at a
    known instant is what lets a test assert that a rule fires at the right
    moment and not merely that it fires.
    """

    name: str
    starts_after: timedelta
    duration: timedelta
    #: Fraction of calls that fail during the incident.
    failure_rate: float = 0.0
    #: Multiplier applied to latency during the incident.
    latency_multiplier: float = 1.0
    #: Multiplier applied to output tokens, for a cost incident.
    token_multiplier: float = 1.0
    #: Multiplier applied to input tokens. The realistic cost incident is not a
    #: chattier model, it is a context window that stopped being trimmed: input
    #: grows by an order of magnitude, output barely moves, and nothing else in
    #: the telemetry changes at all.
    input_multiplier: float = 1.0
    #: Restrict the incident to one model, as a real one usually is.
    model: str = ""
    error_type: str = "upstream_unavailable"


@dataclass(frozen=True, slots=True)
class Scenario:
    """Everything the generator needs."""

    name: str
    duration: timedelta = timedelta(days=3)
    #: Calls per hour at the daily peak. The rate follows a diurnal curve, so
    #: the mean is roughly two thirds of this.
    #:
    #: 120 is chosen so a shipped three-day window is a couple of megabytes
    #: rather than ten, while the quietest hour still carries about 40 calls —
    #: comfortably above the 30-call minimum a burn rule needs before it will
    #: reach a verdict. A corpus whose night-time hours fall under that minimum
    #: would demonstrate the guard rather than the rules.
    peak_calls_per_hour: int = 120
    start: datetime = DEFAULT_START
    models: tuple[ModelMix, ...] = ()
    incidents: tuple[Incident, ...] = ()
    #: Attribute values sprinkled onto spans, all bounded — the corpus should
    #: not itself contain the cardinality mistake this tool refuses.
    environments: tuple[str, ...] = ("prod",)
    routes: tuple[str, ...] = ("/chat", "/summarise", "/classify")
    seed: int = DEFAULT_SEED
    metadata: dict[str, str] = field(default_factory=dict)


DEFAULT_MODELS: tuple[ModelMix, ...] = (
    ModelMix(
        provider="openai",
        model="gpt-4o-mini",
        weight=0.40,
        median_latency_ms=750.0,
        mean_input_tokens=520,
        mean_output_tokens=180,
        cache_hit_rate=0.25,
    ),
    ModelMix(
        provider="openai",
        model="gpt-4o",
        weight=0.15,
        median_latency_ms=1650.0,
        latency_sigma=0.55,
        mean_input_tokens=900,
        mean_output_tokens=420,
        cache_hit_rate=0.15,
    ),
    ModelMix(
        provider="anthropic",
        model="claude-haiku",
        weight=0.20,
        median_latency_ms=620.0,
        mean_input_tokens=480,
        mean_output_tokens=160,
    ),
    ModelMix(
        provider="openai",
        model="text-embedding-3-small",
        weight=0.30,
        operation="embedding",
        median_latency_ms=90.0,
        latency_sigma=0.3,
        mean_input_tokens=340,
        mean_output_tokens=0,
    ),
)


def _diurnal(hour_of_day: float) -> float:
    """Traffic multiplier at a given hour, between roughly 0.3 and 1.0.

    A cosine trough at 04:00 and peak at 16:00. Not because real traffic is a
    cosine, but because a flat arrival rate would make the corpus useless for
    the thing it exists to demonstrate: a burn-rate rule's behaviour depends on
    how many calls land in its window, and a window with a constant rate never
    exercises the minimum-samples guard.
    """
    return 0.65 + 0.35 * math.cos((hour_of_day - 16.0) / 24.0 * 2.0 * math.pi)


def _log_normal(rng: random.Random, median: float, sigma: float) -> float:
    """Draw a latency sample. Median is the geometric mean of a log-normal."""
    return median * math.exp(rng.gauss(0.0, sigma))


def _pick(rng: random.Random, models: tuple[ModelMix, ...]) -> ModelMix:
    total = sum(model.weight for model in models)
    point = rng.random() * total
    running = 0.0
    for model in models:
        running += model.weight
        if point <= running:
            return model
    return models[-1]  # pragma: no cover - float arithmetic only


def _active(scenario: Scenario, at: datetime, model: str) -> Incident | None:
    for incident in scenario.incidents:
        start = scenario.start + incident.starts_after
        if start <= at < start + incident.duration and incident.model in {"", model}:
            return incident
    return None


def generate(scenario: Scenario) -> Window:
    """Produce the window *scenario* describes.

    Deterministic under ``scenario.seed``: the same scenario gives the same
    spans, byte for byte, on any machine.
    """
    models = scenario.models or DEFAULT_MODELS
    # nosec B311 - a seeded PRNG is the point. This generates fixtures, not
    # keys, nonces or anything a secret depends on.
    rng = random.Random(scenario.seed)  # noqa: S311  # nosec B311
    spans: list[Span] = []
    end = scenario.start + scenario.duration

    hours = int(scenario.duration.total_seconds() // 3600)
    for hour in range(hours):
        hour_start = scenario.start + timedelta(hours=hour)
        rate = scenario.peak_calls_per_hour * _diurnal(hour_start.hour + hour_start.minute / 60.0)
        # Poisson-ish: a rounded rate with jitter rather than a true Poisson
        # draw. The distribution of *counts* is not what any objective reads;
        # the count itself is.
        count = max(0, int(rate + rng.gauss(0.0, rate * 0.08)))
        for index in range(count):
            offset = timedelta(seconds=rng.random() * 3600.0)
            at = hour_start + offset
            if at >= end:
                continue
            spans.append(
                _span(
                    scenario,
                    rng=rng,
                    at=at,
                    mix=_pick(rng, models),
                    ordinal=hour * 100_000 + index,
                )
            )

    return build_window(
        spans,
        start=scenario.start,
        end=end,
        source=f"<synth:{scenario.name}>",
        metadata={
            "generated": "true",
            "scenario": scenario.name,
            "seed": str(scenario.seed),
            "note": "Synthesised telemetry. Nothing here was captured from a real service.",
            **scenario.metadata,
        },
    )


def _span(
    scenario: Scenario,
    *,
    rng: random.Random,
    at: datetime,
    mix: ModelMix,
    ordinal: int,
) -> Span:
    incident = _active(scenario, at, mix.model)

    failure_rate = incident.failure_rate if incident else mix.base_failure_rate
    latency_multiplier = incident.latency_multiplier if incident else 1.0
    token_multiplier = incident.token_multiplier if incident else 1.0
    input_multiplier = incident.input_multiplier if incident else 1.0

    outcome: Outcome = "ok"
    error_type = ""
    if rng.random() < failure_rate:
        outcome = "error"
        error_type = incident.error_type if incident else "provider_error"
    elif mix.operation == "chat" and rng.random() < REFUSAL_RATE:
        # A safety refusal. Rare, and deliberately present in the corpus: it is
        # the case that distinguishes an availability objective which counts
        # refusals as outages from one that does not.
        outcome = "refusal"

    duration = _log_normal(rng, mix.median_latency_ms, mix.latency_sigma) * latency_multiplier
    if outcome == "error":
        # A failed call is usually fast, because it failed early. Giving errors
        # the same latency distribution as successes would let an availability
        # incident silently move the latency numbers too, and the two would stop
        # being independent measurements.
        duration *= 0.25

    input_mean = mix.mean_input_tokens * input_multiplier
    input_tokens = max(1, int(rng.gauss(input_mean, input_mean * 0.3)))
    output_mean = mix.mean_output_tokens * token_multiplier
    output_tokens = (
        0 if mix.mean_output_tokens == 0 else max(1, int(rng.gauss(output_mean, output_mean * 0.4)))
    )
    if outcome != "ok":
        output_tokens = 0
    cached = int(input_tokens * mix.cache_hit_rate) if rng.random() < CACHE_HIT_CHANCE else 0

    return Span(
        span_id=f"{scenario.name}-{ordinal:08d}",
        trace_id=f"t{ordinal:08d}",
        started_at=at,
        duration_ms=round(duration, 3),
        provider=mix.provider,
        model=mix.model,
        operation=mix.operation,
        outcome=outcome,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        error_type=error_type,
        attributes={
            "environment": rng.choice(scenario.environments),
            "route": rng.choice(scenario.routes),
        },
    )


#: The scenarios the repository ships. Each one exists to demonstrate something
#: a reader would otherwise have to take on trust.
SCENARIOS: dict[str, Scenario] = {
    # Three quiet days. The control: every objective holds, so a green report
    # is a fact about the tool and not an absence of data.
    "steady": Scenario(
        name="steady",
        duration=timedelta(days=3),
        models=DEFAULT_MODELS,
        metadata={"shows": "a healthy platform; every objective holds"},
    ),
    # A 40-minute upstream outage on one model, 22 hours in. Long enough and
    # sharp enough that the 1-hour rule fires and the 3-day rule does not.
    "outage": Scenario(
        name="outage",
        duration=timedelta(days=3),
        models=DEFAULT_MODELS,
        seed=DEFAULT_SEED + 1,
        incidents=(
            Incident(
                name="upstream-outage",
                starts_after=timedelta(hours=22),
                duration=timedelta(minutes=40),
                failure_rate=0.55,
                error_type="upstream_unavailable",
                model="gpt-4o-mini",
            ),
        ),
        metadata={"shows": "a sharp availability incident; the fast-burn rule fires"},
    ),
    # Latency doubles for six hours with no errors at all. Invisible to an
    # availability objective, which is the point.
    "slowdown": Scenario(
        name="slowdown",
        duration=timedelta(days=3),
        models=DEFAULT_MODELS,
        seed=DEFAULT_SEED + 2,
        incidents=(
            Incident(
                name="degraded-latency",
                starts_after=timedelta(hours=30),
                duration=timedelta(hours=6),
                latency_multiplier=4.5,
                model="gpt-4o-mini",
            ),
        ),
        metadata={"shows": "latency degradation with no errors; availability stays green"},
    ),
    # A prompt change triples output length on the expensive model. No errors,
    # no latency change to speak of, and the bill doubles.
    "cost-spike": Scenario(
        name="cost-spike",
        duration=timedelta(days=3),
        models=DEFAULT_MODELS,
        seed=DEFAULT_SEED + 3,
        incidents=(
            Incident(
                name="context-leak",
                starts_after=timedelta(hours=40),
                duration=timedelta(hours=20),
                input_multiplier=9.0,
                token_multiplier=2.5,
                # Every model, because the leak is in the agent framework that
                # assembles the context rather than in one model's prompt. It
                # is also what makes the scenario legible: an incident confined
                # to a model holding 15% of the traffic moves the total bill by
                # only two or three times, and at this volume that is inside
                # the hour-to-hour variation the diurnal curve already produces.
                model="",
            ),
        ),
        metadata={"shows": "a context leak: input tokens explode, nothing else moves"},
    ),
}
