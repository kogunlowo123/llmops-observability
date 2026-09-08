"""Adversarial cases. A failure here is a security regression, not a bug.

The claim this file supports is stated precisely in SECURITY.md and on the front
of the README:

> A span has no prompt or completion field. There is nowhere to put one.
> Redaction over the attribute bag is a second line of defence.

Both halves are asserted. The first is structural and cheap; the second is the
one that has to keep working as the code changes, and it is asserted against the
**whole serialised artefact** rather than against one member of it. The
interesting failure is not a missing pattern — it is a control that exists and
is not wired up, which is what happens when a derived string is attached after
the pass has run.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from llmops.cost.pricebook import PriceBook
from llmops.cost.report import cost_window
from llmops.redaction import PLACEHOLDER, is_sensitive_key, redact_structure, redact_text
from llmops.report import json_report, junit, markdown
from llmops.slo.evaluate import evaluate
from llmops.slo.objective import ObjectiveSet, parse_objectives
from llmops.telemetry.export import JsonlExporter, otlp_payload
from llmops.telemetry.span import Span
from llmops.telemetry.window import build_window
from tests.conftest import (
    FAKE_ANTHROPIC_KEY,
    FAKE_AWS_KEY,
    FAKE_BEARER,
    FAKE_GITHUB_TOKEN,
    FAKE_JWT,
    FAKE_OPENAI_KEY,
    ORIGIN,
    make_span,
)

pytestmark = pytest.mark.security

CREDENTIALS = [
    FAKE_OPENAI_KEY,
    FAKE_ANTHROPIC_KEY,
    FAKE_AWS_KEY,
    FAKE_GITHUB_TOKEN,
    FAKE_BEARER,
    FAKE_JWT,
]


class TestThereIsNowhereToPutAPrompt:
    def test_the_span_has_no_content_field(self):
        # The strongest control in the pipeline, and it costs nothing. A field
        # added later in good faith would silently undo it, so it is asserted.
        forbidden = {"prompt", "completion", "messages", "input", "output", "text", "content"}
        assert forbidden.isdisjoint(Span.model_fields)

    def test_an_unknown_field_cannot_be_smuggled_in(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Span(
                span_id="s",
                started_at=ORIGIN,
                duration_ms=1.0,
                provider="p",
                model="m",
                prompt="the user's private question",  # type: ignore[call-arg]
            )


class TestRedactionPatterns:
    @pytest.mark.parametrize("credential", CREDENTIALS)
    def test_each_credential_shape_is_removed(self, credential: str):
        cleaned, redaction = redact_text(f"the value is {credential} and that is that")
        assert credential not in cleaned
        assert PLACEHOLDER in cleaned
        assert redaction.count == 1

    def test_a_url_credential_is_removed_and_the_host_kept(self):
        # Removing the host too would make the attribute worthless, and the
        # host is the half a person needs to debug with.
        cleaned, _ = redact_text("postgres://user:hunter2@db.internal:5432/app")
        assert "hunter2" not in cleaned
        assert "db.internal" in cleaned

    def test_a_private_key_header_is_removed(self):
        cleaned, _ = redact_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
        assert "BEGIN RSA PRIVATE KEY" not in cleaned

    def test_ordinary_text_is_untouched(self):
        # A redaction pass that mangles normal values makes dashboards useless
        # and gets turned off.
        text = "gpt-4o answered in 812ms via /chat in prod"
        assert redact_text(text) == (text, redact_text(text)[1])
        assert redact_text(text)[1].clean

    def test_the_placeholder_does_not_encode_the_length(self):
        short, _ = redact_text(FAKE_OPENAI_KEY)
        long, _ = redact_text(FAKE_JWT)
        assert short == long == PLACEHOLDER

    def test_the_report_names_the_rule_and_never_the_value(self):
        _, redaction = redact_text(f"key={FAKE_OPENAI_KEY}")
        assert redaction.rules == ("openai",)
        # Not even a prefix. A "first four characters" hint is a real attack
        # against a short credential.
        assert not any(FAKE_OPENAI_KEY[:4] in rule for rule in redaction.rules)


class TestSensitiveKeys:
    @pytest.mark.parametrize(
        "key",
        [
            "authorization",
            "Authorization",
            "api_key",
            "x-api-key",
            "password",
            "cookie",
            "http.request.header.authorization",
            "auth-token",
        ],
    )
    def test_a_sensitive_key_is_recognised(self, key: str):
        assert is_sensitive_key(key) is True

    @pytest.mark.parametrize("key", ["model", "route", "environment", "operation", "author"])
    def test_an_ordinary_key_is_not(self, key: str):
        assert is_sensitive_key(key) is False

    def test_the_value_of_a_sensitive_key_goes_whatever_it_looks_like(self):
        # A value under `authorization` is a credential even when it does not
        # match any vendor's key shape.
        cleaned, redaction = redact_structure({"authorization": "some-internal-scheme abc123"})
        assert cleaned["authorization"] == PLACEHOLDER
        assert "sensitive-key" in redaction.rules


class TestTheAssembledArtefact:
    """The class of bug that has bitten this series before.

    A pass over the *inputs* leaves anything derived from them carrying what
    the pass removed. So every one of these asserts against the finished
    document, serialised, rather than against one field of it.
    """

    @pytest.fixture
    def poisoned_window(self):
        return build_window(
            [
                make_span(
                    index=0,
                    attributes={
                        "base_url": f"https://api.example/v1?key={FAKE_OPENAI_KEY}",
                        "authorization": FAKE_BEARER,
                        "route": "/chat",
                    },
                    error_type="auth_failed",
                    outcome="error",
                )
            ],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )

    @pytest.fixture
    def objectives(self) -> ObjectiveSet:
        return parse_objectives(
            "objectives: [{name: a, kind: availability, target: 0.99, min_samples: 1}]",
            source="<test>",
        )

    def test_nothing_reaches_the_json_report(self, poisoned_window, objectives):
        document = json_report.render(evaluate(poisoned_window, objectives))
        for credential in (FAKE_OPENAI_KEY, FAKE_BEARER):
            assert credential not in document

    def test_nothing_reaches_the_junit_report(self, poisoned_window, objectives):
        document = junit.render(evaluate(poisoned_window, objectives))
        for credential in (FAKE_OPENAI_KEY, FAKE_BEARER):
            assert credential not in document

    def test_nothing_reaches_the_markdown_summary(
        self, poisoned_window, objectives, pricebook: PriceBook
    ):
        document = markdown.render(
            evaluate(poisoned_window, objectives),
            cost=cost_window(poisoned_window, pricebook),
        )
        for credential in (FAKE_OPENAI_KEY, FAKE_BEARER):
            assert credential not in document

    def test_nothing_reaches_the_written_window(self, poisoned_window, tmp_path: Path):
        JsonlExporter(tmp_path / "w.jsonl").export(poisoned_window)
        written = (tmp_path / "w.jsonl").read_text(encoding="utf-8")
        for credential in (FAKE_OPENAI_KEY, FAKE_BEARER):
            assert credential not in written

    def test_nothing_reaches_an_otlp_payload(self, poisoned_window):
        cleaned, _ = redact_structure(otlp_payload(poisoned_window))
        serialised = json.dumps(cleaned)
        for credential in (FAKE_OPENAI_KEY, FAKE_BEARER):
            assert credential not in serialised

    def test_the_report_carries_no_span_attributes_at_all(self, poisoned_window, objectives):
        # Stated precisely rather than aspirationally. The credentials above do
        # not reach the JSON report because *no* span attribute does: the report
        # is objectives, burn rates and digests. That is a stronger guarantee
        # than redaction, and it is the reason the tests above pass — so it is
        # asserted directly rather than left to be inferred from them.
        document = json.loads(json_report.render(evaluate(poisoned_window, objectives)))
        assert "route" not in json.dumps(document)
        assert "base_url" not in json.dumps(document)

    def test_the_pass_is_still_wired_up_where_something_does_reach_the_report(self, objectives):
        # The window's own source *is* carried through, into the metadata, and
        # a path can be a URL with a credential in it. This is the assertion
        # that would fail if the redaction call were deleted — the ones above
        # would not.
        window = build_window(
            [make_span()],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
            source=f"https://svc:{FAKE_OPENAI_KEY}@telemetry.example/windows/today.jsonl",
        )

        document = json.loads(json_report.render(evaluate(window, objectives)))

        assert FAKE_OPENAI_KEY not in json.dumps(document)
        assert "telemetry.example" in json.dumps(document)
        # Silent redaction is indistinguishable from redaction that did not
        # run, and the count is the only way a reader can tell.
        assert document["redaction"]["count"] >= 1

    def test_a_clean_report_says_it_removed_nothing(self, objectives):
        window = build_window(
            [make_span(attributes={"route": "/chat"})],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        document = json.loads(json_report.render(evaluate(window, objectives)))
        assert document["redaction"] == {"count": 0, "rules": []}


class TestUntrustedInput:
    def test_a_control_character_does_not_break_the_junit_file(self):
        # An error_type copied from a provider's response can contain anything,
        # and a report that will not parse is worse than no report.
        window = build_window(
            [make_span(outcome="error", error_type="bad\x00\x08thing")],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        objectives = parse_objectives(
            "objectives: [{name: a, kind: availability, target: 0.5, min_samples: 1}]",
            source="<test>",
        )
        from xml.etree import ElementTree

        ElementTree.fromstring(junit.render(evaluate(window, objectives)))

    def test_a_pipe_in_an_objective_name_does_not_break_a_markdown_table(self):
        objectives = parse_objectives(
            'objectives: [{name: "a|b|c", kind: availability, target: 0.99}]',
            source="<test>",
        )
        window = build_window([make_span()], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        row = next(
            line
            for line in markdown.render(evaluate(window, objectives)).splitlines()
            if line.startswith("| a")
        )
        # Count the pipes that still separate cells: an escaped one does not,
        # which is the whole point of escaping it.
        assert row.replace("\\|", "").count("|") == 6
