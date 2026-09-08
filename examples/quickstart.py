#!/usr/bin/env python
"""The whole loop in one screen: generate, gate, break, gate again.

Run it:

    python examples/quickstart.py

Nothing here reaches a network, needs a credential, or takes more than a couple
of seconds.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llmops.corpus.synth import SCENARIOS, generate
from llmops.slo.evaluate import evaluate
from llmops.slo.objective import ObjectiveSet, load_objectives
from llmops.telemetry.window import Window

OBJECTIVES = ROOT / "examples" / "objectives.yaml"

# The instant the outage scenario's incident is live. It matters: by the end of
# the window the incident is over, the short window has reset, and the gate is
# green — which is correct behaviour and the whole reason there are two windows.
DURING = SCENARIOS["outage"].start + timedelta(hours=22, minutes=40)


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def report(name: str, window: Window, objectives: ObjectiveSet, now: datetime) -> None:
    evaluation = evaluate(window, objectives, now=now)
    verdict = "held" if evaluation.passed else "BREACHED"
    print(f"{name:<28} {verdict}")
    for result in evaluation.breached:
        for firing in result.firing:
            print(f"    {firing.rule.severity.upper():<7} {result.objective.name}")
            print(f"            {firing.summary()}")


def main() -> int:
    objectives = load_objectives(OBJECTIVES)
    # Only the two objectives that need no price book, so the example runs with
    # nothing but this file.
    from dataclasses import replace

    objectives = replace(
        objectives,
        objectives=tuple(item for item in objectives.objectives if item.kind != "spend"),
    )

    rule("1. Generate three days of telemetry")
    healthy = generate(SCENARIOS["steady"])
    print(f"{healthy.total} spans over {healthy.duration}, digest {healthy.digest()[:23]}...")
    print("Synthesised, not captured. See examples/README.md.")

    rule("2. Gate it. This is the control, and it has to be green")
    report("steady, at the incident:", healthy, objectives, DURING)

    rule("3. Now a window with a 40-minute outage 22 hours in")
    broken = generate(SCENARIOS["outage"])
    report("outage, at the incident:", broken, objectives, DURING)

    rule("4. And the same window an hour later")
    report(
        "outage, an hour after:",
        broken,
        objectives,
        DURING + timedelta(hours=1),
    )
    print(
        "\nThe incident is over, so the short window has reset and the alert has\n"
        "cleared. A single-window alert would still be firing, which is how a\n"
        "pager comes to be ignored."
    )

    rule("5. What a CI job does")
    print("llmops gate --window telemetry.jsonl --objectives objectives.yaml")
    print("echo $?   # 0 held, 2 an objective was missed, 3 the tool could not run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
