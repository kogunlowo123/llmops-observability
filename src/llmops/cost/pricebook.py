"""What a call costs, and where that number comes from.

The central decision in this module is that **an unpriced model is a hard
failure**. It would be easy to cost it at zero and carry on; every tool that
does this produces a spend report that is quietly wrong, and wrong in one
specific direction, because the model missing from the price book is by
construction the newest one — which is the expensive one, and the one somebody
turned on last week without telling finance.

So: `llmops cost` over a window containing an unpriced model exits 3 and names
it. Not exit 2 — nothing was measured, so nothing was overspent as far as this
tool can prove.

Three further things the format takes seriously.

**Prices are per million tokens**, as every vendor publishes them, and are
parsed as :class:`~decimal.Decimal`. Money is not a float. Summing a hundred
thousand spans priced in binary floating point accumulates an error that shows
up as a report which disagrees with itself between two groupings of the same
spans, and a reviewer will find that before you do.

**A price book is dated and content-addressed.** Prices change; a report over
last quarter's window computed with this quarter's prices is not a fact about
last quarter. The digest goes in the report so the two can never be silently
mixed up, and `valid_from` lets one file carry a model's history.

**Cached input is a separate rate, not a discount factor.** Providers publish it
as its own price, and expressing it as a multiplier means storing a number that
is not the one on the vendor's page — which is the number a person will check.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from llmops.errors import PriceBookError, UnpricedModelError
from llmops.telemetry.span import Operation, Span

#: Vendors publish per-million-token prices, so that is what the file holds. A
#: file in per-token units would be full of numbers like 2.5e-06, which nobody
#: can check against a pricing page at a glance.
TOKENS_PER_UNIT = Decimal(1_000_000)

#: A price book is a small committed file.
MAX_PRICEBOOK_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Price:
    """What one model costs, from one date."""

    provider: str
    model: str
    #: USD per million input tokens.
    input_per_million: Decimal
    #: USD per million output tokens.
    output_per_million: Decimal
    #: USD per million input tokens the provider served from its own cache.
    #: Defaults to the full input rate: assuming a discount nobody wrote down
    #: understates a bill, and this tool errs towards the number that prompts a
    #: question rather than the one that passes quietly.
    cached_input_per_million: Decimal | None = None
    #: A flat charge per call, for providers that levy one.
    per_call_usd: Decimal = Decimal(0)
    valid_from: date = date(1970, 1, 1)
    operation: Operation | None = None

    def cost(self, span: Span) -> Decimal:
        """Return what *span* cost at this price."""
        cached_rate = (
            self.cached_input_per_million
            if self.cached_input_per_million is not None
            else self.input_per_million
        )
        billable = Decimal(span.billable_input_tokens) * self.input_per_million
        cached = Decimal(span.cached_input_tokens) * cached_rate
        output = Decimal(span.output_tokens) * self.output_per_million
        return (billable + cached + output) / TOKENS_PER_UNIT + self.per_call_usd


class PriceBook:
    """Prices for the models a window contains."""

    def __init__(self, prices: list[Price], *, source: str = "<memory>", name: str = "") -> None:
        self.source = source
        self.name = name
        # Newest first, so the first entry whose `valid_from` has passed is the
        # one that applied. A linear scan over a handful of dated entries beats
        # an index that has to be kept correct.
        self._prices = sorted(
            prices,
            key=lambda price: (price.provider, price.model, price.valid_from),
            reverse=True,
        )

    def __len__(self) -> int:
        return len(self._prices)

    @property
    def prices(self) -> tuple[Price, ...]:
        """Every entry, newest first."""
        return tuple(self._prices)

    def price_for(self, span: Span) -> Price:
        """Return the price that applied to *span*.

        Raises :class:`UnpricedModelError` rather than returning ``None``. A
        caller who has to remember to check a return value is a caller who will
        one day forget, and the failure mode of forgetting is a zero.
        """
        when = span.started_at.date()
        operation_match: Price | None = None
        general_match: Price | None = None
        for price in self._prices:
            if price.provider != span.provider or price.model != span.model:
                continue
            if price.valid_from > when:
                continue
            if price.operation == span.operation and operation_match is None:
                operation_match = price
            elif price.operation is None and general_match is None:
                general_match = price
        # An operation-specific entry wins: embeddings are an order of magnitude
        # cheaper than chat, and falling back to the chat rate for them is the
        # kind of error that makes a whole report indefensible.
        found = operation_match or general_match
        if found is None:
            raise UnpricedModelError(
                f"no price for {span.provider}/{span.model} on {when.isoformat()}.",
                remedy=(
                    f"Add it to the price book ({self.source}). An unpriced model is "
                    "refused rather than costed at zero, because the model missing "
                    "from a price book is usually the newest and most expensive one."
                ),
            )
        return found

    def cost(self, span: Span) -> Decimal:
        """Return what *span* cost."""
        return self.price_for(span).cost(span)

    def digest(self) -> str:
        """Return a content address over the rates.

        Covers what a cost is computed from — provider, model, dates, rates —
        and not the file's comments or ordering. Recorded in every report, so a
        number can always be traced to the prices that produced it.
        """
        material = "\n".join(
            sorted(
                "␟".join(
                    [
                        price.provider,
                        price.model,
                        price.operation or "*",
                        price.valid_from.isoformat(),
                        str(price.input_per_million),
                        str(price.output_per_million),
                        str(price.cached_input_per_million or ""),
                        str(price.per_call_usd),
                    ]
                )
                for price in self._prices
            )
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_pricebook(path: str | Path) -> PriceBook:
    """Read a price book from YAML."""
    file = Path(path)
    if not file.is_file():
        raise PriceBookError(
            f"the price book {str(file)!r} could not be opened.",
            remedy="Check the path. An example ships in examples/prices.yaml.",
        )
    size = file.stat().st_size
    if size > MAX_PRICEBOOK_BYTES:
        raise PriceBookError(
            f"the price book is {size} bytes; the limit is {MAX_PRICEBOOK_BYTES}.",
            remedy="A price book is a list of rates, not a dataset.",
        )
    return parse_pricebook(file.read_text(encoding="utf-8"), source=str(file))


def parse_pricebook(text: str, *, source: str = "<string>") -> PriceBook:
    """Parse a price book.

    ``yaml.safe_load`` only. A price book is a file a CI job reads from a
    repository, and the full loader constructs arbitrary Python objects.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PriceBookError(
            f"the price book is not valid YAML: {exc}.",
            remedy="Check indentation and quoting around the failing line.",
        ) from exc
    if not isinstance(raw, dict):
        raise PriceBookError(
            "a price book is a mapping with a 'prices' list.",
            remedy="See examples/prices.yaml.",
        )

    entries = raw.get("prices")
    if not isinstance(entries, list) or not entries:
        raise PriceBookError(
            "the price book has no 'prices' entries.",
            remedy="An empty price book prices nothing, which is not the same as free.",
        )

    prices = [_price(entry, index=index) for index, entry in enumerate(entries)]
    return PriceBook(prices, source=source, name=str(raw.get("name") or ""))


