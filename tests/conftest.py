"""Shared fixtures.

Credential-shaped strings are built by concatenation — ``"AKIA" + "IOSFO..."`` —
so a scanner reading this repository does not report a fixture as a finding, and
so a human can see at a glance that nothing here was ever valid.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from llmops.cost.pricebook import PriceBook, parse_pricebook
from llmops.slo.objective import ObjectiveSet, parse_objectives
from llmops.telemetry.span import Operation, Outcome, Span
from llmops.telemetry.window import Window, build_window

# -- inert credential-shaped fixtures ------------------------------------

FAKE_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_GITHUB_TOKEN = "ghp_" + "a" * 36
FAKE_OPENAI_KEY = "sk-" + "b" * 32
FAKE_ANTHROPIC_KEY = "sk-ant-" + "c" * 40
FAKE_BEARER = "Bearer " + "d" * 40
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9." + "e" * 20 + "." + "f" * 20

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"
WINDOWS = EXAMPLES / "windows"

#: A fixed instant every fixture is anchored to. Not ``now``: a test whose
#: outcome depends on the wall clock is a test that fails at midnight.
ORIGIN = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def make_span(
    *,
    index: int = 0,
    at: datetime | None = None,
    provider: str = "openai",
    model: str = "gpt-4o-mini",
    operation: Operation = "chat",
    outcome: Outcome = "ok",
    duration_ms: float = 500.0,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    **extra: Any,
) -> Span:
    """One span, with everything defaulted to something uninteresting."""
    fields: dict[str, Any] = {
        "span_id": f"s{index:06d}",
        "trace_id": f"t{index:06d}",
        "started_at": at if at is not None else ORIGIN,
        "duration_ms": duration_ms,
        "provider": provider,
        "model": model,
        "operation": operation,
        "outcome": outcome,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    # A failing span needs an error_type to be a realistic one, and a span that
    # already carries one keeps it. Set here rather than in the signature so a
    # caller passing `error_type=` does not collide with the default.
    if outcome != "ok" and "error_type" not in extra:
        fields["error_type"] = "upstream_error"
    return Span(**fields, **extra)


@pytest.fixture
def span_factory() -> Callable[..., Span]:
    """Build a span with sensible defaults."""
    return make_span


def spread(
    count: int,
    *,
    over: timedelta,
    ending_at: datetime = ORIGIN,
    bad: int = 0,
    start_index: int = 0,
    outcome: Outcome = "ok",
    **kwargs: Any,
) -> list[Span]:
    """*count* spans evenly spaced over *over*, ending just before *ending_at*.

    ``bad`` of them fail, and they are the **last** ones — so a short window at
    the end sees them and a long window sees them diluted, which is exactly the
    shape the two-window conjunction is designed to distinguish.

    ``outcome`` sets what the *other* spans are, for a test about refusals.
    """
    step = over / count
    spans: list[Span] = []
    for offset in range(count):
        at = ending_at - over + step * offset
        is_bad = offset >= count - bad
        spans.append(
            make_span(
                index=start_index + offset,
                at=at,
                outcome="error" if is_bad else outcome,
                **kwargs,
            )
        )
    return spans


@pytest.fixture
def window_factory() -> Callable[..., Window]:
    """Build a window from spans, inferring nothing the caller did not say."""

    def build(
        spans: list[Span],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Window:
        return build_window(spans, start=start, end=end, source="<test>")

    return build


SIMPLE_PRICES = textwrap.dedent(
    """
    name: test-rates
    prices:
      - provider: openai
        model: gpt-4o-mini
        input_per_million: 1.0
        output_per_million: 2.0
      - provider: openai
        model: gpt-4o
        input_per_million: 10.0
        output_per_million: 30.0
    """
)


@pytest.fixture
def pricebook() -> PriceBook:
    """Round numbers, so an expected cost can be worked out by hand."""
    return parse_pricebook(SIMPLE_PRICES, source="<test>")


SIMPLE_OBJECTIVES = textwrap.dedent(
    """
    name: test
    objectives:
      - name: availability
        kind: availability
        target: 0.99
        period: 30d
        min_samples: 10
    """
)


@pytest.fixture
def objectives() -> ObjectiveSet:
    """One availability objective with the standard rules."""
    return parse_objectives(SIMPLE_OBJECTIVES, source="<test>")


@pytest.fixture
def example_window_path() -> Path:
    """The healthy window shipped in the repository."""
    return WINDOWS / "steady.jsonl.gz"


@pytest.fixture
def example_objectives_path() -> Path:
    """The shipped objectives."""
    return EXAMPLES / "objectives.yaml"


@pytest.fixture
def example_prices_path() -> Path:
    """The shipped price book."""
    return EXAMPLES / "prices.yaml"
