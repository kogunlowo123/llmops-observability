# Architecture

How this tool is put together, and the decisions a reader is most likely to
argue with — recorded with the reasoning, so that disagreeing with one is a
matter of disputing an argument rather than guessing at an intention.

## The shape of it

One command line over five libraries. Nothing runs as a service; there is no
database, no daemon, and no state between invocations. A gate run is a pure
function of three files and one instant:

```
   window (JSONL)  ─┐
   objectives (YAML)├─▶  evaluate  ─▶  Evaluation  ─▶  report  ─▶  exit 0 | 2
   price book (YAML)┘                                              ▲
                                                                   │
                                              stderr summary, JSON, JUnit, Markdown
```

| Package | What lives there |
| --- | --- |
| `llmops.telemetry` | The `Span`, the `Window`, reading and writing them, and the exporters |
| `llmops.slo` | Objectives, burn-rate rules, and the evaluation that puts them together |
| `llmops.cost` | Price books and spend reports, in `Decimal` |
| `llmops.cardinality` | The label budget, checked at registration |
| `llmops.corpus` | The deterministic scenario generator behind `llmops synth` |
| `llmops.report` | Four renderings of one `Evaluation` |
| `llmops.cli` | Argument parsing, exit codes, and nothing else |

The direction of every import is downward: `report` reads an `Evaluation` and
knows nothing about files, `slo` knows about spans and not about YAML parsing,
`telemetry` knows about neither. `cli` is the only module that touches
`argparse`, `sys.exit` or the filesystem layout, which is what makes the same
code testable in-process and as a subprocess.

## Data model

A **span** is one call to a model. It carries the provider, the model, the
operation, the outcome, a duration, token counts, and a small bag of bounded
attributes. It does **not** carry the prompt or the completion — see ADR-001.

A **window** is the spans in a half-open interval `[start, end)` plus the lines
the reader could not parse. Half-open because a closed interval double-counts a
boundary span, which surfaces a week later as a spend report that will not
reconcile by exactly one call.

An **objective** is a target, a period, an optional selector, and a list of burn
rules. An **evaluation** is every rule's verdict at one instant, together with
the digests of the window and the objectives that produced it.

---

## ADR-001 — A span has no prompt or completion field

**Status:** accepted.

**Context.** Telemetry pipelines leak prompts. Not through a bug in the
redactor: through an `attributes` dictionary that someone reasonably filled with
the user's question because the field was there and the debugging was hard.

**Decision.** The span model has no field for the text of a request or a
response, and `extra="forbid"` means an attempt to add one is rejected at
validation rather than carried. Attributes are capped in count and in size.

**Consequences.** The most effective privacy control available to this pipeline
costs nothing and cannot be switched off: there is no configuration under which
a prompt is exported, because there is nowhere to put one. Redaction over the
attribute bag remains, as a second line of defence against a key that happens to
contain a token, not as the only one.

The cost is real and worth stating: you cannot debug a bad completion from this
telemetry. That is the right side of the trade for a tool whose output goes to
a metrics backend and a CI job summary.

---

## ADR-002 — An unpriced model is a hard failure, not a zero

**Status:** accepted.

**Context.** A price book is a file someone maintains by hand. A new model
appears in production before it appears in that file — always, because the
sequence is procurement, then deployment, then somebody notices. The tempting
implementation charges an unknown model zero and carries on.

**Decision.** Costing a span whose model is not in the price book raises
`UnpricedModelError` and the command exits 3. There is no fallback rate, no
average, and no flag to suppress it.

**Consequences.** The model missing from the price book is, by construction, the
newest one — which is to say the most expensive one, deployed most recently, by
the team most likely to be surprised. Charging it zero produces a spend report
that is confidently, silently, arbitrarily low, and a budget gate that goes
green on the exact day it should go red.

Exit 3 rather than 2 matters: this is "the tool could not run", not "the budget
was burned". A pipeline that treats them the same is one where a broken price
book looks like a healthy platform.

---

## ADR-003 — The conventional burn-rate table, not one invented here

**Status:** accepted.

