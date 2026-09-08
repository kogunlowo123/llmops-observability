# Cardinality

Why a metric label is refused before the metric exists, which labels are
refused, and what to use instead.

```bash
llmops labels --metric llm_requests --label model --label tenant_id
```

```
metric 'llm_requests' has unbounded label(s): tenant_id.
  'tenant_id' -> bucket it as 'tenant_tier'.
```

Exit **3**.

## The failure this prevents

Adding `tenant_id` to a metric's labels is a one-line change that passes review.
It creates one time series per tenant. With fifty tenants nothing happens. At
five thousand the metrics backend starts dropping writes, queries that used to
take a second take a minute, and somebody spends a day working out which of the
last three months of commits did it.

The cost is not linear either — labels **multiply**:

```
llm_requests_total{provider, model, route, service}
  = 20 × 200 × 200 × 50
  = 40,000,000 series
```

Every one of those is bounded. Not one of them looks wrong on its own. That is
the case a reviewer checking labels one at a time cannot catch, and it is why
the check is arithmetic rather than a list of forbidden names.

## Checked at registration, not at emit

This is the decision worth arguing about, and it is [ADR-006](../ARCHITECTURE.md).

Validating a label *value* as it arrives is too late. By then the metric exists,
the code that emits it is deployed, and the series are already being created —
the damage is done in the same instant you detect it. Refusing the *declaration*
means the failure happens in a unit test on a laptop, with a message naming the
label, its cardinality and its replacement.

It is the same shape as two other decisions in this codebase: an exporter that
cannot be **constructed** without `--allow-network`, and the evaluation harness
elsewhere in this series that enforces hermeticity at construction rather than
detecting a network call after the fact. Making the invalid state
unrepresentable beats detecting it.

## The registry

```python
from llmops.cardinality.budget import DEFAULT_LABELS, LabelRegistry

registry = LabelRegistry(DEFAULT_LABELS)
spec = registry.check("llm_requests_total", ["provider", "model", "outcome"])
spec.series  # 20 * 200 * 5 = 20000  -> refused against the 1000 budget
```

The default series budget is **1000** per metric, configurable with
`LLMOPS_GATE__SERIES_BUDGET`.

Cardinality is a property of the *data*, decided once, and not something each
metric negotiates for itself — which is why the label registry is deliberately
separate from any metric registry. Re-registering a name with a different
cardinality is refused: two answers to the question means the optimistic one
wins by accident.

### Labels known to be bounded

| Label | Cardinality |
| --- | --- |
| `provider` | 20 |
| `model` | 200 |
| `operation` | 4 |
| `outcome` | 5 |
| `environment` | 8 |
| `service` | 50 |
| `route` | 200 |
| `le` | 16 |

`le` is the histogram bucket boundary — bounded because the boundaries are
declared up front, which is the entire reason histograms are safe and raw
durations are not.

### Unregistered labels fail closed

A label nobody has declared is refused. The alternative — assuming an unknown
label is fine — means the guard is only as good as somebody's memory, and the
label that gets forgotten is by definition the unusual one.

## Known-unbounded labels, and what to use instead

The guard does not merely say no. Each of these maps to the bucketed
alternative:

| Refused | Use instead | Why |
| --- | --- | --- |
| `user_id` | `user_tier` | One series per user |
| `tenant_id` | `tenant_tier` | One series per tenant |
| `session_id` | `session_kind` | Unbounded and short-lived, the worst combination |
| `request_id`, `trace_id`, `span_id` | `route` | One series per request |
| `prompt`, `prompt_hash` | `prompt_template` | Also a privacy problem |
| `url`, `path`, `query` | `route` | The templated route, not the instance |
| `duration_ms`, `latency_ms` | `le` | A continuous value is infinite cardinality; bucket it |
| `cost_usd` | `cost_bucket` | Same |
| `completion`, `input`, `output`, `email`, `ip`, `timestamp` | — nothing | These do not belong in a metric label at all |

The last row has no replacement on purpose. Some of these are unbounded *and*
personal data, and the honest advice is to remove them rather than to bucket
them.

**Bucketing is the way out, and the guard says so.** A rejected metric is
usually a metric that needs one dimension replaced, not deleted:

```
REFUSED  llm_requests_total{provider, model, route, service}
         metric can produce 40000000 series; the budget is 1000.
         Drop a dimension or narrow one. The largest is 'model' at 200.

ok       llm_requests_total{model, tenant_tier} - up to 800 series
```

Naming the largest dimension matters: it is the difference between "this is
refused" and "here is what to change".

## The corpus obeys its own rule

Every attribute on every span in `examples/` is a registered, bounded label.
There is a test that asserts it. A tool that refuses unbounded labels while
shipping a corpus full of them would be making an argument it does not believe.

## In a report

`llmops gate` can attach the metric specs it validated:

```json
"metrics": [{"name": "llm_requests_total", "labels": ["model", "outcome"], "max_series": 1000}]
```

so a review can see the series budget a change is spending, alongside the error
budget it is spending.

## Runnable

```bash
python examples/cardinality_demo.py
```

Walks the whole argument: a metric that looks fine, the multiplication that
makes it not fine, the bucketed version that passes, and why the check runs
where it does.

## See also

- [Windows](windows.md) — why `attributes` is a bounded bag on the span too
- [Threat model](../THREAT-MODEL.md) — the privacy half of the same problem
