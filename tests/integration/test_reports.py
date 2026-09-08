"""The three report shapes, and especially the sections nobody sees until a red build."""

from __future__ import annotations

import json
import textwrap
from dataclasses import replace
from datetime import timedelta
from xml.etree import ElementTree

import pytest

from llmops.cost.pricebook import PriceBook
from llmops.cost.report import cost_window
from llmops.report import json_report, junit, markdown
from llmops.slo.evaluate import evaluate
from llmops.slo.objective import ObjectiveSet, parse_objectives
from llmops.telemetry.window import build_window
from tests.conftest import ORIGIN, make_span, spread

pytestmark = pytest.mark.integration


def objectives(text: str) -> ObjectiveSet:
    return parse_objectives(textwrap.dedent(text), source="<test>")


BREAKING = """
    objectives:
      - name: availability
        kind: availability
        target: 0.99
        min_samples: 10
        description: Calls answer.
        rules:
          - {name: fast-burn, severity: page, long_window: 1h, short_window: 5m, burn_rate: 5}
    """


@pytest.fixture
def red():
    """An evaluation with a firing rule."""
    window = build_window(
        spread(300, over=timedelta(hours=1), ending_at=ORIGIN, bad=100),
        start=ORIGIN - timedelta(hours=1),
        end=ORIGIN,
    )
    return evaluate(window, objectives(BREAKING))


@pytest.fixture
def green():
    window = build_window(
        spread(300, over=timedelta(hours=1), ending_at=ORIGIN),
        start=ORIGIN - timedelta(hours=1),
        end=ORIGIN,
    )
    return evaluate(window, objectives(BREAKING))


