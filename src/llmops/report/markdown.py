"""A summary a person reads, in a pull request or a job summary.

Written for the moment somebody is looking at a red build and wants to know
what broke before they want to know anything else. So: the verdict, then the
objectives that failed and what they saw, then everything else.

A rule that was *not evaluated* gets its own section rather than being omitted.
"We did not look" and "we looked and it was fine" are different facts, and a
summary that renders them identically is lying by omission — which is the one
thing a report about reliability cannot afford to do.
"""

from __future__ import annotations

from llmops.cost.report import CostReport
from llmops.redaction import redact_text
from llmops.slo.evaluate import Evaluation, ObjectiveResult

#: Rows before a table is truncated. A summary with two hundred rows is one
#: nobody reads, and the renderer should choose where to stop rather than
#: leaving it to whatever truncates the page.
LIST_LIMIT = 15


def render(evaluation: Evaluation, *, cost: CostReport | None = None) -> str:
    """Render the summary as Markdown."""
    verdict = "held" if evaluation.passed else "BREACHED"
    lines = [
        f"# Error budget: {verdict}",
        "",
        f"{evaluation.summary()} at {evaluation.evaluated_at.isoformat()}.",
        "",
    ]

    if evaluation.unreadable:
        # Before anything else. A verdict computed over 97% of the evidence has
        # to say so above the numbers, not in a footnote below them.
        lines += [
            (
                f"> **{evaluation.unreadable} line(s) of the window could not be read.** "
                "Every number below is over what was left."
            ),
            "",
        ]

    breached = evaluation.breached
    if breached:
        lines += ["## What broke", ""]
        for result in breached:
            lines += _objective_section(result)

    lines += ["## Every objective", "", "| Objective | Kind | Scope | In scope | Verdict |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for result in evaluation.results:
        objective = result.objective
        state = "breached" if result.breached else "held"
        lines.append(
            f"| {_cell(objective.name)} | {objective.kind} | "
            f"{_cell(objective.selector.describe())} | {result.in_scope} | {state} |"
        )
    lines.append("")

    unevaluated = [
        (result.objective.name, rule.rule.name, rule.long.samples)
        for result in evaluation.results
        for rule in result.results
        if not rule.evaluated
    ]
    not_covered = [
        (result.objective.name, name, reason)
        for result in evaluation.results
        for name, reason in result.not_covered
    ]
    if unevaluated or not_covered:
        lines += ["## Not evaluated", ""]
        lines.append(
            "These rules reached no verdict. That is not the same as passing, and it is "
            "listed here so nobody reads a green summary as more coverage than it is."
        )
        lines.append("")
        lines.extend(
            f"- `{name}` / `{rule}` — only {samples} call(s) in its window"
            for name, rule, samples in unevaluated[:LIST_LIMIT]
        )
        lines.extend(
            f"- `{name}` / `{rule}` — {_cell(reason)}"
            for name, rule, reason in not_covered[:LIST_LIMIT]
        )
        lines.append("")

    if cost is not None:
        lines += _cost_section(cost)

    lines += [
        "---",
        "",
        (
            f"window `{cost.window_digest if cost else evaluation.window_digest}` · "
            f"objectives `{evaluation.objectives_digest}`"
        ),
        "",
    ]
    return "\n".join(lines)


def _objective_section(result: ObjectiveResult) -> list[str]:
    objective = result.objective
    lines = [
        f"### {objective.name} ({objective.kind}, {objective.selector.describe()})",
        "",
    ]
    if objective.description:
        lines += [_cell(objective.description), ""]
    lines.append("| Rule | Severity | Long window | Short window | Threshold |")
    lines.append("| --- | --- | --- | --- | --- |")
    for rule in result.firing:
        lines.append(
            f"| `{rule.rule.name}` | {rule.rule.severity} | "
            f"{rule.long.burn:.2f}x | {rule.short.burn:.2f}x | "
            f"{rule.rule.burn_rate:g}x |"
        )
    lines.append("")
    return lines


def _cost_section(cost: CostReport) -> list[str]:
    lines = ["## Spend", "", cost.summary(), "", f"| {cost.dimension.title()} | Calls | Cost |"]
    lines.append("| --- | --- | --- |")
    lines.extend(
        f"| {_cell(line.key)} | {line.calls} | ${float(line.cost_usd):.6f} |"
        for line in cost.lines[:LIST_LIMIT]
    )
    if len(cost.lines) > LIST_LIMIT:
        lines.append(f"| … | | {len(cost.lines) - LIST_LIMIT} more |")
    lines.append("")

    if cost.discrepancies:
        lines += [
            "### Where this disagrees with the provider",
            "",
            "| Span | Model | Computed | Reported |",
            "| --- | --- | --- | --- |",
        ]
        for item in cost.discrepancies[:LIST_LIMIT]:
            lines.append(
                f"| `{_cell(item.span_id)}` | {_cell(item.model)} | "
                f"${float(item.computed_usd):.6f} | ${float(item.reported_usd):.6f} |"
            )
        if len(cost.discrepancies) > LIST_LIMIT:
            lines.append(f"| … | | | {len(cost.discrepancies) - LIST_LIMIT} more |")
        lines.append("")
    return lines


def _cell(text: str, limit: int = 120) -> str:
    """Fit a string into a table cell without breaking the table."""
    cleaned, _ = redact_text(text)
    collapsed = " ".join(cleaned.split()).replace("|", "\\|")
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed or "—"
