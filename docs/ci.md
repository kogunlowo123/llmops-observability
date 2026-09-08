# Wiring it into CI

The gate is a command that exits non-zero. Everything else here is detail.

```bash
llmops gate --window telemetry.jsonl.gz --objectives objectives.yaml
```

| Exit | Meaning | What a pipeline should do |
| --- | --- | --- |
| 0 | Every objective that could be evaluated held | Continue |
| 1 | The command line was used wrongly | Fix the invocation |
| 2 | An objective was breached | **Fail the build** |
| 3 | The tool could not produce a verdict | **Fail the build** |

Both 2 and 3 fail, and they stay distinguishable. A stale price book, an
unreadable window and a malformed objectives file all exit 3, and none of them
must ever be mistaken for a healthy platform. A pipeline that treats "non-zero"
as one thing is a pipeline where a broken gate gets retried until it goes green.

## GitHub Actions

```yaml
- name: Error budget
  run: |
    uv run llmops gate \
      --window telemetry/last-24h.jsonl.gz \
      --objectives slo/objectives.yaml \
      --pricebook slo/prices.yaml \
      --json-out reports/slo.json \
      --junit-out reports/slo.xml \
      --markdown-out reports/slo.md

- name: Publish the summary
  if: always()
  run: cat reports/slo.md >> "$GITHUB_STEP_SUMMARY"

- name: Upload the reports
  if: always()
  uses: actions/upload-artifact@v7
  with:
    name: slo-reports
    path: reports/
```

`if: always()` on both, because the reports are most useful on the run that
failed.

## The three report formats

### JUnit XML — the one that matters

```bash
llmops gate ... --junit-out reports/slo.xml
```

A breached objective appears next to the unit tests, with the same red mark, in
whatever dashboard the team already looks at. Nobody has to be taught a new
tool to notice that the platform is on fire, and that is worth more than any
amount of bespoke reporting.

An objective that reached no verdict is a `skipped` case, **not** a passing one.
Every renderer draws that distinction, and this is the format where getting it
wrong would be most damaging: a green suite that silently contains an
unevaluated budget is exactly the false confidence a gate is supposed to remove.

### Markdown — the job summary

```bash
llmops gate ... --markdown-out reports/slo.md
cat reports/slo.md >> "$GITHUB_STEP_SUMMARY"
```

Verdict first line, reason before the tables, damaged-window notice above the
numbers rather than in a footnote below them. Long tables are truncated by the
renderer: a summary with two hundred rows is one nobody reads.

### JSON — for anything else

```bash
llmops gate ... --json-out reports/slo.json
jq -r '.passed' reports/slo.json
```

`passed` is the **first key** in the document, so a script that greps rather
than parses can answer the only question that matters without reading the whole
thing. Burn rates are finite: a zero error budget would otherwise produce
`Infinity`, which is not valid JSON and which half the parsers downstream of a
CI job reject.

The document carries `window_digest` and `objectives_digest`, so a report and
the inputs that produced it can be matched up months later without trusting a
commit message.

## Choosing a window

The gate evaluates at one instant and every rule looks backwards from it. Two
consequences worth planning for:

**Give it more history than the longest rule needs.** The default table's
`creeping-burn` looks back three days. A window holding twenty-four hours will
report that rule as *not covered*, naming how much history it actually had —
correct, but not what you wanted. Either supply three days or drop the rule.

**The incident may be over.** A short window is what makes an alert reset
quickly, which also means a gate run at the end of a window sees a platform that
has already recovered. `--at` evaluates as at a specific instant:

```bash
llmops gate --window w.jsonl.gz --objectives o.yaml --at 2026-09-01T22:40:00+00:00
```

For a nightly job over the last 24 hours, the window's end is the right instant
and no `--at` is needed.

## Producing the window

Whatever writes your telemetry needs to emit the [window
format](windows.md) — one header line, then one span per line, gzipped if the
path ends `.gz`. `llmops synth` generates one for testing your pipeline before
you have real telemetry flowing:

```bash
llmops synth --scenario outage --out telemetry.jsonl.gz
llmops gate --window telemetry.jsonl.gz --objectives objectives.yaml
echo $?    # 2, if your objectives would have caught it
```

That is a genuinely useful thing to do on the day you write your objectives
file: it answers "would this have caught last month's incident?" before there is
a next one.

## Container

```bash
docker run --rm --network none \
  -v "$PWD/telemetry:/app/telemetry:ro" \
  -v "$PWD/reports:/app/reports" \
  llmops-observability:local \
  gate --window telemetry/window.jsonl.gz \
       --objectives telemetry/objectives.yaml \
       --pricebook telemetry/prices.yaml \
       --json-out reports/slo.json
```

`--network none` is safe: a gate reads files and decides, and the only exporter
that could open a socket cannot be constructed without `--allow-network`.

The image runs as uid 10001, so a bind-mounted output directory must be
writable by it. On Linux a bind mount carries the host's ownership straight
through, so `chmod 0777 reports` once. Docker Desktop on Windows ignores
ownership entirely, which is why this failure shows up only in CI.

## Failing the build for the right reason

The trap worth naming: a gate that has only ever been observed passing is
indistinguishable from `exit 0`. A green pipeline proves nothing about the gate.

This repository's own CI answers that with `scripts/check-incidents.py`, which
asserts three things per scenario:

1. the process exits **2** — not merely non-zero, because 3 would mean the tool
   broke;
2. exactly the expected objective is breached, and no other;
3. **the healthy window is still green at the same instant.**

The third is the one that is easy to leave out and the one that carries the
argument. Without it, "the gate fired at 22:40" could be a fact about 22:40
rather than a fact about the incident.

Worth stealing for your own pipeline: keep one window you know is bad, assert
the gate rejects it, and assert a known-good window at the same moment does not.

## What CI runs here

| Workflow | What it does |
| --- | --- |
| `ci.yml` | Lint, format, mypy, five test layers, the coverage gate, the self-gate, examples, wheel |
| `security.yml` | gitleaks over the full history, bandit, pip-audit, CodeQL, trivy |
| `docker.yml` | Build, trivy image scan, non-root check, and the smoke test inside the image |
| `pages.yml` | Build the documentation site and deploy it |

`python tasks.py all` runs the same sequence locally, cheapest gate first.

## See also

- [Objectives](objectives.md) — what the gate is enforcing
- [Burn rate](burnrate.md) — how long before a rule fires
- [Windows](windows.md) — the input format
