# Does recall survive manoeuvring traffic?

Generated 2026-08-18T00:24:42.893193+00:00 · seed `20260815` · 120 scenarios per row · generator `acp-gen-v1` · detector `acp-separation-v1`

Reproduce with `python eval/run_manoeuvre_sweep.py --scenarios 120`.

Every row is the nominal family with exactly one parameter changed: the
probability that each aircraft of the staged pair manoeuvres part-way
through. Same seed, same density, same geometry, same speeds.

| Manoeuvre probability | Recall | Missed | Precision | Median lead |
| --- | --- | --- | --- | --- |
| 0.0 | 0.9773 | 1 / 44 | 0.7818 | 276.0 s |
| 0.35 ←nominal | 1.0 | 0 / 38 | 0.5672 | 244.5 s |
| 0.7 | 1.0 | 0 / 43 | 0.589 | 221.0 s |
| 1.0 | 1.0 | 0 / 27 | 0.3857 | 211.0 s |

## What this says

**The README's caveat about recall was wrong, and this is the measurement
that says so.** It argued that recall 1.00 was flattered because the
encounters are mostly constant-velocity, which is the detector's own
assumption. That predicts recall falling as manoeuvres increase. It does
not fall:

* At zero manoeuvres — the assumption made flesh — recall is 0.9773, the *lowest* row.
* At the nominal 0.35 it is 1.0.
* With every staged aircraft manoeuvring it is still 1.0.

Recall is robust to manoeuvre density here, and the plausible reason is
that the detector re-evaluates every second against a five-minute horizon.
A turn does not have to be predicted, only observed: once the aircraft is
established on its new heading there is still ample time to raise an alert
before the violation. The detector needs to be right once, at any point in
the approach, and manoeuvring traffic gives it many chances.

**What manoeuvres actually cost is precision and lead time.** Precision
falls from 0.7818 to 0.3857 across the sweep,
and the median warning from 276.0 s to
211.0 s. Every turn creates a
transient geometry that briefly looks like a conflict and then resolves.

**Which is the same finding as the lookahead sweep, from the other side.**
[`lookahead_tradeoff.md`](lookahead_tradeoff.md) shows precision falling as
the detector extrapolates further; this shows it falling as the traffic
becomes less extrapolable. Both are the same quantity — how much
constant-velocity error accumulates before closest approach — and precision
tracks it while recall does not.

**Caveat on the denominators.** Changing manoeuvre probability changes
which encounters actually violate, so the number of real events differs per
row (44 at 0.0, 27 at 1.0). These are not the same
conflicts made harder; they are different traffic. The comparison is
between populations, and at 27-44 events per row a single detection moves
recall by two to four points.

