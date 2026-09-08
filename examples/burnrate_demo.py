#!/usr/bin/env python
"""Why two windows, and why these four thresholds.

The argument for burn-rate alerting, in about forty lines of output. Every
number below is computed rather than quoted, so it is checkable.

    python examples/burnrate_demo.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llmops.slo.burnrate import STANDARD_RULES, detection_time, evaluate_rule

# A 99.9% objective over 30 days: 0.1% of calls may fail.
TARGET = 0.999
BUDGET = 1.0 - TARGET
PERIOD = timedelta(days=30)

FAST = STANDARD_RULES[0]


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def main() -> int:
    print(f"Objective: {TARGET:.1%} of calls succeed over {PERIOD.days} days.")
    print(f"Error budget: {BUDGET:.1%} of calls may fail.")

    rule("The conventional table, and what each row means")
    print(f"{'rule':<16}{'long':>7}{'short':>8}{'burn':>7}{'budget at trigger':>20}")
    for item in STANDARD_RULES:
        print(
            f"{item.name:<16}"
            f"{_short(item.long_window):>7}"
            f"{_short(item.short):>8}"
            f"{item.burn_rate:>6g}x"
            f"{item.budget_consumed(PERIOD):>19.0%}"
        )
    print(
        "\nThe last column is the point: 'fast-burn' fires once 2% of a month's\n"
        "budget is gone, which is a number a person can reason about. '14.4' is\n"
        "not; it is what 2% works out to over an hour."
    )

    rule("How fast each rule notices a given failure rate")
    print(f"{'failure rate':<16}{'burn':>8}   what happens")
    for failure_rate, description in [
        (0.0005, "half the budget rate — healthy"),
        (0.0007, "a blip"),
        (0.0144, "the fast rule's trigger"),
        (0.05, "5% of calls failing"),
        (1.0, "a total outage"),
    ]:
        detected = detection_time(FAST, budget=BUDGET, failure_rate=failure_rate)
        outcome = "never fires" if detected is None else f"fast-burn in {_short(detected)}"
        print(f"{failure_rate:<16.2%}{failure_rate / BUDGET:>7.1f}x   {outcome} ({description})")

    rule("Why both windows have to agree")
    print(f"{'long window':<14}{'short window':<14}{'fires?':<8}why")
    for long_rate, short_rate, why in [
        (0.02, 0.02, "an incident that is still happening"),
        (0.02, 0.0, "the same incident, ten minutes after it ended"),
        (0.0, 0.5, "a sixty-second blip, right now"),
        (0.0005, 0.0005, "a healthy service"),
    ]:
        fired = evaluate_rule(
            FAST,
            budget=BUDGET,
            long_samples=10_000,
            long_bad=round(10_000 * long_rate),
            short_samples=10_000,
            short_bad=round(10_000 * short_rate),
        ).fired
        print(f"{long_rate:<14.2%}{short_rate:<14.2%}{'yes' if fired else 'no':<8}{why}")
    print(
        "\nRow two is the reason for the short window: the alert clears soon after\n"
        "the incident does, instead of smouldering for another hour. Row three is\n"
        "the reason for the long one."
    )

    rule("And why a thin window says nothing at all")
    thin = evaluate_rule(
        FAST, budget=BUDGET, long_samples=10, long_bad=10, short_samples=10, short_bad=10
    )
    print(f"Ten calls, all of them failed: {thin.summary()}")
    print(
        "\nA burn rate of 1000x, and it is a quiet Sunday. It is reported as *not\n"
        "evaluated* rather than as passing: 'we did not look' and 'we looked and\n"
        "it was fine' are different facts."
    )
    return 0


def _short(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds / size:g}{unit}"
    return f"{seconds}s"


if __name__ == "__main__":
    raise SystemExit(main())
