"""Costing a window, and reconciling against the provider's own numbers."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from llmops.cost.pricebook import PriceBook
from llmops.cost.report import cost_window, total_cost, unpriced_models
from llmops.errors import UnpricedModelError
from llmops.telemetry.span import Span
from llmops.telemetry.window import Window, build_window
from tests.conftest import ORIGIN, make_span, spread

pytestmark = pytest.mark.integration


def window(*spans: Span) -> Window:
    return build_window(list(spans), start=ORIGIN, end=ORIGIN + timedelta(hours=1))


class TestBreakdowns:
    def test_the_total_is_the_sum_of_the_lines(self, pricebook: PriceBook):
        report = cost_window(
            window(
                make_span(index=0, model="gpt-4o-mini"),
                make_span(index=1, model="gpt-4o"),
                make_span(index=2, model="gpt-4o"),
            ),
            pricebook,
        )
        assert sum((line.cost_usd for line in report.lines), Decimal(0)) == report.total_usd

    @pytest.mark.parametrize(
        ("dimension", "expected"),
        [
            ("model", {"openai/gpt-4o", "openai/gpt-4o-mini"}),
            ("provider", {"openai"}),
            ("operation", {"chat"}),
            ("outcome", {"ok"}),
        ],
    )
    def test_each_dimension_groups_by_the_right_key(
        self, pricebook: PriceBook, dimension, expected
    ):
        report = cost_window(
            window(
                make_span(index=0, model="gpt-4o-mini"),
                make_span(index=1, model="gpt-4o"),
            ),
            pricebook,
            dimension=dimension,
        )
        assert {line.key for line in report.lines} == expected

    def test_the_same_spans_cost_the_same_under_any_grouping(self, pricebook: PriceBook):
        # A report that disagrees with itself between two groupings is the
        # symptom of costing in binary floating point.
        spans = window(
            make_span(index=0, model="gpt-4o-mini"),
            make_span(index=1, model="gpt-4o"),
            make_span(index=2, model="gpt-4o", operation="completion"),
        )
        by_model = cost_window(spans, pricebook, dimension="model").total_usd
        by_operation = cost_window(spans, pricebook, dimension="operation").total_usd
        assert by_model == by_operation

    def test_the_report_records_which_prices_produced_it(self, pricebook: PriceBook):
        # So a number can always be traced to the rates it came from.
        report = cost_window(window(make_span()), pricebook)
        assert report.pricebook_digest == pricebook.digest()

    def test_a_line_carries_its_token_counts(self, pricebook: PriceBook):
        report = cost_window(
            window(
                make_span(index=0, input_tokens=100, output_tokens=10),
                make_span(index=1, input_tokens=200, output_tokens=20),
            ),
            pricebook,
        )
        assert report.lines[0].input_tokens == 300
        assert report.lines[0].output_tokens == 30


class TestReconciliation:
    def test_agreement_is_not_a_discrepancy(self, pricebook: PriceBook):
        span = make_span(input_tokens=1_000_000, output_tokens=0, reported_cost_usd=1.0)
        report = cost_window(window(span), pricebook)
        assert report.reconciled == 1
        assert report.discrepancies == ()

    def test_a_material_disagreement_is_reported(self, pricebook: PriceBook):
        # The provider says $2, this tool computes $1. A vendor rate changed
        # and nobody updated the price book.
        span = make_span(input_tokens=1_000_000, output_tokens=0, reported_cost_usd=2.0)
        report = cost_window(window(span), pricebook)
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].difference_usd == Decimal("-1.0")

    def test_a_rounding_difference_is_not(self, pricebook: PriceBook):
        # A fraction alone would flag every sub-cent call, because rounding at
        # the fourth decimal is a large relative error on a small number.
        span = make_span(input_tokens=1, output_tokens=0, reported_cost_usd=0.000002)
        assert cost_window(window(span), pricebook).discrepancies == ()

    def test_a_large_bill_drifting_by_a_fraction_is_reported(self, pricebook: PriceBook):
        # And an absolute floor alone would let this pass.
        span = make_span(input_tokens=100_000_000, output_tokens=0, reported_cost_usd=120.0)
        assert len(cost_window(window(span), pricebook).discrepancies) == 1

    def test_a_provider_reporting_zero_for_a_billable_call_is_reported(self, pricebook: PriceBook):
        # The most interesting case there is, and dividing by the reported cost
        # would hide it.
        span = make_span(input_tokens=1_000_000, output_tokens=0, reported_cost_usd=0.0)
        report = cost_window(window(span), pricebook)
        assert len(report.discrepancies) == 1
        assert report.discrepancies[0].relative == Decimal(1)

    def test_nothing_to_reconcile_is_said_out_loud(self, pricebook: PriceBook):
        # "0 discrepancies" over 0 comparisons reads as a clean bill of health
        # and is nothing of the kind.
        report = cost_window(window(make_span()), pricebook)
        assert report.reconciled == 0
        assert "no provider-reported costs" in report.summary()

    def test_the_summary_says_how_many_were_compared(self, pricebook: PriceBook):
        report = cost_window(window(make_span(reported_cost_usd=0.0014)), pricebook)
        assert "1 reconciled" in report.summary()


class TestUnpricedModels:
    def test_costing_raises_on_the_first_one(self, pricebook: PriceBook):
        with pytest.raises(UnpricedModelError):
            cost_window(window(make_span(model="gpt-5")), pricebook)

    def test_the_diagnostic_lists_all_of_them(self, pricebook: PriceBook):
        # Being told about one missing model at a time is four round trips
        # instead of one.
        missing = unpriced_models(
            window(
                make_span(index=0, model="gpt-5"),
                make_span(index=1, model="gpt-6"),
                make_span(index=2, model="gpt-4o-mini"),
            ),
            pricebook,
        )
        assert missing == ("openai/gpt-5", "openai/gpt-6")

    def test_a_fully_priced_window_lists_nothing(self, pricebook: PriceBook):
        assert unpriced_models(window(make_span()), pricebook) == ()


class TestTheShippedCorpus:
    def test_every_model_in_the_healthy_window_is_priced(
        self, example_window_path, example_prices_path
    ):
        # The claim the README's spend figure depends on. If a model were
        # unpriced the command would exit 3, and the figure would be a fiction.
        from llmops.cost.pricebook import load_pricebook
        from llmops.telemetry.window import read_window

        assert (
            unpriced_models(read_window(example_window_path), load_pricebook(example_prices_path))
            == ()
        )

    def test_the_embedding_rate_is_the_one_that_applies(
        self, example_window_path, example_prices_path
    ):
        # An embedding costs a fiftieth of a chat turn. If the general rate
        # were picked instead, every spend number would be indefensible.
        from llmops.cost.pricebook import load_pricebook
        from llmops.telemetry.window import read_window

        prices = load_pricebook(example_prices_path)
        window_ = read_window(example_window_path)
        embedding = next(span for span in window_ if span.operation == "embedding")
        assert prices.price_for(embedding).operation == "embedding"


class TestTotalCost:
    def test_it_sums_what_a_budget_objective_reads(self, pricebook: PriceBook):
        spans = spread(10, over=timedelta(hours=1), input_tokens=1_000_000, output_tokens=0)
        assert total_cost(spans, pricebook) == Decimal(10)

    def test_an_empty_iterable_costs_nothing(self, pricebook: PriceBook):
        assert total_cost([], pricebook) == Decimal(0)
