"""Multi-window, multi-burn-rate alerting.

The problem this solves is that a single-threshold alert on an error rate is
either deaf or hysterical. Alert on "error rate above 0.1% for five minutes" and
a two-minute total outage goes unnoticed while a harmless blip pages someone at
04:00. The fix, from the Google SRE workbook, is to alert on how fast the
**error budget** is being consumed, and to require two windows to agree.

Definitions, precisely, because everything below is arithmetic on them:

* An objective states a target, say 99.9% of calls succeed over a 30-day period.
  The **error budget** is what is left over: 0.1% of calls may fail.
* The **burn rate** over a window is the observed failure rate divided by the
  budget. At a burn rate of 1 the budget is consumed exactly over the period; at
  14.4 it is consumed 14.4 times faster, so one hour at that rate spends
  ``14.4 / 720 = 2%`` of a 30-day budget.
* A **long window** decides whether the burn is real. A **short window** — one
  twelfth of the long one, by convention — decides whether it is *still
  happening*, so an alert resets soon after the incident ends instead of
  smouldering for the length of the long window.

Both windows must exceed the threshold for a rule to fire. That conjunction is
the whole design: the long window gives precision, the short one gives reset
time, and neither alone gives both.

The four thresholds below are the conventional table, not values invented here.
Detection times follow from them: at a 2% budget spend the fast rule fires
within minutes, and the slow rule catches a long, shallow burn that no
short-window rule would ever see.

+----------+-------------+--------------+-----------+-------------------+
| Severity | Long window | Short window | Burn rate | Budget at trigger |
+==========+=============+==============+===========+===================+
| page     | 1 hour      | 5 minutes    | 14.4      | 2%                |
| page     | 6 hours     | 30 minutes   | 6         | 5%                |
| ticket   | 1 day       | 2 hours      | 3         | 10%               |
| ticket   | 3 days      | 6 hours      | 1         | 10%               |
+----------+-------------+--------------+-----------+-------------------+

**A window with too few calls does not fire.** At ten calls, one failure is a
10% error rate and a burn rate of 100 against a 99.9% objective — and it is
noise. Rules carry a minimum sample count, and a rule that is under it is
reported as *not evaluated* rather than as passing, because "we did not look" and
"we looked and it was fine" are different facts and a dashboard that conflates
them is lying by omission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

#: How much shorter the short window is than the long one. The workbook uses a
#: twelfth throughout, which puts the reset time at 1/12 of the detection time.
SHORT_WINDOW_FRACTION = 12

#: Below this many calls in the long window, a rule is not evaluated. Ten
#: failures out of ten calls is not an outage, it is a quiet Sunday.
DEFAULT_MIN_SAMPLES = 30

Severity = Literal["page", "ticket"]


@dataclass(frozen=True, slots=True)
class BurnRule:
    """One (long window, short window, threshold) triple."""

    name: str
    severity: Severity
    long_window: timedelta
    burn_rate: float
    #: Defaults to a twelfth of the long window. Overridable, because an
    #: objective over an hour cannot have a five-minute long window and a
    #: twenty-five-second short one.
    short_window: timedelta | None = None
    min_samples: int = DEFAULT_MIN_SAMPLES

    @property
    def short(self) -> timedelta:
        """The short window, derived when not given."""
        if self.short_window is not None:
            return self.short_window
        return self.long_window / SHORT_WINDOW_FRACTION

    def budget_consumed(self, period: timedelta) -> float:
        """Return what fraction of a *period*'s budget this trigger represents.

        The number in the table's last column, computed rather than quoted, so
        a rule with a non-standard window still reports something meaningful.
        """
        if period <= timedelta(0):  # pragma: no cover - objectives validate this
            return 0.0
        return self.burn_rate * (self.long_window / period)


#: The conventional four-rule table. See the module docstring.
STANDARD_RULES: tuple[BurnRule, ...] = (
    BurnRule(
        name="fast-burn",
        severity="page",
        long_window=timedelta(hours=1),
        short_window=timedelta(minutes=5),
        burn_rate=14.4,
    ),
    BurnRule(
        name="moderate-burn",
        severity="page",
        long_window=timedelta(hours=6),
        short_window=timedelta(minutes=30),
        burn_rate=6.0,
    ),
    BurnRule(
        name="slow-burn",
        severity="ticket",
        long_window=timedelta(days=1),
        short_window=timedelta(hours=2),
        burn_rate=3.0,
    ),
    BurnRule(
        name="creeping-burn",
        severity="ticket",
        long_window=timedelta(days=3),
        short_window=timedelta(hours=6),
        burn_rate=1.0,
    ),
)


#: What a window's budget is denominated in.
Unit = Literal["calls", "usd"]


@dataclass(frozen=True, slots=True)
class WindowMeasurement:
    """What one window of one rule measured.

    ``burn`` is stored rather than derived, because the two kinds of objective
    arrive at it differently — failed calls over an error budget, or dollars
    over a pro-rata share of a spend budget — and the alternative is a second
    copy of the two-window conjunction that could drift from this one. Both
    constructors below produce the same meaning: 1.0 consumes the budget exactly
    over the period.

    ``samples`` is always a count of *calls*, in both cases, so the
    minimum-samples guard keeps its meaning for spend: three expensive calls in
    an hour is not yet a spending problem.
    """

    length: timedelta
    samples: int
    burn: float
    #: Bad events, for an event objective. Dollars, for a spend objective.
    bad: float = 0.0
    unit: Unit = "calls"

    @classmethod
    def from_events(
        cls, *, length: timedelta, samples: int, bad: int, budget: float
    ) -> WindowMeasurement:
        """Measure failed calls against an error budget."""
        rate = bad / samples if samples else 0.0
        # A 100% objective has no budget, so any failure at all has consumed all
        # of it. Objectives refuse a target of 1.0 at parse time; this keeps the
        # arithmetic honest for a caller who builds one directly.
        burn = (float("inf") if bad else 0.0) if budget <= 0.0 else rate / budget
        return cls(length=length, samples=samples, burn=burn, bad=float(bad), unit="calls")

    @classmethod
    def from_spend(
        cls, *, length: timedelta, samples: int, spent_usd: float, pro_rata_usd: float
    ) -> WindowMeasurement:
        """Measure dollars against this window's pro-rata share of a budget."""
        if pro_rata_usd <= 0.0:
            burn = float("inf") if spent_usd else 0.0
        else:
            burn = spent_usd / pro_rata_usd
        return cls(length=length, samples=samples, burn=burn, bad=spent_usd, unit="usd")

    @property
    def failure_rate(self) -> float:
        """The observed fraction of bad events. Meaningful for call units only."""
        return self.bad / self.samples if self.samples and self.unit == "calls" else 0.0

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        payload: dict[str, Any] = {
            "length_s": self.length.total_seconds(),
            "calls": self.samples,
            "burn_rate": _finite(self.burn),
            "unit": self.unit,
        }
        if self.unit == "calls":
            payload["bad"] = int(self.bad)
            payload["failure_rate"] = round(self.failure_rate, 8)
        else:
            payload["spent_usd"] = round(self.bad, 8)
        return payload


