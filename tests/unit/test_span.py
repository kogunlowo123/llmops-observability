"""The span: the unit everything else is computed from."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from llmops.errors import SpanError
from llmops.telemetry.span import MAX_ATTRIBUTE_BYTES, MAX_ATTRIBUTES, Span
from tests.conftest import ORIGIN, make_span

pytestmark = pytest.mark.unit


class TestWhatASpanRefuses:
    def test_a_naive_timestamp_is_refused(self):
        # A naive datetime is an instant plus an assumption, and the assumption
        # is the machine's local zone — which silently moves spans across a
        # window boundary between a laptop and a CI runner.
        with pytest.raises(SpanError, match="no timezone"):
            make_span(at=datetime(2026, 9, 1, 12, 0, 0))  # noqa: DTZ001

    def test_an_offset_timestamp_is_normalised_to_utc(self):
        from datetime import timezone

        span = make_span(at=datetime(2026, 9, 1, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))))
        assert span.started_at == datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    def test_more_cached_tokens_than_input_tokens_is_refused(self):
        # Cached tokens are the discounted subset of the input, not an extra
        # charge on top. Costing them twice inflates a bill.
        with pytest.raises(SpanError, match="exceeds input_tokens"):
            make_span(input_tokens=100, cached_input_tokens=101)

    def test_all_cached_is_allowed(self):
        assert make_span(input_tokens=100, cached_input_tokens=100).billable_input_tokens == 0

    def test_a_successful_span_may_not_carry_an_error_type(self):
        with pytest.raises(SpanError, match="carries an error_type"):
            Span(
                span_id="s",
                started_at=ORIGIN,
                duration_ms=1.0,
                provider="openai",
                model="m",
                outcome="ok",
                error_type="upstream_error",
            )

    def test_an_unknown_field_is_refused(self):
        # extra="forbid". A misspelt `output_token` would otherwise be dropped
        # and every cost computed from that span would be short.
        with pytest.raises(ValidationError):
            Span(
                span_id="s",
                started_at=ORIGIN,
                duration_ms=1.0,
                provider="openai",
                model="m",
                output_token=5,  # type: ignore[call-arg]
            )

    def test_too_many_attributes_are_refused(self):
        with pytest.raises(SpanError, match="attributes"):
            make_span(attributes={f"k{index}": "v" for index in range(MAX_ATTRIBUTES + 1)})

    def test_an_oversized_attribute_is_refused(self):
        # A span has no prompt field, and an oversized attribute is usually one
        # arriving by the back door.
        with pytest.raises(SpanError, match="longer than"):
            make_span(attributes={"note": "x" * (MAX_ATTRIBUTE_BYTES + 1)})

    def test_a_negative_duration_is_refused(self):
        with pytest.raises(ValidationError):
            make_span(duration_ms=-1.0)

    def test_there_is_nowhere_to_put_a_prompt(self):
        # The single most effective privacy control available to a telemetry
        # pipeline: no field. Asserted rather than assumed, because a field
        # added in good faith later would silently undo it.
        assert "prompt" not in Span.model_fields
        assert "completion" not in Span.model_fields
        assert "messages" not in Span.model_fields


class TestWhatASpanComputes:
    def test_ended_at_is_started_at_plus_duration(self):
        span = make_span(duration_ms=1500.0)
        assert span.ended_at == span.started_at + timedelta(milliseconds=1500)

    def test_billable_input_excludes_the_cached_part(self):
        assert make_span(input_tokens=1000, cached_input_tokens=400).billable_input_tokens == 600

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("ok", True),
            # A refusal is a success for availability: the platform answered.
            # An objective that counts refusals as outages rewards a team for
            # weakening its safety filter.
            ("refusal", True),
            ("error", False),
            ("timeout", False),
            ("filtered", False),
        ],
    )
    def test_which_outcomes_count_as_successful(self, outcome, expected):
        assert make_span(outcome=outcome, error_type="").succeeded is expected


class TestTheFingerprint:
    def test_it_covers_the_billable_facts(self):
        assert (
            make_span(output_tokens=200).fingerprint() != make_span(output_tokens=201).fingerprint()
        )

    def test_it_ignores_what_no_report_reads(self):
        # An attribute added for a dashboard does not change what anything
        # bills or measures, and a drift check that fires on it is a check
        # people learn to ignore.
        assert (
            make_span(attributes={"route": "/a"}).fingerprint()
            == make_span(attributes={"route": "/b"}).fingerprint()
        )

    def test_it_is_stable_across_processes(self):
        # sha256 of a joined string, not `hash()`, which is salted per process
        # and would make a committed digest meaningless.
        assert make_span().fingerprint() == make_span().fingerprint()
        assert make_span().fingerprint().startswith("sha256:")


class TestSerialisation:
    def test_defaults_are_omitted(self):
        payload = make_span().as_dict()
        assert "cached_input_tokens" not in payload
        assert "error_type" not in payload
        assert "attributes" not in payload

    def test_what_is_set_is_kept(self):
        payload = make_span(
            cached_input_tokens=10, attributes={"route": "/chat"}, reported_cost_usd=0.5
        ).as_dict()
        assert payload["cached_input_tokens"] == 10
        assert payload["attributes"] == {"route": "/chat"}
        assert payload["reported_cost_usd"] == 0.5

    def test_a_round_trip_preserves_the_fingerprint(self):
        span = make_span(cached_input_tokens=10, attributes={"route": "/chat"})
        assert Span.model_validate(span.as_dict()).fingerprint() == span.fingerprint()

    def test_otlp_attributes_use_the_genai_convention_names(self):
        attributes = {item["key"] for item in make_span().as_otlp_attributes()}
        assert "gen_ai.request.model" in attributes
        assert "gen_ai.usage.input_tokens" in attributes
        assert "gen_ai.system" in attributes

    def test_otlp_integers_are_strings(self):
        # The OTLP JSON encoding requires int64 values as strings, because
        # JSON numbers cannot represent the full range.
        tokens = next(
            item
            for item in make_span().as_otlp_attributes()
            if item["key"] == "gen_ai.usage.input_tokens"
        )
        assert tokens["value"] == {"intValue": "1000"}

    def test_an_error_type_reaches_otlp(self):
        attributes = {
            item["key"]: item["value"] for item in make_span(outcome="error").as_otlp_attributes()
        }
        assert attributes["error.type"] == {"stringValue": "upstream_error"}
