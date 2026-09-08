# Security policy

## Reporting a vulnerability

Report privately through GitHub's advisory workflow:
<https://github.com/kogunlowo123/llmops-observability/security/advisories/new>

Please do not open a public issue for a vulnerability.

Include what you have: the window, objectives file or command that triggers it,
what you expected, what happened, and the version or commit. A proof of concept
helps but is not required.

**What to expect.** This is a portfolio project maintained by one person, not a
funded product, and it is fairer to say so than to publish a service-level
agreement nobody is on call for. Acknowledgement within a week is realistic. If
a report is valid it will be fixed and credited, and the fix will say what was
wrong rather than describing it as "hardening".

## Supported versions

The `main` branch. There is no backport policy for older tags.

## What this protects, and what it does not

[THREAT-MODEL.md](THREAT-MODEL.md) is the document to read before running this
on anything that matters. The short version:

**In scope.** Prompt or completion text reaching telemetry; credentials reaching
a report through the attribute bag; unintended network egress; resource
exhaustion through a hostile window file, including a decompression bomb; a gate
that silently stops being able to fail; an unevaluated rule being reported as a
passing one.

**Out of scope.** Telemetry that is wrong rather than malformed — there is no
cryptographic provenance on a span and no attempt at one. An attacker who can
already modify the objectives file in your repository; they do not need a
vulnerability. The security of the collector you point `--allow-network` at.

## The things most worth understanding

**A span has no prompt or completion field.** Not a redaction rule — there is no
field. `extra="forbid"` means an attempt to add one is refused at validation.
There is no configuration under which this tool exports a prompt, because there
is nowhere to put one. Redaction over the attribute bag is a second line of
defence against a credential arriving through it, not the only line.

**Nothing reaches the network unless it is asked for.** An exporter that opens a
socket cannot be *constructed* without `--allow-network`. Not "declines to
send" — the object does not exist in the process. An `LLMOPS_EXPORT__OTLP_ENDPOINT`
left in a CI job by mistake cannot start sending on its own.

**Compressed input is bounded.** Accepting `.gz` windows means accepting the
possibility of a decompression bomb, so the size limit is applied to the
decompressed stream and not only to the file on disk. Without that guard a
200 KB file could expand until the runner is killed — which reaches an operator
as "the gate is flaky" rather than as an attack.

**A rule that was not evaluated is never reported as passing.** Under-sampled
and uncovered rules get their own section in every report format, including a
`skipped` case in the JUnit XML. "We did not look" and "we looked and it was
fine" are different facts, and a gate that conflates them is worse than no gate
because it manufactures confidence.

## Credentials

This project handles none of its own. It reads telemetry files and writes
reports; there is no provider client and no authentication anywhere in the
codebase. `.env.example` contains no credential field, because there is nothing
for one to configure.

Never commit a populated `.env`. It is gitignored, and CI runs `gitleaks` over
both the working tree and the **full git history** — not just the diff — on
every push, because a secret removed in a later commit is still in the history.

## What CI enforces

| Check | Tool | Scope |
| --- | --- | --- |
| Secrets | gitleaks | Working tree and full history |
| Static analysis | bandit | `src/` |
| Dependency vulnerabilities | pip-audit | The locked set, `--strict --no-deps` |
| Code scanning | CodeQL | `security-extended` queries |
| Filesystem and config | trivy | HIGH and CRITICAL |
| Container image | trivy | HIGH and CRITICAL, plus a non-root assertion |

A HIGH or CRITICAL dependency finding fails the build. Time-boxed exceptions are
recorded in [`security/audit-exceptions.md`](security/audit-exceptions.md) with
an identifier, a reason, a compensating control and a review date — never as a
bare entry in an ignore list.

## Container

The image runs as uid 10001 with no build toolchain in the final layer, and
`docker-compose.yml` sets `network_mode: none` on every service except the one
whose purpose is to export. Turning the network on is a visible act in a
reviewed file rather than a default nobody noticed.

## Dependencies

Four at runtime: `pydantic`, `pydantic-settings`, `structlog`, `pyyaml`. The
OTLP exporter is a hundred lines of stdlib `urllib` rather than a tracing SDK,
specifically so that the largest dependency in the project is not the one
serving its least-used component. `uv.lock` is committed and CI installs with
`--locked`.
