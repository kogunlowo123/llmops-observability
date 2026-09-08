# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-09-08

First release. Everything below is new.

### The gate

- `llmops gate` evaluates a telemetry window against an objectives file at one
  instant and exits **0** when every objective that could be evaluated held,
  **2** when one was breached, **3** when it could not produce a verdict, and
  **1** on a usage error. The four codes are part of the interface (ADR-010).
- Multi-window, multi-burn-rate alerting: four rules, two windows each, and a
  rule fires only when **both** windows exceed the threshold. The conventional
  table from the Google SRE workbook, unchanged (ADR-003).
- Three objective kinds — `availability`, `latency` and `spend` — sharing one
  engine, because measuring dollars against a window's pro-rata share of the
  period budget puts spend on the same scale as failed calls (ADR-004).
- A rule that could not be evaluated is reported as **not evaluated**, never as
  passing: under-sampled rules and rules looking back further than the data
  goes each get their own section in every report format.
- `--at` evaluates as at a given instant, because a short window means an
  incident is often over by the end of the file.

### Cost

- `llmops cost` prices a window from a versioned price book, in `Decimal`
  throughout, with rates per million tokens as vendors publish them.
- Cached input is a separate rate rather than a discount factor; an
  operation-specific rate beats a general one; rates carry `valid_from` so a
  span is costed at the rate in force when it happened.
- **An unpriced model is a hard failure, not a zero** (ADR-002).
- Reconciliation against a provider's own `reported_cost_usd`, with both a
  relative and an absolute tolerance — a span is discrepant only when both are
  exceeded. "No comparisons" is reported as such rather than as "0 discrepant".

### Telemetry

- A `Span` model with no field for prompt or completion text, and
  `extra="forbid"` so one cannot be added (ADR-001). Bounded attribute bag: at
  most 32 keys, at most 1024 bytes each.
- A refusal counts as a successful call. An availability objective that treats
  refusals as outages rewards weakening a safety filter.
- Windows are JSON Lines over a half-open interval; a malformed line is counted
  and reported, a malformed header is fatal (ADR-008).
- Windows are gzipped when the path ends `.gz`, with a zero timestamp in the
  gzip member so the same window always produces the same bytes. The shipped
  corpus is 620 KB rather than 7.1 MB.
- The size limit applies to the decompressed stream as well as the file on
  disk, so a decompression bomb is refused rather than swallowing the runner.
- A window's digest is over the spans' billable and measurable facts, not over
  the bytes, so reformatting or compressing a window does not move it.
- `JsonlExporter` and `OtlpHttpExporter`. A networked exporter cannot be
  *constructed* without `--allow-network` (ADR-007). OTLP/HTTP is spoken over
  stdlib `urllib` rather than by taking on a tracing SDK.
- Redaction runs once over the assembled document immediately before it is
  written, never over the inputs (ADR-009).

### Cardinality

- `llmops labels` refuses a metric whose labels multiply out past the series
  budget, at registration rather than at emit (ADR-006), naming the largest
  dimension and the bucketed alternative for each known-unbounded label.
- Unregistered labels fail closed. Re-registering a label with a different
  cardinality is refused.

### The corpus

- `llmops synth` generates deterministic telemetry from a seeded scenario, with
  a diurnal arrival rate and log-normal latency. Four scenarios ship: `steady`,
  `outage`, `slowdown` and `cost-spike`, each breaking exactly one of the four
  example objectives.
- Every generated window declares in its own header that it was synthesised and
  that nothing in it was captured from a real service (ADR-005).
- `llmops check` verifies a committed window still matches its scenario, by
  digest rather than by regenerating and diffing.

### Reports

- JSON with `passed` as the first key and no `Infinity`; JUnit XML where an
  unevaluated objective is a `skipped` case and not a passing one; a Markdown
  job summary with the verdict on the first line and the reason above the
  tables.

### Quality

- 461 tests across five layers — unit, integration, security, end-to-end and
  meta — at 94% line coverage against a 90% gate.
- `scripts/check-incidents.py` asserts each incident exits 2, breaches exactly
  the expected objective, and that the healthy window is green at the same
  instant. CI runs it on every push.
- CI: ruff, mypy `--strict`, five test layers, a coverage gate, the self-gate,
  every example, a wheel that is installed and invoked, gitleaks over the full
  history, bandit, pip-audit, CodeQL, trivy, and a container smoke test that
  runs the gate inside the built image and asserts it can still fail there.

[Unreleased]: https://github.com/kogunlowo123/llmops-observability/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kogunlowo123/llmops-observability/releases/tag/v0.1.0