**Context.** Alerting on an error budget needs thresholds and window lengths.
It is easy to pick some, and hard to pick well: the constraint is a
three-cornered trade between detection time, reset time and precision, and the
arithmetic that balances it is not obvious from a dashboard.

**Decision.** The default rules are the four-rule table from the Google SRE
workbook, unchanged:

| Severity | Long window | Short window | Burn rate | Budget consumed |
| --- | --- | --- | --- | --- |
| page | 1h | 5m | 14.4× | 2% |
| page | 6h | 30m | 6× | 5% |
| ticket | 1d | 2h | 3× | 10% |
| ticket | 3d | 6h | 1× | 10% |

A rule fires only when **both** windows exceed the threshold. The long window
supplies precision; the short one supplies reset time, so an alert stops within
minutes of the incident ending rather than hours.

**Consequences.** A reader who knows the workbook recognises this immediately
and can stop reading. A reader who does not can be pointed at a published source
rather than at this repository's own reasoning. Numbers invented here would have
had to be defended here, and would have been wrong.

The table is a default and not a law. Two of the four shipped objectives
override it, both for documented reasons — see `docs/burnrate.md`, which also
records where the table stops applying.

---

## ADR-004 — Spend and availability are one engine

**Status:** accepted.

**Context.** A monthly cost budget is obviously an error budget: there is a
quantity, a period, and a rate at which it is being consumed. The question is
whether to say so in the code.

**Decision.** A spend objective measures dollars against the window's
**pro-rata share** of the period budget. Six hours of a thirty-day $80 budget is
$0.667; spending $2 in those six hours is a burn rate of 3×. That is the same
number, on the same scale, as three times the acceptable rate of failed calls,
so the same rules, the same two-window conjunction and the same renderers apply
without a special case.

**Consequences.** There is one implementation of the thing most likely to be got
wrong — the conjunction — rather than two that drift apart. A reader who
understands the availability path understands the spend path.

The cost is a real limitation and it is documented rather than hidden: a flat
pro-rata budget against diurnal traffic under-reads at night. `docs/burnrate.md`
names the effect, shows where it was observed, and explains why the shipped
spend objective uses six-hour and one-day windows instead of the table's.

---

## ADR-005 — The corpus is synthesised, deterministically, and the tool says so

**Status:** accepted.

**Context.** This repository has to demonstrate that a gate fires on a real
incident. Real telemetry cannot be published: it is someone's traffic, and
scrubbing it well enough to publish is a larger and less certain job than
generating it.

**Decision.** Every example window is generated by `llmops synth`, a shipped and
documented command, from a seeded scenario. The header of every window file
carries `generated: true` and a note saying nothing in it was captured from a
real service. `llmops check` re-runs the generator and compares digests.

**Consequences.** A generated window can contain an incident of a known size at
a known instant, which is what makes it possible to assert that a rule fires *at
the right moment* — and, more usefully, that a healthy window evaluated at that
same moment stays green. `scripts/check-incidents.py` asserts exactly that, and
CI runs it.

The drift check compares **digests**, not files. Regenerate-and-diff is the
obvious implementation and it is wrong here for the same reason it was wrong in
the sibling repository: a file that carries a timestamp always differs from its
regeneration, so the check either never passes or is quietly disabled.

---

## ADR-006 — Cardinality is refused at registration, not at emit

**Status:** accepted.

**Context.** `tenant_id` as a metric label passes code review and takes down the
metrics backend three months later, when the tenant count crosses a threshold
nobody was watching. Checking label *values* as they arrive is too late: by then
the metric exists, the emitting code is deployed, and the series are being
created.

**Decision.** A metric declares its labels to a `LabelRegistry`, which multiplies
the cardinality of each and refuses the declaration if the product exceeds the
budget. Unregistered labels fail closed. Each known-unbounded label maps to the
bucketed alternative to use instead.

**Consequences.** The failure happens in a unit test on a laptop, with a message
that names the offending label, its cardinality, and its replacement. This is
the same shape as two other decisions in this codebase — the exporter that
cannot be *constructed* without `--allow-network`, and the harness elsewhere in
this series that enforces hermeticity at construction. Making the invalid state
unrepresentable beats detecting it.

---

## ADR-007 — Nothing reaches the network unless it is asked for

