# Objectives

An objectives file is the contract the gate enforces. It is a small YAML
document, it lives in the repository next to the code it guards, and it is
reviewed like code — because lowering a target is the easiest way to make a
failing gate pass.

## A complete file

```yaml
name: example-platform

objectives:
  - name: chat-availability
    kind: availability
    description: Chat calls answer. A safety refusal counts as an answer.
    target: 0.999
    period: 30d
    select:
      operation: chat

  - name: chat-latency
    kind: latency
    description: 99% of chat calls answer within five seconds.
    target: 0.99
    threshold_ms: 5000
    period: 30d
    select:
      operation: chat

  - name: monthly-spend
    kind: spend
    description: The platform costs no more than $80 a month.
    budget_usd: 80
    period: 30d
    rules:
      - {name: fast-burn, severity: page, long_window: 1d, short_window: 6h, burn_rate: 2}
```

`examples/objectives.yaml` is this file with the reasoning left in the comments.
Each of the four shipped scenarios breaks exactly one of its objectives, which
is the argument that they measure different things rather than three views of
one signal.

## The three kinds

### `availability`

The fraction of in-scope calls that succeeded. `target: 0.999` allows one call
in a thousand to fail over the period.

A **refusal counts as a success**: the platform responded, and a policy declined
the request. An objective that treats refusals as outages rewards a team for
weakening its safety filter.

### `latency`

The fraction of in-scope calls faster than `threshold_ms`.

Phrased as *"99% of calls under 5s"* rather than *"p99 under 5s"* on purpose.
The first has an error budget — the 1% that were not — and slots directly into
the burn-rate machinery. The second needs a percentile estimator over a sliding
window, which is a considerably worse thing to build a gate on: it is
approximate, its error depends on the distribution, and two implementations of
it disagree.

**A failed call is not a slow call.** `is_bad` counts a span only when it
succeeded *and* exceeded the threshold. Counting failures as slow would mean an
outage silently consumes the latency budget too, and the two objectives would
stop being independent measurements of different things.

### `spend`

Dollars against `budget_usd` over `period`. Needs `--pricebook`.

The burn rate is spend measured against the window's **pro-rata share** of the
period budget. Six hours of a 30-day $80 budget is $0.667, so spending $2 in
those six hours is 3× — the same number, on the same scale, as three times the
acceptable rate of failed calls. That is what lets one engine serve all three
kinds (ADR-004).

A spend objective is the only one that sees a prompt change which triples output
length: no errors, no latency signal, and an invoice that doubles.

> Spend has a genuine limitation under diurnal traffic, and the shipped spend
> objective overrides the default rules because of it. The effect is measured
> and explained in [burn rate](burnrate.md#where-the-table-stops-applying).

## Keys

| Key | Applies to | Notes |
| --- | --- | --- |
| `name` | all | Required. Findings are reported by it |
| `kind` | all | `availability`, `latency` or `spend` |
| `description` | all | Shown in the Markdown summary. Worth writing |
| `target` | availability, latency | `0 < target < 1`, exclusive |
| `threshold_ms` | latency | Required and positive |
| `budget_usd` | spend | Required and positive |
| `period` | all | `30d`, `7d`, `24h`. Defaults to `30d` |
| `select` | all | Exact-equality matches; omitted means all traffic |
| `rules` | all | Overrides the standard table |
| `min_samples` | all | Overrides the 30-sample floor |

**Unknown keys are refused, not ignored.** A misspelt `threshold_ms` would
otherwise leave the threshold at zero, which makes every call slow and every
window an outage — a gate that fails loudly for a reason nobody can find.

**`target: 1.0` is refused** with an explanation. A 100% objective has a zero
error budget: every burn rate is a division by zero and the only honest report
is that one failure consumed everything. Saying so at parse time is kinder than
saying it during an incident. Use `0.9999`.

## Selectors

```yaml
select:
  operation: chat
  environment: prod
```

Every key must match, exactly. `provider`, `model`, `operation` and `outcome`
are read from the span itself; anything else is looked up in the attribute bag.

**There is no pattern syntax** — no globs, no regular expressions, no negation.
That is deliberate. A selector is the thing that decides which traffic an
objective is about, and a selector nobody can evaluate by reading it is a
selector that silently covers the wrong calls. Exact equality is checkable at a
glance; if you need two shapes of traffic, write two objectives.

The report states each objective's scope in words — `operation=chat`, or `all
traffic` — so a reader can see what was measured without re-deriving it.

## Rules

Omit `rules` and the objective gets the [standard four-rule
table](burnrate.md#the-default-table). Supply them and you replace it entirely:

```yaml
rules:
  - name: fast-burn
    severity: page          # page | ticket
    long_window: 6h
    short_window: 30m       # optional; defaults to long_window / 12
    burn_rate: 14.4
    min_samples: 30         # optional
```

Both windows must exceed `burn_rate` for the rule to fire. Overriding is the
right move in exactly two situations, both shown in
`examples/objectives.yaml` with the reasoning attached: a spend objective under
diurnal traffic, and a low-volume service that cannot be watched as closely.

## Reading the verdict

```
BREACHED  3/4 objective(s) held
  PAGE  chat-availability: fast-burn: FIRING — burn 229.17x over 1h and 400.00x over 5m, threshold 14.4x
  note  chat-availability: slow-burn not covered — the rule looks back 1d and only 22.6667h of history precedes 2026-09-01T22:40:00+00:00
```

- `PAGE` / `TICKET` — a rule fired. The exit code is **2**.
- `note` — a rule reached no verdict, because the window was under-sampled or
  did not go back far enough.

A `note` is **not** a pass. "We did not look" and "we looked and it was fine"
are different facts, and every renderer keeps them in separate sections: JSON,
JUnit (a `skipped` case, not a passing one) and Markdown alike.

## The objectives digest

Every report carries a content address over the objectives that produced it:

```json
"objectives_digest": "sha256:..."
```

It covers each objective's name, kind, target, period, threshold, budget,
selector and rules — that is, everything that changes a verdict, and nothing
that does not. A report and the file that produced it can be matched up months
later without trusting a commit message.

## Limits

| Limit | Value |
| --- | --- |
| File size | 1 MB |
| Objectives per file | 64 |

Parsing is `yaml.safe_load`. Nothing in an objectives file is evaluated,
imported or executed.

## See also

- [Burn rate](burnrate.md) — the arithmetic, and where the table stops applying
- [Cost](cost.md) — what a spend objective is measuring
- [CI](ci.md) — wiring the verdict into a pipeline
