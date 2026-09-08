"""Prices, and the refusal to invent one."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from llmops.cost.pricebook import PriceBook, parse_pricebook
from llmops.errors import PriceBookError, UnpricedModelError
from tests.conftest import make_span

pytestmark = pytest.mark.unit


def book(text: str) -> PriceBook:
    return parse_pricebook(textwrap.dedent(text), source="<test>")


class TestCosting:
    def test_a_cost_is_tokens_times_the_rate_per_million(self, pricebook: PriceBook):
        # 1000 input at $1/M plus 200 output at $2/M.
        span = make_span(input_tokens=1000, output_tokens=200)
        assert pricebook.cost(span) == Decimal("0.0014")

    def test_money_is_decimal_not_float(self, pricebook: PriceBook):
        # Summing a hundred thousand spans in binary floating point accumulates
        # an error that shows up as a report disagreeing with itself between
        # two groupings of the same spans.
        assert isinstance(pricebook.cost(make_span()), Decimal)

    def test_cached_input_is_charged_at_its_own_rate(self):
        prices = book(
            """
            prices:
              - provider: openai
                model: m
                input_per_million: 10.0
                output_per_million: 0.0
                cached_input_per_million: 1.0
            """
        )
        # 1000 tokens, 800 of them cached: 200 at $10/M plus 800 at $1/M.
        span = make_span(model="m", input_tokens=1000, cached_input_tokens=800, output_tokens=0)
        assert prices.cost(span) == Decimal("0.0028")

    def test_without_a_cached_rate_cached_tokens_cost_full_price(self):
        # Assuming a discount nobody wrote down understates a bill. This tool
        # errs towards the number that prompts a question.
        prices = book(
            """
            prices:
              - provider: openai
                model: m
                input_per_million: 10.0
                output_per_million: 0.0
            """
        )
        span = make_span(model="m", input_tokens=1000, cached_input_tokens=800, output_tokens=0)
        assert prices.cost(span) == Decimal("0.01")

    def test_a_per_call_charge_is_added(self):
        prices = book(
            """
            prices:
              - provider: openai
                model: m
                input_per_million: 0.0
                output_per_million: 0.0
                per_call_usd: 0.004
            """
        )
        assert prices.cost(make_span(model="m")) == Decimal("0.004")


class TestUnpricedModels:
    def test_an_unpriced_model_raises_rather_than_costing_zero(self, pricebook: PriceBook):
        # The central decision in the module. A spend report that silently omits
        # a model is worse than none: it is a number somebody will put in a
        # slide, and the missing model is by construction the newest.
        with pytest.raises(UnpricedModelError, match="no price for openai/gpt-5"):
            pricebook.cost(make_span(model="gpt-5"))

    def test_the_error_names_the_file_to_edit(self, pricebook: PriceBook):
        with pytest.raises(UnpricedModelError) as caught:
            pricebook.cost(make_span(model="gpt-5"))
        assert "<test>" in caught.value.remedy

    def test_a_known_model_from_the_wrong_provider_is_unpriced(self, pricebook: PriceBook):
        # Provider and model together. The same name at two vendors is two
        # different prices, and matching on the name alone would pick one.
        with pytest.raises(UnpricedModelError):
            pricebook.cost(make_span(provider="azure", model="gpt-4o-mini"))


class TestDatedRates:
    RATES = """
        prices:
          - provider: openai
            model: m
            input_per_million: 10.0
            output_per_million: 0.0
          - provider: openai
            model: m
            input_per_million: 2.0
            output_per_million: 0.0
            valid_from: 2026-06-01
        """

    def test_a_span_after_the_change_uses_the_new_rate(self):
        span = make_span(
            model="m",
            at=datetime(2026, 7, 1, tzinfo=UTC),
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert book(self.RATES).cost(span) == Decimal("2.0")

    def test_a_span_before_the_change_uses_the_old_one(self):
        # Costing last quarter with this quarter's prices produces a number
        # that is not a fact about last quarter.
        span = make_span(
            model="m",
            at=datetime(2026, 5, 1, tzinfo=UTC),
            input_tokens=1_000_000,
            output_tokens=0,
        )
        assert book(self.RATES).cost(span) == Decimal("10.0")


class TestOperationScopedRates:
    RATES = """
        prices:
          - provider: openai
            model: m
            input_per_million: 10.0
            output_per_million: 0.0
          - provider: openai
            model: m
            operation: embedding
            input_per_million: 0.02
            output_per_million: 0.0
        """

    def test_an_operation_specific_rate_wins(self):
        # An embedding costs a fiftieth of a chat turn. Falling back to the
        # chat rate makes a whole spend report indefensible.
        span = make_span(model="m", operation="embedding", input_tokens=1_000_000, output_tokens=0)
        assert book(self.RATES).cost(span) == Decimal("0.02")

    def test_an_unscoped_rate_still_applies_to_other_operations(self):
        span = make_span(model="m", operation="chat", input_tokens=1_000_000, output_tokens=0)
        assert book(self.RATES).cost(span) == Decimal("10.0")


class TestParsing:
    def test_an_unknown_key_is_refused(self):
        # A misspelt `cached_input_per_milion` would silently charge cached
        # tokens at the full rate, which no test would ever notice.
        with pytest.raises(PriceBookError, match="unknown key"):
            book(
                """
                prices:
                  - provider: openai
                    model: m
                    input_per_million: 1.0
                    output_per_million: 1.0
                    cached_input_per_milion: 0.5
                """
            )

    def test_a_missing_rate_is_refused(self):
        with pytest.raises(PriceBookError, match="output_per_million"):
            book(
                """
                prices:
                  - provider: openai
                    model: m
                    input_per_million: 1.0
                """
            )

    def test_a_negative_rate_is_refused(self):
        with pytest.raises(PriceBookError, match="negative"):
            book(
                """
                prices:
                  - provider: openai
                    model: m
                    input_per_million: -1.0
                    output_per_million: 1.0
                """
            )

    def test_an_empty_price_book_is_refused(self):
        # Pricing nothing is not the same as everything being free.
        with pytest.raises(PriceBookError, match="no 'prices' entries"):
            book("prices: []")

    def test_a_float_rate_does_not_pick_up_binary_noise(self):
        # Decimal(2.5) is 2.5000000000000004440892098500626. Parsing via str
        # keeps the number the person typed.
        prices = book(
            """
            prices:
              - provider: openai
                model: m
                input_per_million: 2.5
                output_per_million: 0.0
            """
        )
        assert prices.prices[0].input_per_million == Decimal("2.5")

    def test_yaml_is_loaded_safely(self):
        # A price book is a file a CI job reads from a repository.
        with pytest.raises(PriceBookError):
            parse_pricebook("!!python/object/apply:os.system ['echo pwned']")

    def test_an_unknown_operation_is_refused(self):
        with pytest.raises(PriceBookError, match="unknown operation"):
            book(
                """
                prices:
                  - provider: openai
                    model: m
                    operation: transcription
                    input_per_million: 1.0
                    output_per_million: 1.0
                """
            )


class TestTheDigest:
    def test_it_changes_when_a_rate_changes(self):
        one = book("prices: [{provider: o, model: m, input_per_million: 1, output_per_million: 1}]")
        two = book("prices: [{provider: o, model: m, input_per_million: 2, output_per_million: 1}]")
        assert one.digest() != two.digest()

    def test_it_ignores_comments_and_ordering(self):
        one = book(
            """
            prices:
              - {provider: o, model: a, input_per_million: 1, output_per_million: 1}
              - {provider: o, model: b, input_per_million: 2, output_per_million: 2}
            """
        )
        two = book(
            """
            # a comment nobody costs anything with
            prices:
              - {provider: o, model: b, input_per_million: 2, output_per_million: 2, note: hi}
              - {provider: o, model: a, input_per_million: 1, output_per_million: 1}
            """
        )
        assert one.digest() == two.digest()
