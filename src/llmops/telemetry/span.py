"""One record of one model call.

A span here is deliberately narrow. It is not a general tracing primitive with
arbitrary children and arbitrary attributes; it is *the unit an LLM platform is
accountable for*, and every field on it exists because some objective in this
tool reads it:

``model`` and ``provider``      priced by the price book, and a cost dimension
``operation``                   chat, embedding or completion; priced differently
``input_tokens``/``output_tokens``  the two numbers cost is computed from
``duration_ms``                 the latency objective
``outcome``                     the availability objective
``started_at``                  which window a span falls into

Attribute names follow the OpenTelemetry GenAI semantic conventions
(``gen_ai.request.model``, ``gen_ai.usage.input_tokens`` and so on) so that a
window exported from here lands in an existing dashboard without a translation
layer. This project does not claim conformance to that specification and does
not ship a checker for it: the conventions are still moving, and a conformance
claim pinned to a revision nobody can verify from the README is worth less than
no claim. What is guaranteed is the shape of *this* record, which is versioned.

**Prompts and completions are not fields.** There is nowhere on a span to put
one. That is the single most effective privacy control available to a telemetry
pipeline, it costs nothing, and it is why the redaction pass over the remaining
free-text fields is a second line of defence rather than the only one.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from llmops.errors import SpanError

#: Bumped when a field changes meaning. A window carries it, so a reader can
#: tell "this window predates the field you are looking for" from "this window
#: is broken".
SPAN_SCHEMA_VERSION = 1

#: What a call was for. Priced separately because the rates differ by an order
#: of magnitude, and a spend report that treats an embedding as a chat turn is
#: wrong in the direction that looks fine.
Operation = Literal["chat", "completion", "embedding", "rerank"]

#: How a call ended.
#:
#: ``ok`` and ``error`` are obvious. ``refusal`` is separate because a model
#: declining to answer is a *successful* call the platform was billed for and a
#: *failed* call from the user's point of view, and an availability objective
#: that folds it into either one is measuring the wrong thing. Which side it
#: counts on is the objective's decision, not the span's.
Outcome = Literal["ok", "error", "timeout", "refusal", "filtered"]

#: Outcomes that cost money. A timeout is billed by most providers when the
#: model had already started producing tokens, so it is not free; a request
#: rejected before it reached the model is.
MAX_ATTRIBUTE_BYTES = 1024
MAX_ATTRIBUTES = 32


class Span(BaseModel):
    """One model call, as this tool accounts for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Identifies the call. Not required to be globally unique across windows;
    #: required to be unique *within* one, so a duplicate is detectable.
    span_id: Annotated[str, Field(min_length=1, max_length=64)]
    trace_id: Annotated[str, Field(min_length=1, max_length=64)] = "unset"

    started_at: datetime
    duration_ms: Annotated[float, Field(ge=0.0, le=3_600_000.0)]

    provider: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    operation: Operation = "chat"
    outcome: Outcome = "ok"

    input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    output_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0
    #: Tokens the provider served from its own cache, billed at a reduced rate.
    #: A subset of ``input_tokens``, not an addition to it — see the validator.
    cached_input_tokens: Annotated[int, Field(ge=0, le=100_000_000)] = 0

    #: What the provider says it charged, when it says anything. Optional, and
    #: the reason the reconciliation report exists: a computed cost that has
    #: never been checked against a bill is an estimate wearing a number's
    #: clothes.
    reported_cost_usd: Annotated[float, Field(ge=0.0)] | None = None

    #: Free-form dimensions: environment, service, route, tenant class. Bounded
    #: in count and size, redacted before export, and refused outright by the
    #: cardinality guard when used as a metric label without bucketing.
    attributes: dict[str, str] = Field(default_factory=dict)

    #: Set when ``outcome`` is not ``ok``. A class of failure, never a message:
    #: a message is where a prompt fragment ends up in a dashboard.
    error_type: Annotated[str, Field(max_length=64)] = ""

    @field_validator("started_at")
    @classmethod
    def _must_be_aware(cls, value: datetime) -> datetime:
        """Reject a naive timestamp.

        A window is a half-open interval over instants. A naive datetime is not
        an instant, it is an instant *plus an assumption*, and the assumption is
        the machine's local zone — which differs between a developer's laptop
        and a CI runner, silently moving spans across a window boundary.
        """
        if value.tzinfo is None:
            raise SpanError(
                "started_at has no timezone.",
                remedy=(
                    "Use an aware timestamp: datetime.now(UTC), or parse with a "
                    "'Z' or '+00:00' suffix. A naive timestamp means a different "
                    "instant on every machine."
                ),
            )
        return value.astimezone(UTC)

    @field_validator("attributes")
    @classmethod
    def _bounded(cls, value: dict[str, str]) -> dict[str, str]:
        """Bound the attribute bag in count and in size."""
        if len(value) > MAX_ATTRIBUTES:
            raise SpanError(
                f"a span carries {len(value)} attributes; the limit is {MAX_ATTRIBUTES}.",
                remedy="Attributes are dimensions, not a payload. Drop what nothing queries.",
            )
        for key, item in value.items():
            if len(item.encode("utf-8")) > MAX_ATTRIBUTE_BYTES:
                raise SpanError(
                    f"attribute {key!r} is longer than {MAX_ATTRIBUTE_BYTES} bytes.",
                    remedy=(
                        "A span has no prompt or completion field, and an oversized "
                        "attribute is usually one arriving by the back door."
                    ),
                )
        return value

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        """Reject combinations that cannot have happened."""
        if self.cached_input_tokens > self.input_tokens:
            raise SpanError(
                "cached_input_tokens exceeds input_tokens.",
                remedy=(
                    "Cached tokens are the discounted *subset* of the input, not an "
                    "extra charge on top of it. Costing them twice inflates the bill."
                ),
            )
        if self.outcome == "ok" and self.error_type:
            raise SpanError(
                "a successful span carries an error_type.",
                remedy="Set outcome to 'error', or clear error_type.",
            )
        return self

    @property
    def ended_at(self) -> datetime:
        """When the call finished."""
        return datetime.fromtimestamp(
            self.started_at.timestamp() + self.duration_ms / 1000.0, tz=UTC
        )

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens charged at the full rate."""
        return self.input_tokens - self.cached_input_tokens

    @property
    def succeeded(self) -> bool:
        """Whether this call is a success for an availability objective.

        A refusal counts as a success here: the platform answered, correctly,
        and an availability objective that counts safety refusals as outages
        rewards a team for weakening its safety filter. Whether a refusal is
        *acceptable* is a question for a quality gate, which is a different
        tool in this series.
        """
        return self.outcome in {"ok", "refusal"}

    def fingerprint(self) -> str:
        """Return a content address of the billable facts.

        Covers what a cost or budget report is computed from and nothing else,
        so that reformatting a window, reordering it or adding an attribute does
        not change it. Used by the corpus drift check.
        """
        material = "␟".join(
            [
                self.span_id,
                self.provider,
                self.model,
                self.operation,
                self.outcome,
                f"{self.started_at.timestamp():.6f}",
                f"{self.duration_ms:.3f}",
                str(self.input_tokens),
                str(self.output_tokens),
                str(self.cached_input_tokens),
            ]
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a window file."""
        payload: dict[str, Any] = {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "outcome": self.outcome,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.cached_input_tokens:
            payload["cached_input_tokens"] = self.cached_input_tokens
        if self.reported_cost_usd is not None:
            payload["reported_cost_usd"] = self.reported_cost_usd
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.attributes:
            payload["attributes"] = dict(self.attributes)
        return payload

    def as_otlp_attributes(self) -> list[dict[str, Any]]:
        """Return the OTLP key/value form, using GenAI semantic convention names.

        Emitted so a window lands in an existing dashboard without a translation
        layer. See the module docstring on why this is not a conformance claim.
        """
        values: list[tuple[str, Any]] = [
            ("gen_ai.system", self.provider),
            ("gen_ai.operation.name", self.operation),
            ("gen_ai.request.model", self.model),
            ("gen_ai.usage.input_tokens", self.input_tokens),
            ("gen_ai.usage.output_tokens", self.output_tokens),
            ("llmops.outcome", self.outcome),
        ]
        if self.cached_input_tokens:
            values.append(("gen_ai.usage.cached_input_tokens", self.cached_input_tokens))
        if self.error_type:
            values.append(("error.type", self.error_type))
        values.extend(sorted(self.attributes.items()))
        return [{"key": key, "value": _otlp_value(item)} for key, item in values]


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):  # pragma: no cover - no boolean attributes today
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):  # pragma: no cover - no float attributes today
        return {"doubleValue": value}
    return {"stringValue": str(value)}
