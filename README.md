# llmops-observability

**LLM telemetry you can gate on.** Cost accounting against a versioned price
book, multi-window burn-rate error budgets, and a command that exits non-zero
when an objective is missed.

[![CI](https://github.com/kogunlowo123/llmops-observability/actions/workflows/ci.yml/badge.svg)](https://github.com/kogunlowo123/llmops-observability/actions/workflows/ci.yml)
[![Security](https://github.com/kogunlowo123/llmops-observability/actions/workflows/security.yml/badge.svg)](https://github.com/kogunlowo123/llmops-observability/actions/workflows/security.yml)
[![Container](https://github.com/kogunlowo123/llmops-observability/actions/workflows/docker.yml/badge.svg)](https://github.com/kogunlowo123/llmops-observability/actions/workflows/docker.yml)
[![Docs](https://github.com/kogunlowo123/llmops-observability/actions/workflows/pages.yml/badge.svg)](https://kogunlowo123.github.io/llmops-observability/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Most LLM observability tells you what happened. This tells you whether what
happened was acceptable, and it exits `2` when it was not.

```bash
llmops gate --window telemetry.jsonl --objectives objectives.yaml
echo $?   # 0 held, 2 an objective was missed, 3 the tool could not run
```

## Why this exists

Three things go wrong on an LLM platform, and the usual dashboard catches none
of them until somebody notices by hand.

**The bill.** A prompt change triples output length on the expensive model. No
errors, no latency change, and the monthly invoice doubles. A tool that costs
each call from a reviewed price book — and *refuses* to cost a model it has no
price for, rather than silently charging zero — turns that into a build failure
on the day it happens.

**The slow burn.** A single-threshold alert on an error rate is either deaf or
hysterical: alert at 0.1% for five minutes and a two-minute total outage goes
unnoticed while a harmless blip pages someone at 04:00. Burn-rate alerting
measures how fast the *error budget* is being consumed, over two windows at
once, and it is the thing that actually works.

**The metrics outage you caused yourself.** Labelling a metric with `tenant_id`
looks harmless in review and creates one time series per tenant. This refuses
that metric at registration, and names the bucketed label to use instead.

## What it does

### Gate on an error budget

```bash
llmops gate \
  --window examples/windows/outage.jsonl.gz \
  --objectives examples/objectives.yaml \
  --at 2026-09-01T22:40:00+00:00 \
  --junit-out reports/slo.xml \
  --markdown-out reports/slo.md
```

```
BREACHED  3/4 objective(s) held
  PAGE  chat-availability: fast-burn: FIRING — burn 229.17x over 1h and 400.00x over 5m, threshold 14.4x
  PAGE  chat-availability: moderate-burn: FIRING — burn 25.94x over 6h and 379.31x over 30m, threshold 6x
  note  chat-availability: slow-burn not covered — the rule looks back 1d and only 22.6667h of history precedes 2026-09-01T22:40:00+00:00
```

`--at` is there because the incident is *over* by the end of the window, which
is correct and is the whole point of the short window. Rules look backwards from
the instant given. The `note` lines are rules that reached no verdict, reported
separately because "we did not look" and "we looked and it was fine" are
different facts.

Objectives are declared in a file that is reviewed like code:

```yaml
objectives:
  - name: chat-availability
    kind: availability
    target: 0.999          # three nines over the period
    period: 30d
    select: { operation: chat }

  - name: chat-latency
    kind: latency
    target: 0.99           # 99% of calls under the threshold
    threshold_ms: 3000
    period: 30d

  - name: monthly-spend
    kind: spend
    budget_usd: 4000
    period: 30d
```

All three use the same engine. A spend objective burns dollars against a
pro-rata share of the budget, which puts it on exactly the scale the
availability rules use — so the conventional thresholds apply unchanged and
there is one implementation of the two-window conjunction, not two.

### Cost a window, and check the answer

```bash
llmops cost --window examples/windows/steady.jsonl.gz --pricebook examples/prices.yaml --by model
```

```
$6.41581706 over 5609 call(s), no provider-reported costs to reconcile against
```

Full cents, not two decimal places: a per-call cost rounded to a cent is zero
for most of them, and a total assembled from zeroes is zero. Money is `Decimal`
end to end for the same reason.

When a span carries the provider's own `reported_cost_usd`, the two numbers are
compared and a systematic divergence is a finding. A computed cost that has
never been checked against a bill is an estimate wearing a number's clothes.

### Refuse a metric before it exists

```bash
llmops labels --metric llm_requests --label model --label tenant_id
```

```
metric 'llm_requests' has unbounded label(s): tenant_id.
  'tenant_id' -> bucket it as 'tenant_tier'.
```

The check is at registration, not at emit. By the time a bad label *value*
arrives the metric exists, the code is deployed, and the series are already
being created.

## The telemetry is generated, and says so

Every window in `examples/` was **synthesised by this tool**, not captured from
a real service. That is stated here rather than left to be discovered, and
`llmops synth` is a shipped, documented command rather than a script that ran
once:

```bash
llmops synth --scenario outage --out telemetry.jsonl
llmops check --scenario outage --window telemetry.jsonl   # still matches?
```

The committed windows are gzipped — a path ending `.gz` is compressed on the
way out and decompressed on the way in, decided by the suffix and by nothing
else. Telemetry is the most compressible data a platform produces, and these
shrink by a factor of eleven: 620 KB in the tree instead of 7.1 MB.

It is the right trade for what this repository has to show. A generated window
can contain an incident of a known size at a known instant, which is what makes
it possible to assert that a rule fires *at the right moment* rather than merely
that it fires. Four scenarios ship: a healthy platform, a sharp outage, a
latency degradation with no errors at all, and a cost spike with no availability
or latency signal.

## Install

```bash
uv sync --locked --group dev
uv run llmops doctor
```

Or the container, which runs as a non-root user:

```bash
docker build -t llmops:local .
docker run --rm -v "$PWD:/data" llmops:local \
  gate --window /data/telemetry.jsonl --objectives /data/objectives.yaml
```

## In CI

```yaml
- name: Error budget
  run: |
    llmops gate \
      --window telemetry/last-24h.jsonl \
      --objectives slo/objectives.yaml \
      --pricebook slo/prices.yaml \
      --junit-out reports/slo.xml
```

JUnit XML means a breached objective appears next to the unit tests, with the
same red mark, and nobody has to be taught a new dashboard to notice that the
platform is on fire.

## Design

Four decisions a reader is most likely to question, all recorded in
[ARCHITECTURE.md](ARCHITECTURE.md):

- **An unpriced model is a hard failure, not a zero** (ADR-002). The model
  missing from a price book is by construction the newest and most expensive.
- **The conventional burn-rate table, not one invented here** (ADR-003). Four
  rules, two windows each, both required to fire.
- **Spend and availability are one engine** (ADR-004), because a pro-rata budget
  share puts dollars on the same scale as failed calls.
- **The corpus is synthesised, deterministically, and the tool says so**
  (ADR-005).

Four runtime dependencies. The OTLP exporter speaks HTTP/JSON over stdlib
`urllib` rather than pulling in a tracing SDK to send one POST.

## Guarantees, stated precisely

- **A span has no prompt or completion field.** There is nowhere to put one.
  That is the most effective privacy control available to a telemetry pipeline
  and it costs nothing. Redaction over the attribute bag is a second line of
  defence, not the only one. See [THREAT-MODEL.md](THREAT-MODEL.md).
- **Nothing leaves the machine unless you say so.** An exporter that opens a
  socket cannot be *constructed* without `--allow-network`.
- **A rule that was not evaluated is reported as such**, never as passing. "We
  did not look" and "we looked and it was fine" are different facts.

## Documentation

| Page | What is in it |
| --- | --- |
| [Windows](docs/windows.md) | The telemetry format, and why it is JSON Lines |
| [Objectives](docs/objectives.md) | Declaring targets, selectors and rules |
| [Burn rate](docs/burnrate.md) | The arithmetic, the table, and detection times |
| [Cost](docs/cost.md) | Price books, cached tokens, reconciliation |
| [Cardinality](docs/cardinality.md) | Which labels are refused, and what to use instead |
| [CI](docs/ci.md) | Wiring the gate into a pipeline |

## License

MIT. See [LICENSE](LICENSE).