class TestJson:
    def test_the_verdict_is_the_first_key(self, green):
        # A script that greps rather than parses should be able to answer the
        # only question that matters without reading the whole document.
        document = json_report.render(green)
        assert next(iter(json.loads(document))) == "passed"

    def test_a_cost_report_can_be_attached(self, green, pricebook: PriceBook):
        window = build_window([make_span()], start=ORIGIN, end=ORIGIN + timedelta(hours=1))
        document = json.loads(json_report.render(green, cost=cost_window(window, pricebook)))
        assert document["cost"]["calls"] == 1

    def test_metric_specs_can_be_attached(self, green):
        from llmops.cardinality.budget import DEFAULT_LABELS, LabelRegistry

        spec = LabelRegistry(DEFAULT_LABELS).check("m", ["provider"])
        document = json.loads(json_report.render(green, metrics=(spec,)))
        assert document["metrics"][0]["name"] == "m"

    def test_the_report_is_valid_json_with_an_infinite_burn(self):
        # json.dumps writes `Infinity`, which is not valid JSON. A zero error
        # budget produces one, and a report a parser rejects is no report.
        window = build_window(
            spread(30, over=timedelta(hours=1), ending_at=ORIGIN, bad=30),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        from llmops.slo.burnrate import BurnRule, WindowMeasurement, decide
        from llmops.slo.evaluate import Evaluation, ObjectiveResult

        rule = BurnRule(name="r", severity="page", long_window=timedelta(hours=1), burn_rate=1.0)
        result = decide(
            rule,
            WindowMeasurement.from_events(length=timedelta(hours=1), samples=30, bad=1, budget=0.0),
            WindowMeasurement.from_events(
                length=timedelta(minutes=5), samples=30, bad=1, budget=0.0
            ),
        )
        evaluation = Evaluation(
            window_digest=window.digest(),
            objectives_digest="sha256:x",
            evaluated_at=ORIGIN,
            results=(
                ObjectiveResult(
                    objective=objectives(BREAKING).objectives[0],
                    results=(result,),
                    in_scope=30,
                ),
            ),
        )
        json.loads(json_report.render(evaluation))


class TestJUnit:
    def test_it_parses_as_xml(self, red):
        assert ElementTree.fromstring(junit.render(red)).tag == "testsuite"

    def test_a_breached_objective_is_a_failing_case(self, red):
        suite = ElementTree.fromstring(junit.render(red))
        case = next(item for item in suite if item.tag == "testcase")
        assert case.get("name") == "availability"
        assert [child.tag for child in case] == ["failure"]

    def test_a_held_objective_has_no_failure(self, green):
        suite = ElementTree.fromstring(junit.render(green))
        case = next(item for item in suite if item.tag == "testcase")
        assert list(case) == []

    def test_an_objective_with_no_evaluable_rule_is_skipped_not_passed(self):
        # The distinction the whole file exists for.
        window = build_window(
            spread(30, over=timedelta(hours=1), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        evaluation = evaluate(
            window,
            objectives(
                """
                objectives:
                  - name: unmeasurable
                    kind: availability
                    target: 0.99
                    rules:
                      - {name: month, long_window: 30d, burn_rate: 1}
                """
            ),
        )
        suite = ElementTree.fromstring(junit.render(evaluation))
        assert suite.get("skipped") == "1"
        case = next(item for item in suite if item.tag == "testcase")
        assert [child.tag for child in case] == ["skipped"]

    def test_the_properties_record_what_was_measured(self, red):
        suite = ElementTree.fromstring(junit.render(red))
        block = suite.find("properties")
        assert block is not None
        properties = {item.get("name"): item.get("value") for item in block}
        digest = properties["window_digest"]
        assert digest is not None
        assert digest.startswith("sha256:")


class TestMarkdownWhenTheNewsIsBad:
    """The sections a reader only ever sees on a red build.

    The least-exercised paths in the renderer and the ones that matter most: a
    job summary that breaks its own table, or silently omits the reason, is
    worse than no summary, because a reader concludes there was no reason.
    """

    def test_the_verdict_is_the_first_line(self, red):
        assert markdown.render(red).splitlines()[0] == "# Error budget: BREACHED"

    def test_a_held_evaluation_says_so(self, green):
        assert markdown.render(green).splitlines()[0] == "# Error budget: held"

    def test_the_reason_comes_before_the_tables(self, red):
        text = markdown.render(red)
        assert text.index("What broke") < text.index("Every objective")

    def test_the_firing_rule_is_named_with_both_windows(self, red):
        text = markdown.render(red)
        assert "`fast-burn`" in text
        assert "| page |" in text

    def test_the_description_is_shown_when_there_is_one(self, red):
        assert "Calls answer." in markdown.render(red)

    def test_a_damaged_window_is_declared_above_the_numbers(self, red):
        # A verdict computed over 97% of the evidence has to say so above the
        # numbers, not in a footnote below them.
        damaged = replace(red, unreadable=12)
        text = markdown.render(damaged)
        assert "12 line(s) of the window could not be read" in text
        assert text.index("could not be read") < text.index("Every objective")

    def test_a_rule_that_reached_no_verdict_gets_its_own_section(self):
        window = build_window(
            spread(30, over=timedelta(hours=1), ending_at=ORIGIN),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        evaluation = evaluate(
            window,
            objectives(
                """
                objectives:
                  - name: unmeasurable
                    kind: availability
                    target: 0.99
                    rules:
                      - {name: month, long_window: 30d, burn_rate: 1}
                """
            ),
        )
        text = markdown.render(evaluation)
        assert "## Not evaluated" in text
        assert "`month`" in text

    def test_an_under_sampled_rule_is_listed_too(self):
        window = build_window(
            spread(5, over=timedelta(hours=1), ending_at=ORIGIN, bad=5),
            start=ORIGIN - timedelta(hours=1),
            end=ORIGIN,
        )
        text = markdown.render(evaluate(window, objectives(BREAKING)))
        assert "## Not evaluated" in text
        assert "only 5 call(s)" in text

    def test_the_spend_table_is_rendered(self, green, pricebook: PriceBook):
        window = build_window(
            [make_span(index=0), make_span(index=1, model="gpt-4o")],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        text = markdown.render(green, cost=cost_window(window, pricebook))
        assert "## Spend" in text
        assert "openai/gpt-4o" in text

    def test_disagreements_with_the_provider_are_tabulated(self, green, pricebook: PriceBook):
        window = build_window(
            [make_span(input_tokens=1_000_000, output_tokens=0, reported_cost_usd=50.0)],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        text = markdown.render(green, cost=cost_window(window, pricebook))
        assert "Where this disagrees with the provider" in text

    def test_a_long_spend_table_is_truncated_rather_than_dumped(self, green, pricebook: PriceBook):
        # A summary with two hundred rows is one nobody reads, and the renderer
        # should choose where to stop rather than leaving it to the page.
        window = build_window(
            [make_span(index=index) for index in range(markdown.LIST_LIMIT + 5)],
            start=ORIGIN,
            end=ORIGIN + timedelta(hours=1),
        )
        text = markdown.render(green, cost=cost_window(window, pricebook, dimension="outcome"))
        assert "## Spend" in text

    def test_the_footer_records_what_was_measured(self, red):
        text = markdown.render(red)
        assert "window `sha256:" in text
        assert "objectives `sha256:" in text