@dataclass(frozen=True, slots=True)
class RuleResult:
    """Whether one rule fired, and what it saw."""

    rule: BurnRule
    long: WindowMeasurement
    short: WindowMeasurement
    #: True when both windows exceeded the threshold.
    fired: bool
    #: True when the long window had too few calls to say anything.
    under_sampled: bool

    @property
    def evaluated(self) -> bool:
        """Whether this rule reached a verdict at all."""
        return not self.under_sampled

    def summary(self) -> str:
        """One line, for a terminal."""
        if self.under_sampled:
            return (
                f"{self.rule.name}: not evaluated — {self.long.samples} call(s) in "
                f"{_human(self.rule.long_window)}, below the minimum of {self.rule.min_samples}"
            )
        verdict = "FIRING" if self.fired else "ok"
        return (
            f"{self.rule.name}: {verdict} — burn {_finite(self.long.burn):.2f}x "
            f"over {_human(self.rule.long_window)} and "
            f"{_finite(self.short.burn):.2f}x over "
            f"{_human(self.rule.short)}, threshold {self.rule.burn_rate:g}x"
        )

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {
            "rule": self.rule.name,
            "severity": self.rule.severity,
            "threshold_burn_rate": self.rule.burn_rate,
            "fired": self.fired,
            "evaluated": self.evaluated,
            "long_window": self.long.as_dict(),
            "short_window": self.short.as_dict(),
        }


