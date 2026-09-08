# Telemetry windows

The file format everything else reads, and why it is shaped the way it is.

## A span

One call to a model. Every field is either something the platform bills for,
something an objective measures, or something a report groups by. Nothing else
is allowed in.

```json
{
  "span_id": "steady-00000036",
  "trace_id": "t00000036",
  "started_at": "2026-09-01T00:01:19.989069+00:00",
  "duration_ms": 55.612,
  "provider": "openai",
  "model": "text-embedding-3-small",
  "operation": "embedding",
  "outcome": "ok",
  "input_tokens": 138,
  "output_tokens": 0,
  "attributes": {"environment": "prod", "route": "/chat"}
}
```

| Field | Notes |
| --- | --- |
| `span_id` | Unique within the window; up to 64 characters |
| `trace_id` | Optional; defaults to `unset` |
| `started_at` | **Must** carry an offset. A naive timestamp is refused |
| `duration_ms` | Wall clock for the call |
| `provider`, `model` | The pair a price book is keyed on |
| `operation` | `chat`, `embedding`, `completion`, … — what a selector matches |
| `outcome` | `ok`, `refusal`, `error`, `timeout` |
| `input_tokens`, `output_tokens` | Billable counts |
| `cached_input_tokens` | Optional; may not exceed `input_tokens` |
| `reported_cost_usd` | Optional; the provider's own figure, for reconciliation |
| `error_type` | Only permitted when the outcome is not successful |
| `attributes` | At most 32 keys, at most 1024 bytes each |

### What is deliberately absent

**There is no field for the prompt or the completion.** `extra="forbid"` means
adding one is rejected at validation rather than carried through. This is the
most effective privacy control available to a telemetry pipeline and it costs
nothing: there is no configuration under which this tool exports a prompt,
because there is nowhere to put one. See ADR-001 and the
[threat model](../THREAT-MODEL.md).

### A refusal is a success

`outcome: refusal` counts as a successful call. The platform responded; a policy
declined the request. An availability objective that treats refusals as outages
rewards a team for weakening its safety filter to make a graph look better, and
that is a bad incentive to build into a gate.

`Span.succeeded` is where this is decided, in one place.

### Validation refuses the impossible

- A naive `started_at` — an instant without an offset is not an instant.
- `cached_input_tokens > input_tokens` — cached input is a subset, not an extra.
- An `error_type` on a successful span — one of the two is wrong, and guessing
  which would corrupt every count downstream.
- More than 32 attributes, or one over 1024 bytes — an attribute bag becoming a
  transcript is how prompts leak.

## A window

The spans in a half-open interval, plus the lines the reader could not parse.

```
{"llmops_window": 1, "start": "...", "end": "...", "spans": 5609, "digest": "sha256:...", "metadata": {...}}
{"span_id": "...", ...}
{"span_id": "...", ...}
```

### Half-open, `[start, end)`

A span starting exactly at `end` belongs to the next window. Closed intervals
double-count the boundary span, and that surfaces a week later as a spend report
that will not reconcile by exactly one call — an afternoon to find, and no fun
at all.

### JSON Lines, not a JSON array

A telemetry file is appended to by a process that can be killed. A truncated
JSON array is unreadable *in its entirety*; a truncated JSONL file has lost
exactly its last line.

So the reader **counts and reports** lines it could not parse rather than
failing:

```json
"unreadable_lines": 12
```

and every renderer states that above the numbers, not in a footnote below them.
A verdict computed over 97% of the evidence has to say so.

A malformed **header** is fatal. Without it there is no interval, and every
number in the report would be over an interval the tool guessed.

### The digest is over the spans, not the bytes

```python
material = "\n".join(sorted(span.fingerprint() for span in self.spans))
digest = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
```

Reordering the lines, reformatting the JSON, adding a key to the metadata or
compressing the file does not change what any report computes. A drift check
that fired on those is a check people learn to ignore, and a check people ignore
is worse than none because it looks like coverage.

`Span.fingerprint()` covers only the billable and measurable facts, for the same
reason.

## Compression

A path ending `.gz` is gzipped — on the way out and on the way in, decided by
the suffix and never by sniffing the content.

```bash
llmops synth --scenario outage --out telemetry.jsonl.gz
llmops gate --window telemetry.jsonl.gz --objectives objectives.yaml
```

Telemetry is the most compressible data a platform produces: every line repeats
the same dozen keys and the same handful of model names. The corpus in this
repository shrinks by a factor of eleven — 620 KB instead of 7.1 MB — which is
why it is committed that way.

Two details that matter:

**The gzip member carries a zero timestamp.** By default gzip stamps the current
time into its header, so writing the same window twice would produce different
bytes and every regenerated corpus would differ from the committed one for a
reason no reader could see.

**The size limit applies to the decompressed stream.** Checking `stat()` alone
would accept a 200 KB file that expands to forty gigabytes. Without the stream
guard the runner is killed, and that reaches an operator as "the gate is flaky"
rather than as an attack.

Sniffing the magic bytes was considered and rejected. It is friendlier to a
mislabelled file and worse for everything else: it teaches people to ship gzip
named `.jsonl`, and then something downstream that reads by name gets bytes it
cannot parse.

## Limits

| Limit | Value | Why |
| --- | --- | --- |
| `MAX_WINDOW_BYTES` | 256 MB | Windows are read into memory whole. Applied to the decompressed stream too |
| `MAX_LINE_BYTES` | 256 KB | A longer line is not a span; it is a different kind of file |
| Attributes per span | 32 | An attribute bag is not a transcript |
| Bytes per attribute | 1024 | Same |

## Working with windows in code

```python
from llmops.telemetry.window import read_window

window = read_window("telemetry.jsonl.gz")

window.total  # spans
window.failed  # spans that did not succeed
window.duration  # end - start
window.models()  # every (provider, model) pair, sorted
window.digest()  # content address

hour = window.ending_at(window.end, timedelta(hours=1))
```

`slice()` and `ending_at()` are what the burn-rate engine is built on: a long
window and a short one, over the same spans, with no re-reading.

## Generating one

Every window in `examples/` was synthesised by this tool and says so in its own
header:

```json
"metadata": {"generated": "true", "note": "Synthesised telemetry. Nothing here was captured from a real service.", "scenario": "steady", "seed": "20260908"}
```

See [ADR-005](../ARCHITECTURE.md) for why, and `llmops synth --help` for how.

## See also

- [Objectives](objectives.md) — what gets measured over a window
- [Cost](cost.md) — turning tokens into dollars
- [Cardinality](cardinality.md) — why `attributes` is a bounded bag
