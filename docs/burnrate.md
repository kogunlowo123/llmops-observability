# Burn rate

The arithmetic behind the gate, the table it uses, and — the part worth reading
— the two places where that table stops applying and what this repository does
about it.

## The idea in one paragraph

An objective with a target of 99.9% over 30 days allows 0.1% of calls to fail.
That allowance is the **error budget**. The burn rate is how fast it is being
spent, as a multiple of the rate that would exactly exhaust it over the period.
Burning at 1× means finishing the month with nothing left over. Burning at 14.4×
means the month's budget is gone in two days.

```
burn rate = observed failure rate ÷ error budget
          = (failed ÷ total) ÷ (1 − target)
```

A gate on the burn rate rather than on the failure rate is the difference
between an alert that means "this is consuming your month" and one that means
"this number crossed a line somebody picked".

## Two windows, both required

A single window cannot be both precise and quick to reset. A long window
notices a real incident and ignores a blip, but keeps alerting for hours after
the incident ends. A short window resets immediately and pages on noise.

So every rule has two, and **both** must exceed the threshold:

```python
fired = long.burn >= rule.burn_rate and short.burn >= rule.burn_rate
```

The long window supplies precision: it is what decides that this is worth
someone's night. The short window supplies reset time: when the incident stops,
it falls below the threshold within minutes and the alert clears. Requiring both
is the entire trick, and it is why `decide()` is one function used by every kind
of objective rather than three near-copies.

## The default table

Taken unchanged from the Google SRE workbook. See ADR-003 for why it was not
invented here.

| Rule | Severity | Long | Short | Burn rate | Budget consumed | Detection time |
| --- | --- | --- | --- | --- | --- | --- |
| `fast-burn` | page | 1h | 5m | 14.4× | 2% | 1h |
| `moderate-burn` | page | 6h | 30m | 6× | 5% | 6h |
| `slow-burn` | ticket | 1d | 2h | 3× | 10% | 1d |
| `creeping-burn` | ticket | 3d | 6h | 1× | 10% | 3d |

*Budget consumed* is how much of the period's budget has been spent by the time
the rule fires; *detection time* is how long a total outage takes to trigger it.
Both fall out of the same arithmetic:

```
detection time = (budget fraction × period) ÷ burn rate
               = (0.02 × 30d) ÷ 14.4 = 1 hour
```

`detection_time(rule, budget=..., failure_rate=...)` computes it for any failure
rate, and `python examples/burnrate_demo.py` prints the whole table across a
range of them — so the question "how long before this pages someone?" has an
answer you can run rather than derive. It returns `None` when the rule never
fires at that rate, which is a real answer and not an error.

## Firing exactly at the threshold

A burn rate is a ratio of integers divided by a budget, and at the exact
documented trigger point that arithmetic lands a hair below it: 1440 failures in
100,000 calls against a 0.001 budget is `14.399999999999999`, not `14.4`. A
strict `>=` therefore does *not* fire at the number the documentation promises,
which is the kind of defect that survives review because the test that would
catch it is the one nobody writes.

```python
THRESHOLD_TOLERANCE = 1e-9


def _at_or_above(value: float, threshold: float) -> bool:
    return value >= threshold or math.isclose(value, threshold, rel_tol=THRESHOLD_TOLERANCE)
```

The tolerance is relative and tiny. It exists to make the documented boundary
true, not to soften the comparison.

## Sample minimums

Below `min_samples` (30 by default) a rule reports **not evaluated**, with the
count. Three calls of which one failed is a 33% failure rate and a burn rate in
the hundreds, and it means nothing.

"Not evaluated" is not "passed". Every renderer keeps them in separate sections
for that reason. A summary that lists an unevaluated rule among the healthy ones
is worse than no summary, because a reader concludes there was a verdict.

The same applies to a rule that looks back further than the data goes: it is
reported as **not covered**, naming how much history there actually was.

---

## Where the table stops applying

The table is calibrated for **availability at scale**: a high-volume signal
whose expected value is roughly flat over a day. Two of the four shipped
objectives are not that, and both override it. This is the useful part of this
page, because it is what a tool that merely quotes the workbook will not tell
you.

### 1. Spend against a flat budget, under diurnal traffic

A spend objective measures dollars against the window's **pro-rata** share of
the period budget (ADR-004). That share is flat. Real traffic is not: it follows
a daily cycle, and so does spend.