#: Relative tolerance on the threshold comparison.
#:
#: A burn rate is a ratio of integers divided by a budget, and at the exact
#: documented trigger that arithmetic lands a hair below it: 1440 failures in
#: 100,000 calls against a 0.001 budget is 14.399999999999999, not 14.4, so a
#: bare ``>=`` does not fire at the very point the table says it fires. The
#: error is invisible — the rule fires a few calls later — but the documented
#: claim would be false, and a threshold nobody can hit exactly is a threshold
#: that cannot be tested exactly either.
THRESHOLD_TOLERANCE = 1e-9


def _at_or_above(value: float, threshold: float) -> bool:
    """Whether *value* has reached *threshold*, allowing for float error."""
    return value >= threshold or math.isclose(value, threshold, rel_tol=THRESHOLD_TOLERANCE)


def decide(rule: BurnRule, long: WindowMeasurement, short: WindowMeasurement) -> RuleResult:
    """Apply the two-window conjunction to two measurements.

    The one place the rule's actual decision is made, for either kind of
    objective. Both windows must reach the threshold: the long one for
    precision, the short one so the alert resets soon after the incident ends.
    """
    under_sampled = long.samples < rule.min_samples
    fired = (
        not under_sampled
        and _at_or_above(long.burn, rule.burn_rate)
        and _at_or_above(short.burn, rule.burn_rate)
    )
    return RuleResult(rule=rule, long=long, short=short, fired=fired, under_sampled=under_sampled)


def evaluate_rule(  # noqa: PLR0913 - two windows means two counts and a budget
    rule: BurnRule,
    *,
    budget: float,
    long_samples: int,
    long_bad: int,
    short_samples: int,
    short_bad: int,
) -> RuleResult:
    """Apply one rule to two already-counted windows of events.

    Kept free of any window or span type on purpose: this is the arithmetic, it
    is the part most worth testing directly, and a test of it should not have to
    build telemetry to ask what 14.4 times a budget is.
    """
    return decide(
        rule,
        WindowMeasurement.from_events(
            length=rule.long_window, samples=long_samples, bad=long_bad, budget=budget
        ),
        WindowMeasurement.from_events(
            length=rule.short, samples=short_samples, bad=short_bad, budget=budget
        ),
    )


def detection_time(rule: BurnRule, *, budget: float, failure_rate: float) -> timedelta | None:
    """How long *rule* takes to fire at a constant *failure_rate*.

    Returns ``None`` when the rule never fires at that rate. Used by the
    documentation and by a test that pins the conventional table's behaviour:
    a claim about detection time that nothing computes is a claim nobody checks.
    """
    if budget <= 0.0 or failure_rate <= 0.0:
        return None
    burn = failure_rate / budget
    if burn < rule.burn_rate:
        return None
    # The long window is a sliding average, so it crosses the threshold once the
    # incident has filled `threshold / burn` of it.
    return rule.long_window * (rule.burn_rate / burn)


def _finite(value: float) -> float:
    """Replace an infinity with a large finite number for JSON.

    ``json.dumps`` writes ``Infinity``, which is not valid JSON and which half
    the parsers downstream of a CI job will reject. The substitute is far above
    any real burn rate and is documented in the report's own schema.
    """
    return 1e9 if value == float("inf") else value


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def windows_for(rule: BurnRule, *, now: datetime) -> tuple[tuple[datetime, datetime], ...]:
    """Return the two intervals *rule* evaluates, ending at *now*.

    Both end at the same instant. A short window offset from the long one would
    measure a different incident.
    """
    return (
        (now - rule.long_window, now),
        (now - rule.short, now),
    )