def _price(entry: Any, *, index: int) -> Price:
    where = f"prices[{index}]"
    if not isinstance(entry, dict):
        raise PriceBookError(f"{where} is not a mapping.", remedy="Each entry is a mapping.")

    known = {
        "provider",
        "model",
        "input_per_million",
        "output_per_million",
        "cached_input_per_million",
        "per_call_usd",
        "valid_from",
        "operation",
        "note",
    }
    unknown = sorted(set(entry) - known)
    if unknown:
        # Refused rather than ignored: a misspelt `cached_input_per_milion`
        # would otherwise silently charge cached tokens at the full rate.
        raise PriceBookError(
            f"{where} has unknown key(s): {', '.join(unknown)}.",
            remedy=f"Known keys are {', '.join(sorted(known))}.",
        )

    provider = _required_str(entry, "provider", where=where)
    model = _required_str(entry, "model", where=where)
    operation = entry.get("operation")
    if operation is not None and operation not in {"chat", "completion", "embedding", "rerank"}:
        raise PriceBookError(
            f"{where} has an unknown operation {operation!r}.",
            remedy="Known operations are chat, completion, embedding, rerank.",
        )

    return Price(
        provider=provider,
        model=model,
        input_per_million=_money(entry, "input_per_million", where=where, required=True),
        output_per_million=_money(entry, "output_per_million", where=where, required=True),
        cached_input_per_million=_optional_money(entry, "cached_input_per_million", where=where),
        per_call_usd=_money(entry, "per_call_usd", where=where, required=False),
        valid_from=_valid_from(entry.get("valid_from"), where=where),
        operation=operation,
    )


def _required_str(entry: dict[str, Any], key: str, *, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PriceBookError(f"{where} has no {key!r}.", remedy=f"{key} is a non-empty string.")
    return value.strip()


def _money(entry: dict[str, Any], key: str, *, where: str, required: bool) -> Decimal:
    value = entry.get(key)
    if value is None:
        if required:
            raise PriceBookError(
                f"{where} has no {key!r}.",
                remedy="Rates are USD per million tokens, as vendors publish them.",
            )
        return Decimal(0)
    return _decimal(value, key=key, where=where)


def _optional_money(entry: dict[str, Any], key: str, *, where: str) -> Decimal | None:
    value = entry.get(key)
    return None if value is None else _decimal(value, key=key, where=where)


def _decimal(value: Any, *, key: str, where: str) -> Decimal:
    # `str(value)` rather than `Decimal(value)` on a float: YAML parses 2.5 to a
    # binary float, and Decimal(2.5) is 2.5000000000000004440892098500626, which
    # then appears in a report as a rate nobody typed.
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceBookError(
            f"{where} has a non-numeric {key!r}: {value!r}.",
            remedy="Rates are numbers in USD per million tokens.",
        ) from exc
    if parsed < 0:
        raise PriceBookError(
            f"{where} has a negative {key!r}.",
            remedy="A negative rate is a credit, which this tool does not model.",
        )
    return parsed


def _valid_from(value: Any, *, where: str) -> date:
    if value is None:
        return date(1970, 1, 1)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise PriceBookError(
                f"{where} has an unparseable valid_from: {value!r}.",
                remedy="Use an ISO date, for example 2026-01-01.",
            ) from exc
    raise PriceBookError(
        f"{where} has an unusable valid_from: {value!r}.",
        remedy="Use an ISO date, for example 2026-01-01.",
    )
