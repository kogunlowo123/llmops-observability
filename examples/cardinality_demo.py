#!/usr/bin/env python
"""The metric that takes out your metrics backend, refused before it exists.

python examples/cardinality_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from llmops.cardinality.budget import DEFAULT_LABELS, LabelRegistry, bounded
from llmops.errors import CardinalityError


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def try_metric(registry: LabelRegistry, name: str, labels: list[str]) -> None:
    printed = ", ".join(labels) or "(none)"
    try:
        spec = registry.check(name, labels)
    except CardinalityError as exc:
        print(f"  REFUSED  {name}{{{printed}}}")
        print(f"           {exc.message}")
        print(f"           {exc.remedy}")
    else:
        print(f"  ok       {name}{{{printed}}} - up to {spec.series} series")


def main() -> int:
    registry = LabelRegistry(DEFAULT_LABELS)

    rule("What a sensible metric looks like")
    try_metric(registry, "llm_requests_total", ["provider", "model", "outcome"])
    try_metric(registry, "llm_latency_seconds", ["model", "le"])

    rule("The one that looks harmless in a code review")
    try_metric(registry, "llm_requests_total", ["model", "tenant_id"])
    print(
        "\n  One series per tenant. On a busy day that is four hundred thousand\n"
        "  of them, and the metrics backend goes down for everybody - including\n"
        "  the dashboards you would use to work out what happened."
    )

    rule("Content is not a dimension")
    try_metric(registry, "llm_requests_total", ["prompt_hash"])
    try_metric(registry, "llm_requests_total", ["completion"])

    rule("An unknown label fails closed")
    try_metric(registry, "llm_requests_total", ["shard"])
    print(
        "\n  The cost of wrongly refusing a bounded label is one line registering\n"
        "  it. The cost of wrongly accepting an unbounded one is an outage."
    )

    rule("Every label bounded, and the product is not")
    try_metric(registry, "llm_requests_total", ["provider", "model", "route", "service"])
    print(
        "\n  This is the one that slips past a reviewer checking labels one at a\n"
        "  time. Twenty providers times two hundred models times two hundred\n"
        "  routes times fifty services."
    )

    rule("The way out is bucketing, and the guard says so")
    registry.register(bounded("tenant_tier", 4))
    try_metric(registry, "llm_requests_total", ["model", "tenant_tier"])

    rule("Why this runs at registration and not at emit")
    print(
        "  By the time a bad label *value* arrives, the metric exists, the code\n"
        "  that emits it is deployed, and the series are already being created.\n"
        "  Refusing it when it is declared means the failure happens in a unit\n"
        "  test on a laptop."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
