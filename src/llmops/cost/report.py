"""Costing a window, and checking the answer against the provider's own.

Two things happen here. The first is arithmetic: every span priced, grouped by
whatever dimension the caller asked for. The second is the part that makes the
first trustworthy — **reconciliation**.

A computed cost is a model of a bill. Models drift: a vendor changes a rate, a
provider starts counting cached tokens differently, a batch endpoint applies a
discount nobody put in the price book. When a span carries the provider's own
``reported_cost_usd``, the two numbers can be compared, and a systematic
divergence is a finding rather than a surprise at the end of the month.

The tolerance is expressed **both** as a fraction and as an absolute floor. A
fraction alone makes every sub-cent call a discrepancy, because rounding at the
fourth decimal place is a large *relative* error on a number that small; an
absolute floor alone lets a 20% error on a large bill pass. Requiring both to be
exceeded is the only version that survives a real window.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from llmops.cost.pricebook import PriceBook
from llmops.telemetry.span import Span
from llmops.telemetry.window import Window

#: How a spend report is broken down.
Dimension = Literal["model", "provider", "operation", "outcome"]

#: Relative and absolute tolerances for reconciliation. Both must be exceeded
#: before a span is called discrepant — see the module docstring.
DEFAULT_RELATIVE_TOLERANCE = Decimal("0.01")
DEFAULT_ABSOLUTE_TOLERANCE_USD = Decimal("0.000005")

#: Reports are rendered to eight decimal places. A single embedding call can
#: cost less than a millionth of a dollar, and rounding it to cents in the
#: *report* would make a per-call table read as a column of zeros.
QUANTUM = Decimal("0.00000001")


def _key(dimension: Dimension) -> Callable[[Span], str]:
    return {
        "model": lambda span: f"{span.provider}/{span.model}",
        "provider": lambda span: span.provider,
        "operation": lambda span: span.operation,
        "outcome": lambda span: span.outcome,
    }[dimension]


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """One span whose computed cost disagrees with the provider's."""

    span_id: str
    model: str
    computed_usd: Decimal
    reported_usd: Decimal

    @property
    def difference_usd(self) -> Decimal:
        """Computed minus reported. Positive means this tool overestimated."""
        return self.computed_usd - self.reported_usd

    @property
    def relative(self) -> Decimal:
        """The difference as a fraction of what the provider reported."""
        if self.reported_usd == 0:
            # A provider reporting zero for a call that cost something is the
            # most interesting case there is, and dividing by it would hide it.
            return Decimal(1) if self.computed_usd else Decimal(0)
        return abs(self.difference_usd) / self.reported_usd

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "span_id": self.span_id,
            "model": self.model,
            "computed_usd": _round(self.computed_usd),
            "reported_usd": _round(self.reported_usd),
            "difference_usd": _round(self.difference_usd),
            "relative": float(round(self.relative, 6)),
        }


@dataclass(frozen=True, slots=True)
class CostLine:
    """One row of a spend report."""

    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "key": self.key,
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": _round(self.cost_usd),
        }


@dataclass(frozen=True, slots=True)
class CostReport:
    """What a window cost, and whether the number can be trusted."""

    dimension: Dimension
    lines: tuple[CostLine, ...]
    total_usd: Decimal
    calls: int
    pricebook_digest: str
    window_digest: str
    #: Spans whose computed cost disagreed with the provider's.
    discrepancies: tuple[Discrepancy, ...] = ()
    #: How many spans carried a provider-reported cost at all. Reported because
    #: "no discrepancies" over zero comparisons is not evidence of anything.
    reconciled: int = 0
    reported_total_usd: Decimal = Decimal(0)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "dimension": self.dimension,
            "calls": self.calls,
            "total_usd": _round(self.total_usd),
            "pricebook_digest": self.pricebook_digest,
            "window_digest": self.window_digest,
            "lines": [line.as_dict() for line in self.lines],
            "reconciliation": {
                "compared": self.reconciled,
                "reported_total_usd": _round(self.reported_total_usd),
                "discrepancies": [item.as_dict() for item in self.discrepancies],
            },
            "metadata": dict(self.metadata),
        }

    def summary(self) -> str:
        """One line, for a terminal."""
        parts = [f"${_round(self.total_usd)} over {self.calls} call(s)"]
        if self.reconciled:
            parts.append(f"{self.reconciled} reconciled, {len(self.discrepancies)} discrepant")
        else:
            # Said out loud. "0 discrepancies" over 0 comparisons reads as a
            # clean bill of health and is nothing of the kind.
            parts.append("no provider-reported costs to reconcile against")
        return ", ".join(parts)


