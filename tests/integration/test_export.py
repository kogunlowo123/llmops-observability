"""Exporters, including the networked one — against a loopback server.

The HTTP exporter is exercised against a server this test starts, never against
a real collector. A test that reaches a vendor is a test that fails when the
vendor does, and a CI job that reaches one has a verdict that depends on a third
party.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from llmops.errors import ExporterError, NetworkNotAllowedError
from llmops.telemetry.export import (
    JsonlExporter,
    OtlpHttpExporter,
    build_exporter,
    otlp_payload,
)
from llmops.telemetry.window import build_window, read_window
from tests.conftest import FAKE_OPENAI_KEY, ORIGIN, make_span, spread

pytestmark = pytest.mark.integration


class _Collector(BaseHTTPRequestHandler):
    """Records what it was sent, and answers however the test told it to."""

    status = 200
    received: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        type(self).received.append(json.loads(body))
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    # HTTP/1.0, so each request closes its connection rather than being kept
    # alive until the garbage collector notices — which surfaces as a
    # ResourceWarning attributed to an unrelated test.
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:
        """Silence. The test's output is the assertion, not the access log."""


@pytest.fixture
def collector():
    _Collector.received = []
    _Collector.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def window():
    return build_window(spread(5, over=timedelta(minutes=5)))


class TestConstruction:
    def test_a_networked_exporter_cannot_be_built_offline(self):
        # At construction, not at send. A guard that fires when the socket
        # opens has already let a caller build the forbidden object.
        with pytest.raises(NetworkNotAllowedError, match="offline"):
            build_exporter("otlp", offline=True, endpoint="http://localhost:4318/v1/traces")

    def test_the_refusal_names_the_flag_that_lifts_it(self):
        with pytest.raises(NetworkNotAllowedError) as caught:
            build_exporter("otlp", offline=True, endpoint="http://localhost:4318")
        assert "--allow-network" in caught.value.remedy

    def test_a_file_exporter_is_fine_offline(self, tmp_path: Path):
        assert isinstance(
            build_exporter("jsonl", offline=True, path=tmp_path / "w.jsonl"), JsonlExporter
        )

    def test_an_unknown_exporter_names_the_known_ones(self):
        with pytest.raises(ExporterError) as caught:
            build_exporter("kafka", offline=True)
        assert "jsonl" in caught.value.remedy

    def test_a_file_exporter_without_a_path_is_refused(self):
        with pytest.raises(ExporterError, match="needs a path"):
            build_exporter("jsonl", offline=True)

    def test_an_otlp_exporter_without_an_endpoint_is_refused(self):
        with pytest.raises(ExporterError, match="needs an endpoint"):
            build_exporter("otlp", offline=False)

    @pytest.mark.parametrize("endpoint", ["file:///etc/passwd", "ftp://host/x", "gopher://x"])
    def test_a_non_http_scheme_is_refused(self, endpoint: str):
        # A mistyped endpoint should fail loudly rather than write somewhere
        # surprising.
        with pytest.raises(ExporterError, match="not supported"):
            OtlpHttpExporter(endpoint)

    def test_an_endpoint_with_no_host_is_refused(self):
        with pytest.raises(ExporterError, match="no host"):
            OtlpHttpExporter("http:///v1/traces")


class TestTheFileExporter:
    def test_what_it_writes_reads_back(self, tmp_path: Path, window):
        exporter = JsonlExporter(tmp_path / "w.jsonl")
        exporter.export(window)
        assert read_window(tmp_path / "w.jsonl").digest() == window.digest()

    def test_it_creates_the_directory(self, tmp_path: Path, window):
        JsonlExporter(tmp_path / "a" / "b" / "w.jsonl").export(window)
        assert (tmp_path / "a" / "b" / "w.jsonl").is_file()

    def test_it_records_what_redaction_removed(self, tmp_path: Path):
        spans = [
            make_span(attributes={"base_url": f"https://api.example/v1?key={FAKE_OPENAI_KEY}"})
        ]
        exporter = JsonlExporter(tmp_path / "w.jsonl")

        redaction = exporter.export(
            build_window(spans, start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        )

        assert redaction.count == 1
        assert FAKE_OPENAI_KEY not in (tmp_path / "w.jsonl").read_text(encoding="utf-8")

    def test_the_count_is_in_the_written_header(self, tmp_path: Path):
        spans = [make_span(attributes={"note": FAKE_OPENAI_KEY})]
        JsonlExporter(tmp_path / "w.jsonl").export(
            build_window(spans, start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        )
        header = json.loads((tmp_path / "w.jsonl").read_text(encoding="utf-8").split("\n")[0])
        assert header["redaction"]["count"] == 1

    def test_close_is_safe_to_call_twice(self, tmp_path: Path):
        exporter = JsonlExporter(tmp_path / "w.jsonl")
        exporter.close()
        exporter.close()


class TestTheOtlpExporter:
    def _endpoint(self, server: HTTPServer) -> str:
        host, port = server.server_address[:2]
        # `server_address` is typed loosely enough that mypy sees bytes here.
        return f"http://{host!s}:{port}/v1/traces"

    def test_it_posts_the_window(self, collector: HTTPServer, window):
        OtlpHttpExporter(self._endpoint(collector)).export(window)

        assert len(_Collector.received) == 1
        spans = _Collector.received[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 5

    def test_it_redacts_before_sending(self, collector: HTTPServer):
        # The pass runs over the assembled payload, not over the spans on the
        # way in: anything derived from a span after the pass would carry what
        # the pass removed.
        window = build_window(
            [make_span(attributes={"endpoint": f"https://u:{FAKE_OPENAI_KEY}@api.example/v1"})],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        OtlpHttpExporter(self._endpoint(collector)).export(window)

        assert FAKE_OPENAI_KEY not in json.dumps(_Collector.received[0])

    def test_a_rejected_export_names_the_usual_cause(self, collector: HTTPServer):
        _Collector.status = 404
        with pytest.raises(ExporterError) as caught:
            OtlpHttpExporter(self._endpoint(collector)).export(
                build_window([make_span()], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
            )
        assert "/v1/traces" in caught.value.remedy

    def test_an_unreachable_collector_is_reported_not_swallowed(self):
        # Port 1 on loopback: nothing listens there, and the failure is fast.
        with pytest.raises(ExporterError, match="could not be reached"):
            OtlpHttpExporter("http://127.0.0.1:1/v1/traces", timeout_s=2.0).export(
                build_window([make_span()], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
            )


class TestTheOtlpEncoding:
    def test_timestamps_are_nanoseconds_as_strings(self, window):
        span = otlp_payload(window)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        # Nanoseconds do not fit in a JSON number safely, which is why the
        # encoding requires strings.
        assert isinstance(span["startTimeUnixNano"], str)
        assert int(span["endTimeUnixNano"]) > int(span["startTimeUnixNano"])

    def test_a_failed_span_carries_an_error_status(self):
        window = build_window(
            [make_span(outcome="error")], start=ORIGIN, end=ORIGIN + timedelta(hours=1)
        )
        span = otlp_payload(window)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["status"]["code"] == 2

    def test_a_refusal_carries_an_ok_status(self):
        # The platform answered. See `Span.succeeded`.
        window = build_window(
            [make_span(outcome="refusal")], start=ORIGIN, end=ORIGIN + timedelta(hours=1)
        )
        span = otlp_payload(window)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["status"]["code"] == 1

    def test_the_span_kind_is_client(self, window):
        # A model call is an outbound request, so a backend that groups by kind
        # puts it with the other dependencies.
        span = otlp_payload(window)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["kind"] == 3
