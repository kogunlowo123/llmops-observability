# Threat model

What this tool is trusted with, what it refuses to be trusted with, and what it
does not defend against.

Written the way a review would want it: assets first, then a boundary, then the
threats in the order they are likely to matter, each with the control that
answers it and a pointer to the test that holds the control in place.

## What this is

A command-line tool. It reads three files, evaluates them at one instant, writes
reports, and exits. It listens on no port, has no database, and holds no state
between runs. Optionally — and only when asked on the command line — it POSTs
telemetry to an OTLP collector.

That shape removes most of the attack surface a reviewer looks for first. There
is no session, no authentication, no multi-tenancy, and no request handling. The
threats that remain are about **data flow**: what goes into a window, what comes
out in a report, and where it is sent.

## Assets

| Asset | Why it matters |
| --- | --- |
| Prompt and completion text | The user's data. Regulated in most contexts, embarrassing in all of them. |
| Provider credentials | An API key in an attribute bag reaches every reader of every report. |
| The verdict itself | A gate that can be made to say "held" is worse than no gate. |
| Cost figures | Wrong numbers here become wrong budgets and wrong capacity plans. |
| The CI runner | The process that runs the gate has a token and a checkout. |

## Trust boundary

```
  untrusted ─────────────────────────────────┐   trusted
                                             │
  window file  ─────────────────────────────▶│  Span validation (pydantic, extra=forbid)
  objectives   ─────────────────────────────▶│  Schema check, unknown keys refused
  price book   ─────────────────────────────▶│  Schema check, dated, content-addressed
  environment  ─────────────────────────────▶│  Settings, unknown names refused
                                             │
                                             │  evaluate ─▶ report
                                             │
                                             ├─▶ stdout / files      (redacted)
                                             └─▶ OTLP collector      (opt-in only)
```

Everything to the left is written by something else — an agent, a proxy, a
previous run, a contributor's editor. None of it is trusted to be well-formed,
bounded, or honest.

---

## T1 — Prompt or completion text leaks into telemetry

**The realistic version.** Not a bug in a redactor. An engineer debugging a bad
answer puts the question into `attributes["query"]` because the field is there
and the debugging is hard, and six months later that field is in a metrics
backend three teams can read.

**Control — structural.** The span model has **no field** for prompt or
completion text, and `extra="forbid"` rejects an attempt to add one at
validation. There is no configuration under which this tool exports a prompt,
because there is nowhere to put one. See ADR-001.

**Control — defence in depth.** Attributes are capped at 32 keys and 1024 bytes
each, so the bag cannot become a transcript by accident.

**Residual risk.** Somebody can still put a short prompt fragment into an
attribute value deliberately. The cap makes it visible in review rather than
invisible in production. Accepted.

**Tested by** `tests/unit/test_span.py`, `tests/security/test_no_leaks.py`.

---

## T2 — A credential arrives through the attribute bag and is exported

**The realistic version.** A base URL with a key in the query string; an
`Authorization` header attached during an incident and never removed; a
connection string used as a service name.

**Control.** Redaction over the **assembled** document, once, immediately before
it is written — never over the inputs. This is ADR-009, and it is here because
the second repository in this series learned that a pass over inputs leaves
everything derived from them still carrying what the pass removed.

Two mechanisms: a set of sensitive attribute *names* whose values are always
removed regardless of shape (`authorization`, `token`, `secret`, `cookie`,
`password`, and their separator-split variants), and a set of vendor-anchored
*patterns* — OpenAI, Anthropic, Google, AWS, GitHub, Slack, bearer tokens, JWTs,
URL credentials, PEM private-key headers.

The placeholder is fixed-length. A placeholder that echoed the length of what it
replaced would leak it.

**Residual risk.** Patterns are anchored on vendor prefixes precisely so they do
not fire on ordinary text, which means a credential in a format nobody has seen
passes through. This is why the structural control in T1 is the primary one and
this is the second line.

**Tested by** `tests/security/test_no_leaks.py`, including a test that fails if
the redaction pass is deleted.

---

## T3 — Telemetry is sent somewhere nobody intended

**The realistic version.** An `LLMOPS_EXPORT__OTLP_ENDPOINT` set in a CI job
that nobody reads, and telemetry going to a collector that was decommissioned or
was never the right one.

**Control.** `build_exporter` refuses to *construct* a networked exporter unless
`--allow-network` was passed. Not "declines to send" — the object does not
exist in the process. Nothing is exported by default.

**Residual risk.** An operator who passes `--allow-network` and an endpoint gets
what they asked for. Certificate validation is Python's default; there is no
option to disable it.

