"""The machine-readable report.

Goes to stdout, so a build script can parse it. The verdict is the first key,
because a script that greps rather than parses should be able to answer the only
question that matters without reading the whole document.

Redaction runs over the **assembled** report, once, at the end. Every string in
it — an objective's name, a selector value, a window's source path, an error
type — is derived from something a user supplied, and the pass has to see the
finished artefact. This placement is the lesson from the third repository in
this series, where a pass over the inputs left a derived excerpt carrying what
had just been removed.
"""

from __future__ import annotations

import json
from typing import Any

from llmops import __version__
from llmops.cardinality.budget import MetricSpec
from llmops.cost.report import CostReport
from llmops.redaction import redact_structure
from llmops.slo.evaluate import Evaluation


def render(
    evaluation: Evaluation,
    *,
    cost: CostReport | None = None,
    metrics: tuple[MetricSpec, ...] = (),
    indent: int = 2,
) -> str:
    """Render the report as JSON."""
    document: dict[str, Any] = {
        "passed": evaluation.passed,
        "tool": {"name": "llmops", "version": __version__},
        "evaluation": evaluation.as_dict(),
    }
    if cost is not None:
        document["cost"] = cost.as_dict()
    if metrics:
        document["metrics"] = [spec.as_dict() for spec in metrics]

    # Assembled first, redacted once. See the module docstring.
    cleaned, redaction = redact_structure(document)
    cleaned["redaction"] = redaction.as_dict()
    return json.dumps(cleaned, indent=indent, ensure_ascii=False, sort_keys=False)
