"""Applying objectives to a window.

This is where the two halves meet: the arithmetic in ``burnrate`` knows nothing
about spans, and the declarations in ``objective`` know nothing about time
series. Here the spans are counted into the windows each rule asks for, and the
results are assembled into findings a build can act on.

Two decisions worth stating.

**Everything is evaluated at one instant.** ``now`` defaults to the window's end
and is passed down to every rule, so a long rule and a short rule measure two
intervals ending at the same moment. Taking the clock separately per rule would
introduce a skew that is invisible in a test and real in production.

**A rule whose long window extends before the data does not fire.** A 3-day rule
over a 6-hour window is being asked a question the data cannot answer, and
reporting it as ``ok`` would be the tool asserting a fact it does not have. It
is reported as not evaluated, with the reason, exactly like an under-sampled
rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from llmops.cost.pricebook import PriceBook
from llmops.errors import ObjectiveError
from llmops.slo.burnrate import BurnRule, RuleResult, WindowMeasurement, decide, evaluate_rule
from llmops.slo.objective import Objective, ObjectiveSet
from llmops.telemetry.span import Span
from llmops.telemetry.window import Window


@dataclass(frozen=True, slots=True)
class ObjectiveResult:
    """What one objective made of one window."""

    objective: Objective
    results: tuple[RuleResult, ...]
    #: Spans in scope for this objective, across the whole window.
    in_scope: int
    #: Rules skipped because the window is shorter than their long window,
    #: as (rule name, reason).
    not_covered: tuple[tuple[str, str], ...] = ()

    @property
    def firing(self) -> tuple[RuleResult, ...]:
        """Every rule that fired."""
        return tuple(result for result in self.results if result.fired)

    @property
    def breached(self) -> bool:
        """Whether any rule fired."""
        return bool(self.firing)

    @property
    def worst_severity(self) -> str:
        """The severity of the most serious firing rule."""
        if any(result.rule.severity == "page" for result in self.firing):
            return "page"
        return "ticket" if self.firing else "none"

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "objective": self.objective.as_dict(),
            "digest": self.objective.digest(),
            "in_scope": self.in_scope,
            "breached": self.breached,
            "severity": self.worst_severity,
            "rules": [result.as_dict() for result in self.results],
            "not_covered": [{"rule": name, "reason": reason} for name, reason in self.not_covered],
        }


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Every objective, against one window."""

    window_digest: str
    objectives_digest: str
    evaluated_at: datetime
    results: tuple[ObjectiveResult, ...]
    #: Lines the window reader could not parse. Carried through to the report,
    #: because a verdict computed over 97% of the evidence should say so.
    unreadable: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def breached(self) -> tuple[ObjectiveResult, ...]:
        """Every objective with a firing rule."""
        return tuple(result for result in self.results if result.breached)

    @property
    def passed(self) -> bool:
        """Whether every objective held."""
        return not self.breached

    def summary(self) -> str:
        """One line, for a terminal."""
        verdict = "HELD" if self.passed else "BREACHED"
        return (
            f"{verdict}  {len(self.results) - len(self.breached)}/{len(self.results)} "
            f"objective(s) held"
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "passed": self.passed,
            "evaluated_at": self.evaluated_at.isoformat(),
            "window_digest": self.window_digest,
            "objectives_digest": self.objectives_digest,
            "unreadable_lines": self.unreadable,
            "objectives": [result.as_dict() for result in self.results],
            "metadata": dict(self.metadata),
        }


def evaluate(
    window: Window,
    objectives: ObjectiveSet,
    *,
    pricebook: PriceBook | None = None,
    now: datetime | None = None,
) -> Evaluation:
    """Apply every objective in *objectives* to *window*."""
    instant = now or window.end
    results = tuple(
        _one(objective, window, pricebook=pricebook, now=instant)
        for objective in objectives.objectives
    )
    return Evaluation(
        window_digest=window.digest(),
        objectives_digest=objectives.digest(),
        evaluated_at=instant,
        results=results,
        unreadable=len(window.unreadable),
        metadata={"window": window.source, "objectives": objectives.source},
    )


def _one(
    objective: Objective,
    window: Window,
    *,
    pricebook: PriceBook | None,
    now: datetime,
) -> ObjectiveResult:
    scoped = tuple(span for span in window.spans if objective.selector.matches(span))

    if objective.kind == "spend" and pricebook is None:
        raise ObjectiveError(
            f"objective {objective.name!r} measures spend and no price book was given.",
            remedy="Pass --pricebook. A spend objective cannot be checked without rates.",
        )

    results: list[RuleResult] = []
    not_covered: list[tuple[str, str]] = []

    for rule in objective.rules:
        if now - rule.long_window < window.start:
            not_covered.append(
                (
                    rule.name,
                    (
                        f"the rule looks back {_human(rule.long_window)} and only "
                        f"{_human(now - window.start)} of history precedes "
                        f"{now.isoformat()}"
                    ),
                )
            )
            continue
        results.append(_apply(objective, rule, scoped, pricebook=pricebook, now=now))

    return ObjectiveResult(
        objective=objective,
        results=tuple(results),
        in_scope=len(scoped),
        not_covered=tuple(not_covered),
    )


def _apply(
    objective: Objective,
    rule: BurnRule,
    spans: tuple[Span, ...],
    *,
    pricebook: PriceBook | None,
    now: datetime,
) -> RuleResult:
    long_spans = _between(spans, now - rule.long_window, now)
    short_spans = _between(spans, now - rule.short, now)

    if objective.kind == "spend":
        if pricebook is None:  # pragma: no cover - `_one` refuses before we get here
            raise ObjectiveError(
                f"objective {objective.name!r} measures spend and no price book was given."
            )
        return decide(
            rule,
            _spend_measurement(objective, rule.long_window, long_spans, pricebook),
            _spend_measurement(objective, rule.short, short_spans, pricebook),
        )

    return evaluate_rule(
        rule,
        budget=objective.error_budget,
        long_samples=len(long_spans),
        long_bad=sum(1 for span in long_spans if objective.is_bad(span)),
        short_samples=len(short_spans),
        short_bad=sum(1 for span in short_spans if objective.is_bad(span)),
    )


def _spend_measurement(
    objective: Objective,
    length: timedelta,
    spans: tuple[Span, ...],
    pricebook: PriceBook,
) -> WindowMeasurement:
    """Dollars spent in a window against that window's share of the budget.

    This is what makes spend and availability one engine rather than two. A
    window covering a tenth of the period is entitled to a tenth of the budget;
    spending exactly that burns at 1.0, which is the same meaning the
    availability rules give the number, so the same thresholds apply unchanged.
    """
    spent = sum((pricebook.cost(span) for span in spans), Decimal(0))
    share = Decimal(str(length.total_seconds() / objective.period.total_seconds()))
    return WindowMeasurement.from_spend(
        length=length,
        samples=len(spans),
        spent_usd=float(spent),
        pro_rata_usd=float(objective.budget_usd * share),
    )


def _between(spans: tuple[Span, ...], start: datetime, end: datetime) -> tuple[Span, ...]:
    return tuple(span for span in spans if start <= span.started_at < end)


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds / size:g}{unit}"
    return f"{seconds}s"
