"""JUnit XML, so a breached objective looks like a failing test.

Every CI system on earth knows how to render this. Emitting it means an error
budget appears in the same place as the unit tests, with the same red mark, and
nobody has to be taught a new dashboard to notice that the platform is on fire.

One objective becomes one test case. A firing rule becomes the failure, and the
message is the rule's own summary — which already says what it saw and what the
threshold was.

Control characters are stripped, because XML 1.0 cannot represent most of them
at all and a report that will not parse is worse than no report. That is not
hypothetical: an ``error_type`` copied from a provider's response can contain
anything.
"""

from __future__ import annotations

import re

# ElementTree is used to *build* a document and is never handed input: nothing
# in src/ calls a reader on it, so the entity-expansion and external-entity
# attacks B405 warns about have no entry point here. Taking on defusedxml to
# serialise would add a dependency defending a code path this package does not
# have. Recorded in security/audit-exceptions.md.
from xml.etree import ElementTree  # noqa: ICN001  # nosec B405

from llmops.redaction import redact_text
from llmops.slo.evaluate import Evaluation

#: Everything XML 1.0 forbids outright, plus the surrogates.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def render(evaluation: Evaluation) -> str:
    """Render the evaluation as JUnit XML."""
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": "llmops",
            "tests": str(len(evaluation.results)),
            "failures": str(len(evaluation.breached)),
            "errors": "0",
            "skipped": str(sum(1 for result in evaluation.results if not result.results)),
            "timestamp": evaluation.evaluated_at.isoformat(),
        },
    )

    properties = ElementTree.SubElement(suite, "properties")
    for name, value in (
        ("window_digest", evaluation.window_digest),
        ("objectives_digest", evaluation.objectives_digest),
        ("unreadable_lines", str(evaluation.unreadable)),
    ):
        ElementTree.SubElement(properties, "property", {"name": name, "value": _clean(value)})

    for result in evaluation.results:
        case = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": f"objective.{result.objective.kind}",
                "name": _clean(result.objective.name),
            },
        )
        if not result.results:
            # No rule could be evaluated at all. Skipped, not passed: the
            # distinction is the whole reason this file has a `skipped` count.
            ElementTree.SubElement(
                case,
                "skipped",
                {"message": "no burn rule could be evaluated over this window"},
            )
            continue
        for rule in result.firing:
            failure = ElementTree.SubElement(
                case,
                "failure",
                {"type": rule.rule.severity, "message": _clean(rule.summary())},
            )
            failure.text = _clean(
                f"{result.objective.name}: {rule.rule.name} fired. "
                f"Long window {rule.long.burn:.3f}x, short window {rule.short.burn:.3f}x, "
                f"threshold {rule.rule.burn_rate:g}x."
            )

    return ElementTree.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def _clean(text: str) -> str:
    redacted, _ = redact_text(text)
    return _ILLEGAL.sub("", redacted)
