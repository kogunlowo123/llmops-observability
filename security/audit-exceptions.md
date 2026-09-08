# Dependency vulnerability exceptions

CI fails on any HIGH or CRITICAL dependency vulnerability. Occasionally a
finding has no upstream fix and no viable replacement. Such a finding may be
time-boxed here rather than suppressed silently, and never by disabling the
scanner.

## Process

1. Confirm the finding is real and reachable from this codebase. A
   vulnerability in a code path the application never executes is still
   recorded, but with that fact stated.
2. Add a row below with the identifier, the package, why it cannot be fixed
   now, the compensating control, and a review date no more than 90 days out.
3. Add the identifier to `security/audit-ignores.txt`.
4. Remove both entries as soon as a fixed version is available.

An exception whose review date has passed is treated as a build failure by the
reviewer, not as a rubber stamp.

## Current exceptions

| Identifier | Package | Reachable | Why not fixed | Compensating control | Review by |
| --- | --- | --- | --- | --- | --- |
| _(none)_ | | | | | |

As of the last dependency scan, every package in `uv.lock` resolves without a
HIGH or CRITICAL finding, so this table is empty. That is the intended steady
state.

## Static analysis suppressions

Bandit findings suppressed inline, each with the reason recorded at the point of
suppression as well as here. An unexplained `# nosec` is a review failure.

| Rule | Location | Why it does not apply |
| --- | --- | --- |
| B405 (`import xml.etree`) | `src/llmops/report/junit.py` | The module only serialises. There is no `parse`, `fromstring` or `XMLParser` anywhere in `src/`, so the entity-expansion and external-entity attacks the rule warns about have no entry point. `defusedxml` would add a dependency defending a code path this package does not have. |

Verify the claim rather than trusting the table:

```bash
grep -rn "fromstring\|XMLParser\|ElementTree.parse" src/    # expect no matches
```