**Status:** accepted.

**Context.** A telemetry tool is precisely the component that should not
surprise anyone by sending data somewhere. The default configuration exports
nothing, but a default is a thing that can be overridden by an environment
variable in a CI job nobody reads.

**Decision.** `build_exporter` takes `offline` and raises
`NetworkNotAllowedError` when asked for a networked exporter without
`--allow-network`. The refusal is at **construction**, so an exporter that could
open a socket does not exist in the process unless the operator said so on the
command line.

**Consequences.** "Nothing left the machine" is checkable by reading one
constructor rather than by auditing every call site. The security test layer
asserts it, and the assertion is about the object graph rather than about a
mock.

---

## ADR-008 — Windows are JSON Lines, gzipped by suffix

**Status:** accepted.

**Context.** Telemetry is appended to by a process that can be killed. A single
JSON array truncated mid-write is unreadable in its entirety; a truncated JSONL
file has lost exactly its last line.

**Decision.** One header object, then one span per line. The reader counts and
reports lines it could not parse instead of failing, and a report says how much
of the evidence it was computed over. A malformed *header* is fatal, because
without it every subsequent number would be over an interval this tool guessed.

A path ending `.gz` is gzipped, decided by the suffix and never by sniffing the
content. The gzip member carries a zero timestamp so that writing the same
window twice produces the same bytes.

**Consequences.** The example corpus is 620 KB instead of 7.1 MB. The digest is
over the spans, so a window keeps its content address whether it is stored
compressed or not, and the drift check does not notice the difference.

Accepting compressed input means accepting a decompression bomb, so the size
limit is applied to the decompressed stream and not only to the file on disk.
Without that, a 200 KB file could expand until the runner is killed, which
reaches an operator as "the gate is flaky".

---

## ADR-009 — Assemble, then redact once

**Status:** accepted, inherited from the MCP server in this series.

**Context.** Redacting each field as it is written means every new field is a
new place to forget. The forgetting is silent and is discovered by someone
reading a log.

**Decision.** The exporter builds the complete document, redacts the assembled
structure in one pass, and serialises the result. There is exactly one call to
the redactor in the write path.

**Consequences.** Adding a field cannot bypass redaction, because the redactor
runs over whatever the document turned out to contain. The count of redactions
performed is recorded in the window header, so a file states how much of itself
was rewritten.

---

## ADR-010 — Exit codes are part of the interface

**Status:** accepted.

**Context.** A gate is consumed by CI, where the only thing read reliably is the
exit status. "Non-zero" is not enough information: a pipeline needs to
distinguish a platform that missed its objective from a tool that could not run.

**Decision.**

| Code | Meaning |
| --- | --- |
| 0 | Every objective that could be evaluated held |
| 1 | The command line was used wrongly |
| 2 | An objective was breached |
| 3 | The tool could not produce a verdict |

**Consequences.** A broken price book, an unreadable window and a malformed
objectives file all exit 3, and none of them can be mistaken for a healthy
platform. `scripts/check-incidents.py` asserts exit **2** specifically, rather
than "non-zero", because a gate that started exiting 3 on every run would
otherwise look like a gate that works.

---

## Testing strategy

Five layers, each of which catches something the others cannot.

| Layer | Marker | What it is for |
| --- | --- | --- |
| Unit | `unit` | Arithmetic and validation, in isolation |
| Integration | `integration` | Real files, real parsing, the CLI **in-process** |
| Security | `security` | Adversarial input, and what must not appear in output |
| End-to-end | `e2e` | The CLI as a real subprocess, exit codes included |
| Meta | `meta` | Break one thing, assert the gate goes red |

The in-process CLI layer exists because of a defect found in the sibling
repository and repeated here: a subprocess is a different interpreter, so a CLI
exercised only end-to-end measures as 0% covered and its error paths are never
executed under assertion. Adding that layer here moved coverage from 69% to 94%
and surfaced two genuine bugs in the first run.

The meta layer is the one that keeps the rest honest. A test suite that has only
ever observed the gate passing is indistinguishable from `exit 0`. Those tests
corrupt a window, delete a price book, and lower a target, and assert the
specific failure each should produce.
