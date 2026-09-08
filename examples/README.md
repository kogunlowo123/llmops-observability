# Examples

Everything in this directory is generated, runnable, and checked by CI. Nothing
here was captured from a real service.

## What is here

| File | What it is |
| --- | --- |
| `objectives.yaml` | Four objectives, with the reasoning left in the comments |
| `prices.yaml` | An illustrative price book, with a superseded rate kept on purpose |
| `windows/*.jsonl.gz` | Four synthesised telemetry windows, three days each |
| `quickstart.py` | Read a window, evaluate it, print the verdict |
| `burnrate_demo.py` | The table, the two-window conjunction, and detection times |
| `cardinality_demo.py` | Why a metric is refused, and what to use instead |

The Python files are documentation that executes. CI runs all three on every
push, because an example that stops working is a README that lies.

## The windows

```bash
llmops gate --window windows/steady.jsonl.gz --objectives objectives.yaml --pricebook prices.yaml
```

| Scenario | What it shows | Breaks |
| --- | --- | --- |
| `steady` | A healthy platform | nothing |
| `outage` | A sharp failure spike | `chat-availability` |
| `slowdown` | Latency degradation with **no errors at all** | `chat-latency` |
| `cost-spike` | A context leak with no availability or latency signal | `monthly-spend` |

Each breaks **exactly one** objective, and that is the point. Three objectives
that all went red together would be three views of one signal rather than three
measurements of different things. `python tasks.py check-incidents` asserts it —
including that the healthy window is still green at the same instant, which is
the half that carries the argument.

`slowdown` and `cost-spike` are the interesting ones. A platform that is up,
correct, slow and expensive is invisible to an availability objective, and a
prompt change that triples output length produces no error and no latency
signal at all.

## They are synthesised, and they say so

Every window's header carries it:

```json
"metadata": {
  "generated": "true",
  "note": "Synthesised telemetry. Nothing here was captured from a real service.",
  "scenario": "steady",
  "seed": "20260908"
}
```

Real telemetry cannot be published — it is someone's traffic, and scrubbing it
well enough to publish is a larger and less certain job than generating it. A
generated window is also strictly better for what this repository has to
demonstrate: it contains an incident of a known size at a known instant, which
is what makes it possible to assert that a rule fires *at the right moment*
rather than merely that it fires. See ADR-005.

## Regenerating them

```bash
python tasks.py corpus          # regenerate all four
python tasks.py corpus-check    # do the committed ones still match?
```

The generator is deterministic: the same scenario and seed produce the same
spans on every machine, forever. `llmops check` compares **digests** rather than
bytes, because a check that fires on a re-encoding is a check people learn to
ignore.

## Why they are gzipped

620 KB in the tree instead of 7.1 MB, for the same 22,000 spans.

Telemetry is the most compressible data a platform produces — every line repeats
the same dozen keys and the same handful of model names — so these shrink by a
factor of eleven. A path ending `.gz` is compressed on the way out and
decompressed on the way in, decided by the suffix and nothing else. The gzip
member carries a zero timestamp, so regenerating a window produces byte-identical
output and a diff means a real change.

All four are committed rather than generated on demand. They are what makes
`git clone && python tasks.py check-incidents` work on a fresh checkout, and
that first run is the whole demonstration.

## The price book

```yaml
  - provider: openai
    model: gpt-4o
    input_per_million: 2.50
    output_per_million: 10.00
    cached_input_per_million: 1.25
    valid_from: 2026-01-01
```

**These are illustrative figures, not a price list.** They are in the right
order of magnitude for the models named and were not copied from any vendor's
page. Check the real ones before costing anything you care about — and note that
this is exactly why a price book is dated and content-addressed: a report
carries the digest of the rates that produced it, so a number can always be
traced back to what it was computed from.

The book deliberately contains a superseded `gpt-4o` rate with an earlier
`valid_from`, to show what dating is for. Costing last quarter with this
quarter's rates produces a number that is not a fact about last quarter.

## The objectives

`objectives.yaml` is worth reading in full — the comments are the argument.
Two objectives override the standard burn-rate table, both for measured
reasons documented in
[docs/burnrate.md](../docs/burnrate.md#where-the-table-stops-applying):

- **`monthly-spend`** uses 1d and 6h windows because a flat pro-rata budget
  under diurnal traffic makes a 30-minute spend window six and a half times
  noisier than the signal it is watching for.
- **`embedding-latency`** uses 6h and 30m because a low-volume service never
  reaches the sample minimum in an hour, and a rule that is permanently silent
  is indistinguishable from a broken one.

## Running the demos

```bash
python examples/quickstart.py
python examples/burnrate_demo.py
python examples/cardinality_demo.py
```

Or all three: `python tasks.py examples`.