def cost_window(
    window: Window,
    pricebook: PriceBook,
    *,
    dimension: Dimension = "model",
    relative_tolerance: Decimal = DEFAULT_RELATIVE_TOLERANCE,
    absolute_tolerance_usd: Decimal = DEFAULT_ABSOLUTE_TOLERANCE_USD,
) -> CostReport:
    """Price every span in *window* and reconcile against reported costs.

    Raises :class:`~llmops.errors.UnpricedModelError` on the first model the
    price book does not cover. See that class for why this is not a zero.
    """
    key_of = _key(dimension)
    grouped: dict[str, list[Span]] = {}
    total = Decimal(0)
    reported_total = Decimal(0)
    reconciled = 0
    discrepancies: list[Discrepancy] = []

    for span in window.spans:
        computed = pricebook.cost(span)
        total += computed
        grouped.setdefault(key_of(span), []).append(span)

        if span.reported_cost_usd is None:
            continue
        reported = Decimal(str(span.reported_cost_usd))
        reported_total += reported
        reconciled += 1
        difference = abs(computed - reported)
        relative = difference / reported if reported else (Decimal(1) if computed else Decimal(0))
        # Both tolerances, not either. A fraction alone flags every sub-cent
        # call; an absolute floor alone lets a large bill drift by 20%.
        if difference > absolute_tolerance_usd and relative > relative_tolerance:
            discrepancies.append(
                Discrepancy(
                    span_id=span.span_id,
                    model=f"{span.provider}/{span.model}",
                    computed_usd=computed,
                    reported_usd=reported,
                )
            )

    lines = tuple(
        CostLine(
            key=key,
            calls=len(spans),
            input_tokens=sum(span.input_tokens for span in spans),
            output_tokens=sum(span.output_tokens for span in spans),
            cost_usd=sum((pricebook.cost(span) for span in spans), Decimal(0)),
        )
        for key, spans in sorted(grouped.items())
    )

    return CostReport(
        dimension=dimension,
        lines=lines,
        total_usd=total,
        calls=window.total,
        pricebook_digest=pricebook.digest(),
        window_digest=window.digest(),
        discrepancies=tuple(discrepancies),
        reconciled=reconciled,
        reported_total_usd=reported_total,
        metadata={"source": window.source, "pricebook": pricebook.source},
    )


def unpriced_models(window: Window, pricebook: PriceBook) -> tuple[str, ...]:
    """Every (provider, model) in *window* the price book does not cover.

    Exists so a caller can report all of them at once. Raising on the first is
    right for the costing path and wrong for a diagnostic, where being told
    about one missing model at a time is four round trips instead of one.
    """
    missing: list[str] = []
    for provider, model in window.models():
        probe = next(
            span for span in window.spans if span.provider == provider and span.model == model
        )
        try:
            pricebook.price_for(probe)
        except Exception:  # noqa: BLE001 - any failure to price is "missing" here
            missing.append(f"{provider}/{model}")
    return tuple(missing)


def _round(value: Decimal) -> float:
    """Quantise for a JSON report."""
    return float(value.quantize(QUANTUM, rounding=ROUND_HALF_UP))


def total_cost(spans: Iterable[Span], pricebook: PriceBook) -> Decimal:
    """Sum the cost of *spans*. Used by the budget objective."""
    return sum((pricebook.cost(span) for span in spans), Decimal(0))
