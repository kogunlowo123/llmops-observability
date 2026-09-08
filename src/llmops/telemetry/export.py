"""Getting spans out of the process.

Two exporters, and one rule that governs both.

**The rule: nothing leaves the machine unless the caller said so.** An exporter
that can open a socket cannot be *constructed* in an offline run — not "will not
send", cannot be built. This is the same enforcement point the evaluation
harness in this series uses for network-capable providers, and it is at
construction for the same reason: a guard that fires when the socket opens has
already let a caller build an object whose whole purpose is forbidden, and
whether it fires then depends on which code path happened to run.

For a telemetry tool the stakes are higher than for most. This is the component
whose entire job is to send things somewhere, so "surprised nobody by sending
somewhere" has to be the default rather than a flag.

**The JSONL exporter** writes the window format. It is what the corpus, the
gate and the tests all use, and it reaches nothing.

**The OTLP exporter** POSTs the OTLP/HTTP JSON encoding to a collector, over
stdlib ``urllib``. It exists so a window here can be replayed into an existing
observability stack. It is exercised in the tests against a loopback server,
never against a real collector.

Redaction runs over the **assembled** payload, once, immediately before it is
written or sent. Not over the spans on the way in: anything derived from a span
after the pass — a joined string, an attribute copied into a resource — would
carry what the pass removed. That mistake has been made once in this series
already and the fix was to move the pass, not to add patterns.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from llmops import __version__
from llmops.errors import ExporterError, NetworkNotAllowedError
from llmops.redaction import Redaction, redact_structure
from llmops.telemetry.span import Span
from llmops.telemetry.window import Window, write_window_lines

#: Schemes an OTLP endpoint may use. ``file:`` and the rest are refused, so a
#: mistyped endpoint fails loudly instead of writing somewhere surprising.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: The first HTTP status that is not a success. urllib raises before we see one,
#: so the check below is a belt for a redirect handler that stops following.
_FIRST_NON_SUCCESS_STATUS = 300


class Exporter(ABC):
    """Somewhere spans can go."""

    #: Whether constructing this exporter permits a network connection. Read by
    #: :func:`build_exporter` *before* the object exists.
    reaches_network: bool = False
    name: str = "exporter"

    @abstractmethod
    def export(self, window: Window) -> Redaction:
        """Send *window*, returning what redaction removed on the way out."""

    def close(self) -> None:  # noqa: B027 - most exporters hold nothing
        """Release anything held. Safe to call twice.

        Concrete rather than abstract on purpose: an exporter that writes to a
        file holds nothing, and forcing every one of them to write an empty
        override is how a real cleanup comes to be omitted by copy-paste.
        """


class JsonlExporter(Exporter):
    """Writes the window file format. Reaches nothing."""

    reaches_network = False
    name = "jsonl"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def export(self, window: Window) -> Redaction:
        """Write *window*, redacted once over the assembled document."""
        header = window.as_header()
        lines = [dict(header), *(span.as_dict() for span in window.spans)]
        cleaned, redaction = redact_structure(lines)
        cleaned[0]["redaction"] = redaction.as_dict()

        write_window_lines(self.path, cleaned)
        return redaction


class OtlpHttpExporter(Exporter):
    """POSTs the OTLP/HTTP JSON encoding to a collector.

    Deliberately hand-written over stdlib ``urllib`` rather than built on the
    OpenTelemetry SDK. The wire format is a documented JSON shape, this is under
    a hundred lines of it, and taking on a tracing SDK to send one POST would
    make the largest dependency in the project the one serving its least-used
    component.
    """

    reaches_network = True
    name = "otlp"

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 10.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in ALLOWED_SCHEMES:
            raise ExporterError(
                f"the endpoint scheme {parsed.scheme!r} is not supported.",
                remedy=f"Use one of {', '.join(sorted(ALLOWED_SCHEMES))}.",
            )
        if not parsed.netloc:
            raise ExporterError(
                f"the endpoint {endpoint!r} has no host.",
                remedy="Use a full URL, for example http://localhost:4318/v1/traces.",
            )
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self.headers = dict(headers or {})

    def export(self, window: Window) -> Redaction:
        """Send *window* as one OTLP request."""
        payload = otlp_payload(window)
        cleaned, redaction = redact_structure(payload)
        body = json.dumps(cleaned, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(  # noqa: S310  # nosec B310 - scheme checked above
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "user-agent": f"llmops/{__version__}",
                **self.headers,
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310 - scheme checked above
                request, timeout=self.timeout_s
            ) as response:
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            # HTTPError *is* a response object and holds an open socket. Left
            # unclosed it surfaces later as a ResourceWarning attributed to
            # whatever happened to be running when the collector ran — which is
            # a bug report nobody can act on.
            exc.close()
            raise ExporterError(
                f"the collector rejected the export with HTTP {exc.code}.",
                remedy="Check the endpoint path; OTLP/HTTP traces are usually at /v1/traces.",
            ) from exc
        except urllib.error.URLError as exc:
            raise ExporterError(
                f"the collector at {self.endpoint} could not be reached: {exc.reason}.",
                remedy="Check the endpoint and that the collector is running.",
            ) from exc

        if status >= _FIRST_NON_SUCCESS_STATUS:  # pragma: no cover - urllib raises first
            raise ExporterError(f"the collector answered HTTP {status}.")
        return redaction


def otlp_payload(window: Window) -> dict[str, Any]:
    """Return the OTLP/HTTP JSON body for *window*.

    Separate from the exporter so it can be asserted against without a socket,
    and so the JSONL path can be diffed against it.
    """
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "llmops"}},
                        {"key": "telemetry.sdk.name", "value": {"stringValue": "llmops"}},
                        {"key": "telemetry.sdk.version", "value": {"stringValue": __version__}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "llmops", "version": __version__},
                        "spans": [_otlp_span(span) for span in window.spans],
                    }
                ],
            }
        ]
    }


def _otlp_span(span: Span) -> dict[str, Any]:
    start_ns = int(span.started_at.timestamp() * 1_000_000_000)
    end_ns = start_ns + int(span.duration_ms * 1_000_000)
    return {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        # 3 is SPAN_KIND_CLIENT: a model call is an outbound request, and a
        # backend that groups by kind puts it with the other dependencies.
        "kind": 3,
        "name": f"{span.operation} {span.model}",
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": span.as_otlp_attributes(),
        # 2 is STATUS_CODE_ERROR, 1 is OK. A refusal is OK: the platform
        # answered. See `Span.succeeded`.
        "status": {"code": 1 if span.succeeded else 2},
    }


def build_exporter(
    kind: str,
    *,
    offline: bool = True,
    path: str | Path | None = None,
    endpoint: str = "",
    timeout_s: float = 10.0,
) -> Exporter:
    """Construct an exporter, refusing a networked one in an offline run.

    The check is on the *class*, before the object exists. See the module
    docstring.
    """
    classes: dict[str, type[Exporter]] = {"jsonl": JsonlExporter, "otlp": OtlpHttpExporter}
    cls = classes.get(kind)
    if cls is None:
        raise ExporterError(
            f"unknown exporter {kind!r}.",
            remedy=f"Known exporters are {', '.join(sorted(classes))}.",
        )
    if offline and cls.reaches_network:
        raise NetworkNotAllowedError(
            f"the {kind!r} exporter opens a connection and this run is offline.",
            remedy=(
                "Pass --allow-network, having checked which endpoint it names. Nothing "
                "leaves this machine by default: a telemetry tool is exactly the "
                "component that should not surprise anyone by sending somewhere."
            ),
        )
    if cls is JsonlExporter:
        if path is None:
            raise ExporterError("the jsonl exporter needs a path.", remedy="Pass --out <path>.")
        return JsonlExporter(path)
    if not endpoint:
        raise ExporterError(
            "the otlp exporter needs an endpoint.",
            remedy="Pass --endpoint, or set LLMOPS_EXPORT__OTLP_ENDPOINT.",
        )
    return OtlpHttpExporter(endpoint, timeout_s=timeout_s)
