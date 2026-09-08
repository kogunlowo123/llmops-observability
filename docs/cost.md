# Cost accounting

Turning token counts into dollars, in a way that survives being checked against
an invoice.

```bash
llmops cost --window telemetry.jsonl.gz --pricebook prices.yaml --by model
```

```
$6.41581706 over 5609 call(s), no provider-reported costs to reconcile against
```

## Money is `Decimal`

End to end. No float appears anywhere in the cost path.

The reason is not pedantry. A single call to a cheap model on a few hundred
tokens costs a fraction of a cent; a month of them costs four figures. Binary
floating point accumulates error across exactly that kind of summation, and the
result is a total that disagrees with the invoice by an amount nobody can
account for — which destroys the credibility of the whole report, because a
number that cannot be reconciled is a number that will not be trusted next time
either.

For the same reason the CLI prints **full precision**, not two decimal places. A
per-call cost rounded to a cent is zero for most calls, and a total assembled
from zeroes is zero.

## The price book

Rates in USD **per million tokens**, as vendors publish them:

```yaml
name: example-rates

prices:
  - provider: openai
    model: gpt-4o-mini
    input_per_million: 0.15
    output_per_million: 0.60
    cached_input_per_million: 0.075
    valid_from: 2026-01-01
    note: illustrative
```

Per million because that is the unit on the vendor's page. A price book in
dollars-per-token is a price book somebody transcribed, and a transcription is a
place to make a mistake by three orders of magnitude.

### Cached input is a rate, not a discount

`cached_input_per_million` is its own number, not `input_per_million × 0.25`.
Again: that is the figure the vendor publishes, so it is the figure a person
will check. A discount factor is one more thing to be silently wrong, and it
stops being right the moment a vendor changes the ratio.

A span may declare `cached_input_tokens`; they are billed at the cached rate and
the remainder at the full one. `cached_input_tokens > input_tokens` is refused
at validation — cached input is a subset, not an extra.

### Operation-specific rates win

```yaml
  - provider: openai
    model: text-embedding-3-small
    operation: embedding
    input_per_million: 0.02
```

An embedding costs about a fiftieth of a chat turn. Falling back to a chat rate
for one would make the whole spend report indefensible, so a more specific entry
beats a general one.

### Rates are dated

`valid_from` is a date, and a span is costed at the rate in force when it
happened. Costing last quarter with this quarter's rates produces a number that
is not a fact about last quarter. The example book keeps a superseded `gpt-4o`
rate for exactly this reason.

### A price book is content-addressed

Every cost report carries `pricebook_digest`. A number can be traced back to the
rates that produced it, months later, without trusting a commit message.

> The rates shipped in `examples/prices.yaml` are **illustrative**. They are in
> the right order of magnitude for the models named and were not copied from any
> vendor's page. Check the real ones before costing anything you care about.

## An unpriced model is a hard failure

Costing a span whose model is absent from the price book raises
`UnpricedModelError` and exits **3**. There is no fallback rate and no flag to
suppress it. This is [ADR-002](../ARCHITECTURE.md), and it is the decision in
this repository most likely to be argued with, so the argument:

The model missing from the price book is, by construction, the newest one. It is
therefore the most expensive one, deployed most recently, by the team most
likely to be surprised by its bill. Charging it zero produces a spend report
that is confidently, silently, arbitrarily low — and a budget gate that goes
green on the exact day it should go red.

Exit **3**, not 2: this is "the tool could not run", not "the budget was
burned". A pipeline that conflates them is one where a stale price book looks
exactly like a healthy platform.

## Reconciliation

When a span carries the provider's own `reported_cost_usd`, both numbers are
computed and compared.

```
$412.88 over 43,014 call(s), 43,014 reconciled, 3 discrepant
```

A span is called discrepant only when **both** tolerances are exceeded:

| Tolerance | Default |
| --- | --- |
| Relative | 1% |
| Absolute | $0.000005 |

Both, not either. A relative tolerance alone makes every sub-cent call a
discrepancy, because rounding at the eighth decimal place is a large fraction of
a tiny number. An absolute tolerance alone lets a 40% error on an expensive call
through. Requiring both means a finding is a real disagreement about real money.

And when there is nothing to compare against, the summary says so:

```
no provider-reported costs to reconcile against
```

rather than `0 discrepancies`. Zero discrepancies over zero comparisons reads as
a clean bill of health and is evidence of nothing at all. A computed cost that
has never been checked against a bill is an estimate wearing a number's clothes.

## Grouping

```bash
llmops cost --window w.jsonl.gz --pricebook prices.yaml --by model
llmops cost --window w.jsonl.gz --pricebook prices.yaml --by operation
llmops cost --window w.jsonl.gz --pricebook prices.yaml --by outcome
```

```json
{
  "dimension": "model",
  "calls": 5609,
  "total_usd": 6.41581706,
  "pricebook_digest": "sha256:b02fcb3a...",
  "window_digest": "sha256:e6f6662d...",
  "lines": [
    {"key": "openai/gpt-4o", "calls": 779, "input_tokens": 691936, "output_tokens": 326504, "cost_usd": 4.92653}
  ],
  "reconciliation": {"compared": 0, "reported_total_usd": 0.0, "discrepancies": []}
}
```

`--by outcome` is the interesting one during an incident: most providers bill a
timeout, so an outage has a cost as well as an availability consequence, and
seeing them in one table is what makes that concrete.

## Attaching cost to a gate

```bash
llmops gate --window w.jsonl.gz --objectives objectives.yaml --pricebook prices.yaml --with-cost
```

A spend objective needs `--pricebook` regardless; `--with-cost` additionally
puts the breakdown in the report and the Markdown summary. Long tables are
truncated by the renderer rather than dumped: a job summary with two hundred
rows is one nobody reads.

## See also

- [Objectives](objectives.md) — declaring a spend budget
- [Burn rate](burnrate.md) — why spend uses the same engine, and where that
  breaks down under diurnal traffic
- [Windows](windows.md) — the token counts this reads