The consequence is that on a **completely healthy** platform the measured burn
rate swings with the time of day — and the shorter the window, the more it
swings. Measured over the shipped `steady` corpus, which contains no incident at
all:

| Window length | Lowest burn | Highest burn | Spread |
| --- | --- | --- | --- |
| 30m | 0.26× | 1.65× | **6.5×** |
| 6h | 0.44× | 1.33× | 3.1× |
| 12h | 0.53× | 1.09× | 2.1× |
| 1d | 0.80× | 0.86× | 1.1× |

Reproduce it yourself: the numbers come from `examples/windows/steady.jsonl.gz`
and nothing else.

A 30-minute short window on spend is therefore *six and a half times noisier*
than the signal it is watching for, purely from the daily cycle. A rule using
one would drop out of alert every night and back in every morning, and the first
person to be paged twice by it would turn it off.

At one day the cycle has averaged out completely — spread 1.1× — and at six
hours it is small enough to leave headroom. So the shipped `monthly-spend`
objective uses **1d and 6h**, not the table's 1h and 5m:

```yaml
rules:
  - {name: fast-burn,     severity: page,   long_window: 1d, short_window: 6h,  burn_rate: 2}
  - {name: creeping-burn, severity: ticket, long_window: 3d, short_window: 12h, burn_rate: 1.3}
```

The thresholds are lower as well as the windows longer, and for a separate
reason: 14.4× on a cost budget means spending a month's money in two days, and
by the time that fires the invoice is already written. Sustained overspend at 2×
is worth a page.

Check that those numbers separate. During the shipped `cost-spike` incident the
same measurement gives:

| Window length | Lowest burn | Highest burn |
| --- | --- | --- |
| 6h | 0.85× | **4.38×** |
| 1d | 1.89× | **3.18×** |

Healthy peaks at 1.33× (6h) and 0.86× (1d); the incident reaches 4.38× and
3.18×. A threshold of 2× sits cleanly between them on both windows. That is what
"sized to the cycle" means in practice, and it is why the corpus was tuned by
measurement rather than by choosing round numbers.

### 2. Low-volume services

Embeddings in the shipped corpus run at roughly twenty calls an hour. A
one-hour window never reaches the thirty-sample minimum, so the table's
`fast-burn` rule would report "not evaluated" forever — a rule that is
permanently silent, which is indistinguishable from one that is broken.

The answer is not to lower `min_samples`. A rule firing on three calls is worse
than no rule. The answer is that **a low-volume service cannot be watched as
closely**, and the objective should say so:

```yaml
rules:
  - {name: fast-burn,     severity: page,   long_window: 6h, short_window: 30m, burn_rate: 14.4}
  - {name: slow-burn,     severity: ticket, long_window: 1d, short_window: 2h,  burn_rate: 3}
  - {name: creeping-burn, severity: ticket, long_window: 3d, short_window: 6h,  burn_rate: 1}
```

Six hours instead of one, and the acceptance that a fast incident in a
low-traffic service is detected in hours, not minutes. Writing that down is
better than a rule that silently never runs.

A related trap: at very low volume the short window is dominated by single
calls. Ten calls in thirty minutes means one expensive call moves the measured
burn rate by tens of percent, and the gate flaps. Both effects push the same
way — longer windows for smaller services.

---

## Reading a verdict

```
BREACHED  3/4 objective(s) held
  PAGE  chat-availability: fast-burn: FIRING — burn 229.17x over 1h and 400.00x over 5m, threshold 14.4x
  note  chat-availability: slow-burn not covered — the rule looks back 1d and only 22.6667h of history precedes 2026-09-01T22:40:00+00:00
```

Both windows are printed with their measured burn, because a rule that fired on
one and not the other should be visible as such. `note` lines are rules that
reached no verdict, and they never count towards "held".

## Where the code is

| Concern | Location |
| --- | --- |
| `BurnRule`, `WindowMeasurement`, `decide` | `src/llmops/slo/burnrate.py` |
| Turning a window and objectives into an evaluation | `src/llmops/slo/evaluate.py` |
| The default table | `STANDARD_RULES` in `src/llmops/slo/burnrate.py` |
| Detection-time arithmetic | `detection_time` in `burnrate.py` |

## See also

- [Objectives](objectives.md) — declaring targets, selectors and overrides
- [Cost](cost.md) — how the dollars a spend objective burns are computed
- [CI](ci.md) — wiring the verdict into a pipeline
