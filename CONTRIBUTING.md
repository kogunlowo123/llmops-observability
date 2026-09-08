# Contributing

Thanks for taking the time to contribute. This document describes the local
workflow, the quality bar enforced in CI, and how changes are reviewed.

## Prerequisites

- Python 3.12 (the project pins `>=3.12,<3.13`)
- [uv](https://docs.astral.sh/uv/) 0.10 or newer
- Docker (optional, only needed for container work)

`make` is convenient on Linux and macOS but is not required. `python tasks.py`
is the cross-platform entry point and the source of truth; the `Makefile`
simply delegates to it.

## Getting set up

```bash
python tasks.py setup          # uv sync --locked --group dev --group docs
python tasks.py doctor         # proves the install works end to end
```

There is no credential to configure. This project reads telemetry files and
writes reports; it never authenticates to anything. `.env.example` documents the
handful of settings that differ between a laptop and a CI runner, and none of
them change what the gate decides.

Never commit a populated `.env`. `.gitignore` excludes it and CI runs a secret
scan over both the working tree and the full git history.

## Development loop

| Task | Command |
| --- | --- |
| Format | `python tasks.py fmt` |
| Lint | `python tasks.py lint` |
| Type check | `python tasks.py typecheck` |
| Unit tests | `python tasks.py test-unit` |
| Integration tests | `python tasks.py test-integration` |
| Security tests | `python tasks.py test-security` |
| End-to-end tests | `python tasks.py test-e2e` |
| The gate's own negative controls | `python tasks.py test-meta` |
| Full suite + coverage gate | `python tasks.py test` |
| Gate the healthy window, as CI does | `python tasks.py gate` |
| Assert every incident still fires | `python tasks.py check-incidents` |
| Cost the healthy window | `python tasks.py cost` |
| Regenerate the corpus | `python tasks.py corpus` |
| Is the committed corpus still current? | `python tasks.py corpus-check` |
| Run every example | `python tasks.py examples` |
| Documentation site | `python tasks.py site` |
| Local security scans | `python tasks.py security` |
| Container image + smoke test | `python tasks.py smoke` |
| Everything CI runs, in order | `python tasks.py all` |

Run `python tasks.py --list` for the full list.

## Quality bar

A change is mergeable when all of the following hold:

1. `ruff check` and `ruff format --check` are clean.
2. `mypy --strict` reports no errors.
3. The full test suite passes and line coverage is at least **90%**.
4. `python tasks.py check-incidents` passes: every shipped incident still fires,
   with exit 2, breaching exactly the objective it should and no other, **and**
   the healthy window is still green at the same instant.
5. `python tasks.py corpus-check` passes: the committed windows still match the
   scenarios that generated them.
6. `bandit` and `pip-audit` report no unresolved findings. If a dependency
   vulnerability has no upstream fix, add a justified, dated entry to
   `security/audit-exceptions.md` and the identifier to
   `security/audit-ignores.txt`.
7. No secret scanner finding, in the tree or in history.
8. Every example still runs. They are documentation that executes; one that
   stops working is a README that lies.
9. The documentation site builds. The builder fails on a broken internal link,
   so a renamed file fails the pull request rather than the deployment.

## Tests

Five layers, each runnable on its own:

- `unit` — pure logic, no I/O and no network.
- `integration` — components wired together against real temporary files, and
  **the CLI in-process**.
- `security` — adversarial cases. **A failure here is a security regression**,
  not a bug.
- `e2e` — the command line as a real subprocess, so exit codes and the
  stdout/stderr split are actually asserted.
- `meta` — the gate's negative controls. See below.

New behaviour needs a test at the lowest layer that can express it. Tests that
assert nothing meaningful (`assert True`, calls with no assertions) are rejected
in review.

### Five rules specific to this repository

**The CLI needs an in-process layer as well as an end-to-end one.** A subprocess
is a different interpreter: it is invisible to coverage, so a command exercised
only end-to-end measures as 0% and its error paths are never executed under
assertion. That trap cost this project a 69% coverage run and two genuine bugs
that were sitting in plain sight. Both layers, always.

**Every gate rule needs a case that turns the build red.** `tests/meta/` breaks
one thing at a time — corrupts a window, deletes a price book, lowers a target —
and asserts the specific failure each should produce, with a control asserting
the healthy corpus stays green. A test suite that has only ever observed the
gate passing is indistinguishable from `exit 0`.

**An incident assertion needs a control at the same instant.** "The gate fired
at 22:40" is not a fact about the incident unless the healthy window evaluated
at 22:40 is green. `scripts/check-incidents.py` does both, and the control is
the half that is easy to leave out.

**A drift check compares the projection consumers read, never the bytes.**
Regenerate-and-diff is the obvious implementation and it is wrong for any
artefact carrying a timestamp: the check is then red on every run forever, and
somebody disables it. `llmops check` compares digests over the spans' billable
and measurable facts.

**Fixtures that look like credentials are built by concatenation** — `"AKIA" +
"IOSFODNN7EXAMPLE"` — so a scanner does not report a fixture as a finding and a
reader can see at a glance that nothing was ever valid. Add the file to
`.gitleaks.toml` with a written reason.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
  `fix:`, `docs:`, `refactor:`, `test:`, `build:`, `ci:`, `chore:`.
- Keep the subject line under 72 characters and use the imperative mood.
- One logical change per pull request.
- Describe the behaviour change, the risk, and how you verified it.
- Update `CHANGELOG.md` under `## [Unreleased]` for anything user-visible.

## Changing the corpus

`examples/windows/*.jsonl.gz` are generated, and CI checks that they still match
their scenarios. After editing `src/llmops/corpus/synth.py` or a scenario:

```bash
python tasks.py corpus            # regenerate all four
python tasks.py corpus-check      # confirm they match
python tasks.py check-incidents   # confirm each still breaks exactly one objective
```

Commit the regenerated files in the same pull request. Windows are written
gzipped, with a zero timestamp in the gzip header, so the same window produces
the same bytes and the diff is a real change rather than a re-encoding.

Tuning the corpus is done **by measurement**. Each scenario must break exactly
one objective while the control stays green at the same instant, and that is
asserted rather than assumed. `docs/burnrate.md` records the measurements behind
the current window sizes; if you change a scenario's shape, re-take them.

## Adding an objective kind, an exporter or a label

Each is a boundary rather than a convenience:

- **An objective kind** (`docs/objectives.md`) — it must reduce to a burn rate
  on the same scale as the others, so that `decide()` stays the single
  implementation of the two-window conjunction. If it cannot, say why in an ADR.
- **An exporter** (`src/llmops/telemetry/export.py`) — declare
  `reaches_network`. That flag is the whole of the egress control, and it is
  enforced at construction rather than at send. Exercise a networked one against
  a loopback server, never against a vendor.
- **A label** (`docs/cardinality.md`) — give it a finite cardinality, or add it
  to `KNOWN_UNBOUNDED` with the bucketed alternative to use instead. A label
  with neither fails closed, which is the correct default and a poor error
  message.

## Reporting security issues

Do not open a public issue for a vulnerability. Follow the process in
[SECURITY.md](SECURITY.md).