**Tested by** `tests/integration/test_export.py`, `tests/security/test_no_leaks.py`.

---

## T4 — A hostile window file

**The realistic version.** A window is a file. It can be truncated, corrupted,
enormous, or built to be all three.

| Attack | Control |
| --- | --- |
| Truncated mid-line | One JSON document per line; the reader counts unreadable lines and the report states the count |
| A line of 200 MB | `MAX_LINE_BYTES` (256 KB) — a longer line is not a span, it is a different kind of file |
| A 40 GB window | `MAX_WINDOW_BYTES` (256 MB), checked before reading |
| **A 200 KB gzip that expands to 40 GB** | The same limit applied to the *decompressed stream*, not only to the file on disk |
| Spans claiming impossible values | Validators: no naive timestamps, no cached tokens exceeding input tokens, no error type on a successful span |
| A window from a newer schema | Refused by version, with the version named |

The decompression bomb is the one worth calling out. Accepting `.gz` input means
accepting it, and checking only `stat()` would let one through. Without the
stream guard the runner is killed by the OOM killer, which reaches an operator
as "the gate is flaky" rather than as an attack.

**Residual risk.** A window can still contain *plausible but false* spans. This
tool cannot detect a lying producer; see T6.

**Tested by** `tests/integration/test_windows.py`, `tests/unit/test_span.py`.

---

## T5 — The gate is made to pass

**The realistic version.** Nobody edits the tool. Somebody edits the objectives
file, or the pipeline stops running the step, or a rule silently stops being
evaluated and the summary reads as if it held.

| Attack | Control |
| --- | --- |
| Lower a target in a PR | Objectives are a reviewed file; the report carries their digest |
| Delete the price book so the spend objective is skipped | Missing price book with a spend objective is exit 3, never a skip |
| A rule that cannot be evaluated | Reported as **not evaluated**, in its own section, never as passing |
| Under-sampled window | Reported with the sample count, never as passing |
| The gate step is removed | Outside this tool's control — a branch protection concern |

"Not evaluated" and "passed" being different is the single most important
property of the reports, and it is why every renderer has a separate section for
it — JSON, JUnit (`skipped`, not a passing case) and Markdown alike.

**Tested by** `tests/meta/test_the_gate_can_fail.py`, which lowers targets,
corrupts windows and deletes price books and asserts the specific failure each
should produce.

---

## T6 — The telemetry is wrong, not malformed

Out of scope, and stated so rather than left implied. If the producer reports
half the calls it made, this tool computes a correct verdict about half a
platform. There is no cryptographic provenance on a span and no attempt at one.

What the tool does do is refuse to *pretend*: unreadable lines are counted and
declared above the numbers, spend is reconciled against the provider's own
`reported_cost_usd` where the producer supplies it, and a systematic divergence
between computed and reported cost is a finding rather than a rounding note.

---

## T7 — Supply chain

| Concern | Control |
| --- | --- |
| Dependency count | Four runtime dependencies: pydantic, pydantic-settings, structlog, pyyaml |
| Transitive drift | `uv.lock`, committed; CI installs with `--locked` |
| Known vulnerabilities | `pip-audit --strict --no-deps` over the exported lock, in CI |
| Static analysis | `bandit` over `src/`, and CodeQL on push |
| Secrets in history | `gitleaks` over the full history, not only the diff |
| An OTLP SDK's transitive tree | Avoided: the exporter is stdlib `urllib` and under a hundred lines |

Exceptions to the audit are recorded in `security/audit-exceptions.md` with a
reason and a date, not as a bare ignore list.

---

## T8 — The CI runner

The gate runs in a job that has a checkout and a token. This tool executes no
subprocess, evaluates no expression from a file, and imports nothing named by
configuration. The objectives file is YAML parsed with `yaml.safe_load`; the
price book likewise.

Workflow permissions are least-privilege per job, and the container image runs
as a non-root user with no build toolchain in the final layer.

---

## Not defended against

Stated plainly, because a threat model that claims to cover everything is one
nobody can act on.

- **A malicious maintainer.** Commit signing and branch protection are the
  answer, and they live outside this repository.
- **A compromised provider.** If the price book's source is wrong, the costs are
  wrong. Reconciliation against provider-reported costs is the mitigation and it
  is only as good as what the producer reports.
- **Traffic analysis of an OTLP export.** Sizes and timings of exports are
  visible to anything on the path. Use TLS and a private endpoint.
- **Denial of service.** There is no service. A large enough window makes the
  process slow; the size limits bound it, and nothing else is claimed.

## Reporting

Security issues: see [SECURITY.md](SECURITY.md). Please do not open a public
issue for a vulnerability.
