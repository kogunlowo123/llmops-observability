#!/usr/bin/env python
"""Assert the gate goes red on each incident, and red for the right reason.

A gate that has only ever been observed passing is indistinguishable from a
`true` in a shell script. This runs the real command line against each shipped
incident window and asserts three things per scenario:

1. the process exits **2**, not merely non-zero — 3 would mean the tool broke
2. exactly the expected objective is breached, and no other
3. the healthy window is still green at the same instant, so the difference is
   the incident and not the moment it was measured

The third check is the one that is easy to leave out and the one that carries
the argument. Without it, "the gate fired at 22:40" could be a fact about 22:40.

Run through ``python tasks.py check-incidents``. CI runs it on every push.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ROOT / "examples" / "windows"
OBJECTIVES = ROOT / "examples" / "objectives.yaml"
PRICES = ROOT / "examples" / "prices.yaml"

EXIT_OK = 0
EXIT_BUDGET_BURNED = 2

#: scenario -> (the instant the incident was live, the objective it must break)
INCIDENTS: dict[str, tuple[str, str]] = {
    "outage": ("2026-09-01T22:40:00+00:00", "chat-availability"),
    "slowdown": ("2026-09-02T12:00:00+00:00", "chat-latency"),
    "cost-spike": ("2026-09-03T12:00:00+00:00", "monthly-spend"),
}


def gate(window: str, at: str) -> tuple[int, dict[str, Any]]:
    """Run the gate and return its exit code and parsed report."""
    result = subprocess.run(  # noqa: S603 - a fixed argument vector
        [
            sys.executable,
            "-m",
            "llmops",
            "gate",
            "--window",
            str(WINDOWS / f"{window}.jsonl.gz"),
            "--objectives",
            str(OBJECTIVES),
            "--pricebook",
            str(PRICES),
            "--at",
            at,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        cwd=ROOT,
    )
    try:
        return result.returncode, json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"the gate did not produce a report:\n{result.stderr}", file=sys.stderr)
        raise


def breached(report: dict[str, Any]) -> set[str]:
    """The names of the objectives the report says were breached."""
    objectives = report["evaluation"]["objectives"]
    return {str(entry["objective"]["name"]) for entry in objectives if entry["breached"]}


def main() -> int:
    failures = 0

    for scenario, (at, expected) in INCIDENTS.items():
        code, report = gate(scenario, at)
        names = breached(report)

        if code != EXIT_BUDGET_BURNED:
            print(f"FAIL  {scenario}: exit {code}, expected {EXIT_BUDGET_BURNED}", file=sys.stderr)
            failures += 1
        elif names != {expected}:
            print(
                f"FAIL  {scenario}: breached {sorted(names) or 'nothing'}, expected [{expected}]",
                file=sys.stderr,
            )
            failures += 1
        else:
            print(f"ok    {scenario}: exit 2, {expected} breached and nothing else")

        # The control, at the same instant. Without it the finding above could
        # be a fact about the moment rather than about the incident.
        control_code, control = gate("steady", at)
        if control_code != EXIT_OK or breached(control):
            print(
                f"FAIL  steady at {at}: exit {control_code}, breached {sorted(breached(control))}",
                file=sys.stderr,
            )
            failures += 1
        else:
            print("ok    steady at the same instant: exit 0, nothing breached")

    if failures:
        print(f"\n{failures} check(s) failed", file=sys.stderr)
        return 1
    print(f"\nall {len(INCIDENTS)} incident(s) fire correctly, and the control stays green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
