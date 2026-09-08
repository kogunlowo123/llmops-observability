"""Refusing a metric that would explode a time-series database.

The failure this prevents is specific and expensive. A metric with labels
``{model, environment, tenant_id}`` looks harmless in a code review. It creates
one time series per distinct combination, and ``tenant_id`` is unbounded, so on
a busy day the count goes from four hundred to four hundred thousand — which
takes out the metrics backend for everybody, including the dashboards you would
use to work out what happened.

Two decisions make this guard useful rather than decorative.

**It fires at registration, not at emit.** By the time a bad label *value*
arrives, the metric exists, the code that emits it is deployed, and the series
are already being created. Refusing the metric when it is declared means the
failure happens in a unit test on a laptop. This is the same instinct as
refusing a network-capable exporter when a run is offline: check what a thing
*can* do, not what it happened to do.

**Unboundedness is a property of the label, declared once.** ``tenant_id`` is
unbounded everywhere it appears, and a guard that made each metric argue the
case separately would be a guard somebody talks their way past on a Friday. A
label is registered as bounded with an explicit cardinality, or bucketed into
one that is, or it cannot be a label at all.

Bucketing is the way out, and it is offered rather than merely demanded: a
latency in milliseconds is unbounded, ``le="500"`` is not, and a tenant id is
unbounded where ``tenant_tier="enterprise"`` is not. The guard names the bucketed
alternative in its remedy, because a rule that only says no gets disabled.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from llmops.errors import CardinalityError

#: The default ceiling on series per metric. Small on purpose: the number of
#: series a dashboard can usefully draw is in the hundreds, and a metric past
#: this is one nobody is reading anyway.
DEFAULT_SERIES_BUDGET = 1000

#: Labels this tool knows are unbounded, with the bucketed alternative to
#: suggest. Names that appear in the OpenTelemetry GenAI conventions and in
#: every LLM platform that has ever had this outage.
KNOWN_UNBOUNDED: dict[str, str] = {
    "user_id": "user_tier",
    "tenant_id": "tenant_tier",
    "session_id": "session_kind",
    "request_id": "route",
    "trace_id": "route",
    "span_id": "route",
    "prompt": "prompt_template",
    "prompt_hash": "prompt_template",
    "completion": "",
    "input": "",
    "output": "",
    "email": "",
    "ip": "",
    "url": "route",
    "path": "route",
    "query": "route",
    "duration_ms": "le",
    "latency_ms": "le",
    "cost_usd": "cost_bucket",
    "timestamp": "",
}


@dataclass(frozen=True, slots=True)
class Label:
    """One dimension of a metric, and how many values it can take."""

    name: str
    #: How many distinct values this label can take. ``None`` means unbounded,
    #: which is the state that gets a metric refused.
    cardinality: int | None
    #: What to use instead, when unbounded. Empty when the answer is "nothing:
    #: this does not belong in a metric at all".
    bucketed_alternative: str = ""

    @property
    def bounded(self) -> bool:
        """Whether this label can be a metric dimension."""
        return self.cardinality is not None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "name": self.name,
            "cardinality": self.cardinality,
            "bounded": self.bounded,
            "bucketed_alternative": self.bucketed_alternative,
        }


def unbounded(name: str) -> Label:
    """Return a label whose value space is unbounded."""
    return Label(name=name, cardinality=None, bucketed_alternative=KNOWN_UNBOUNDED.get(name, ""))


def bounded(name: str, cardinality: int) -> Label:
    """Return a label with a known, finite value space."""
    if cardinality < 1:
        raise CardinalityError(
            f"label {name!r} was registered with cardinality {cardinality}.",
            remedy="A label takes at least one value. Use `unbounded` if it is unbounded.",
        )
    return Label(name=name, cardinality=cardinality)


#: The labels this tool emits itself, with realistic ceilings. A platform
#: registers its own on top.
DEFAULT_LABELS: tuple[Label, ...] = (
    bounded("provider", 20),
    bounded("model", 200),
    bounded("operation", 4),
    bounded("outcome", 5),
    bounded("environment", 8),
    bounded("service", 50),
    bounded("route", 200),
    bounded("le", 16),
)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """A metric that passed the guard, with the series count it can produce."""

    name: str
    labels: tuple[str, ...]
    series: int

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {"name": self.name, "labels": list(self.labels), "max_series": self.series}


class LabelRegistry:
    """Knows which labels are bounded, and refuses metrics that are not.

    Held deliberately separate from the metric registry: a label's cardinality
    is a property of the data, decided once, and not something each metric
    negotiates for itself.
    """

    def __init__(self, labels: Iterable[Label] = DEFAULT_LABELS) -> None:
        self._labels: dict[str, Label] = {}
        for label in labels:
            self.register(label)

    def __contains__(self, name: str) -> bool:
        return name in self._labels

    def __len__(self) -> int:
        return len(self._labels)

    def register(self, label: Label) -> None:
        """Record a label's cardinality.

        Re-registering the same name with a different cardinality is refused: it
        is how two parts of a codebase come to disagree about whether a label is
        safe, and the optimistic one always wins by accident.
        """
        existing = self._labels.get(label.name)
        if existing is not None and existing != label:
            raise CardinalityError(
                f"label {label.name!r} is already registered with a different cardinality.",
                remedy=(
                    f"It is registered as {_describe(existing)} and you are registering "
                    f"{_describe(label)}. Two answers to this question means the "
                    "optimistic one wins by accident."
                ),
            )
        self._labels[label.name] = label

    def describe(self, name: str) -> Label:
        """Return what is known about *name*.

        An unregistered label is treated as unbounded. Failing closed is the
        only defensible default: the cost of wrongly refusing a bounded label is
        one line registering it, and the cost of wrongly accepting an unbounded
        one is an outage in the metrics backend.
        """
        known = self._labels.get(name)
        if known is not None:
            return known
        return unbounded(name)

    def check(
        self,
        metric: str,
        labels: Sequence[str],
        *,
        series_budget: int = DEFAULT_SERIES_BUDGET,
    ) -> MetricSpec:
        """Refuse *metric* unless its labels are bounded and small enough.

        Two separate failures, reported separately because they have different
        fixes: an unbounded label has to be bucketed or dropped, while a
        combination that is merely too large is usually one label too many.
        """
        duplicates = sorted({name for name in labels if list(labels).count(name) > 1})
        if duplicates:
            raise CardinalityError(
                f"metric {metric!r} repeats label(s): {', '.join(duplicates)}.",
                remedy="A repeated label is usually a copy-paste of the wrong dimension.",
            )

        described = [self.describe(name) for name in labels]
        loose = [label for label in described if not label.bounded]
        if loose:
            raise CardinalityError(
                f"metric {metric!r} has unbounded label(s): "
                f"{', '.join(label.name for label in loose)}.",
                remedy=_remedy(loose),
            )

        series = 1
        for label in described:
            # Narrowed by `loose` above; mypy cannot see that, and an assert
            # would vanish under -O.
            series *= label.cardinality if label.cardinality is not None else 1
        if series > series_budget:
            biggest = max(described, key=lambda label: label.cardinality or 0, default=None)
            raise CardinalityError(
                f"metric {metric!r} can produce {series} series; the budget is {series_budget}.",
                remedy=(
                    "Drop a dimension or narrow one."
                    + (
                        f" The largest is {biggest.name!r} at {biggest.cardinality}."
                        if biggest is not None
                        else ""
                    )
                ),
            )

        return MetricSpec(name=metric, labels=tuple(labels), series=series)


def _describe(label: Label) -> str:
    return "unbounded" if label.cardinality is None else f"bounded at {label.cardinality}"


def _remedy(loose: list[Label]) -> str:
    """Name the bucketed alternative, so the guard is usable rather than a wall."""
    parts: list[str] = []
    for label in loose:
        if label.bucketed_alternative:
            parts.append(f"{label.name!r} -> bucket it as {label.bucketed_alternative!r}")
        elif label.name in KNOWN_UNBOUNDED:
            parts.append(f"{label.name!r} is content, not a dimension; it cannot be a label")
        else:
            parts.append(
                f"{label.name!r} is unknown, so it is assumed unbounded; register it with "
                "`bounded(name, cardinality)` if it is not"
            )
    return "; ".join(parts) + "."
