# ADR 0014 — A conflict is an interval, not an instant

**Status:** accepted
**Date:** 2026-09-04

## Context

An external review found a correctness defect in the core detector, after three
earlier review rounds and several self-audits had passed over it.

The detector solved for the *time of horizontal closest approach*, then
evaluated the vertical standard **at that single instant**. That is not the
question a conflict asks. A conflict is both standards breached *at the same
moment*, and the moment at which that first happens is not necessarily
horizontal closest approach.

The reproduction is simple enough to be embarrassing. Two aircraft 12 NM apart,
head-on at 450 kt each, 2000 ft apart, the higher one descending at 1000 fpm:

| Time | Horizontal | Vertical | |
| --- | --- | --- | --- |
| 48 s | 0.0 NM | 1200 ft | horizontal closest approach — vertically clear |
| 60 s | 3.0 NM | 1000 ft | vertical standard reached |
| **61–67 s** | **3.3 → 4.8 NM** | **983 → 880 ft** | **both breached — a real loss of separation** |

The pair stays inside 5 NM from roughly t=39 s to t=67 s and drops below 1000 ft
at t=60 s. The overlap is real, and the detector reported nothing, because at
t=48 s the aircraft were still 1200 ft apart.

## Decision

**Solve for the interval in which each standard is breached, and test whether
those intervals overlap inside the lookahead window.**

Squared horizontal separation is a quadratic in time, so the times at which the
pair is closer than the lateral standard are the roots of that quadratic minus
the threshold. Vertical separation changes linearly, so its breach interval is a
simple pair of roots. Intersect both with `[0, lookahead]`. A non-empty
intersection is a conflict; an empty one is not.

Reporting then follows from the interval rather than from a single instant:

- `time_to_cpa_s` is the moment of minimum horizontal separation **within the
  overlap**, which is the unconstrained closest approach whenever that falls
  inside it — so the common co-altitude case reports exactly what it did before.
- `min_horizontal_nm` and `min_vertical_ft` are each the minimum **over the
  overlap**, so both values strictly breach their standard. Reporting the
  vertical figure at the horizontal minimum would sometimes print a number equal
  to the standard, which reads as a contradiction.

`DETECTOR_VERSION` moves to `acp-separation-v2`, invalidating every committed
conflict-detection report. All six affected reports were regenerated.

## What it cost and what it recovered

The fix can only *add* detections — it never removes one — so precision can only
fall or hold, while recall can only rise.

| | Before | After |
| --- | --- | --- |
| Nominal recall | 1.00 | 1.00 |
| Nominal precision | 0.5735 | **0.5571** |
| Shifted recall (every lookahead) | 0.867 – 0.933 | **0.9667** |
| Shifted precision at 300 s | 0.4000 | 0.4000 |

**On the manoeuvre-rich shifted family the fix recovered real missed conflicts
at no precision cost**, which is exactly where the defect would be expected to
bite: descending and climbing aircraft crossing a level pair. On the nominal
family, where the geometry barely occurs, it added two alerts and both were
false, costing 0.016 precision.

It also **withdrew an argument in [ADR 0013](0013-lookahead-is-an-operating-point.md)**.
That ADR gave two reasons for keeping the 300 s default; the second was that a
shorter window costs recall on shifted traffic. With the defect fixed, shifted
recall is flat at 0.9667 across every window, so that reason evaporated. The
default still stands on lead time, which was always the stronger half.

## The part worth keeping

**The scenario generator never produced this geometry.** Recall stayed at 1.00
on the nominal family for the entire life of the defect, not because the
detector was correct but because nothing in the evaluated population exercised
the case. A headline recall of 1.00 measured against a generator that shares the
detector's blind spot says considerably less than it appears to.

That is now stated in `limitations.md` next to the number, and it generalises:
**a metric is only as strong as the adversariality of the population it was
measured on.** No amount of confidence-interval discipline fixes a test set that
never asks the hard question.

## Alternatives considered

**Sample the geometry at fixed timesteps and check each sample.** Simple, and it
would have caught this. Rejected because it trades an exact answer for a
sampling rate: a brief overlap between samples is missed, and the scan already
runs against a latency budget at 500 aircraft. The closed-form interval is both
exact and cheaper.

**Check the vertical standard across the whole lookahead rather than at CPA.**
Catches this case, but over-alerts: it would fire on a pair that is vertically
close early and horizontally close much later, with no moment where both hold.
That is the mirror-image error and would have been worse.

**Leave it and document it.** Rejected. This is the one algorithm in the
repository whose correctness the whole project is about, and the defect produced
silent false negatives — the failure mode the system exists to avoid.

## Consequences

**Do not now generate scenarios that specifically exercise this geometry and
republish recall.** That would be fitting the test set to a bug already fixed.
If a family of vertical-crossing encounters is built, it is a new population
measured on its own terms and reported separately.

**The remaining vertical thread is unchanged and still open.** 9 of the 31 false
alerts involve pairs that breached the lateral standard for real while
vertically clear at closest approach. That is a vertical *prediction* error —
constant vertical rate over five minutes — and is a different problem from this
one. It stays a hypothesis in `future-work.md` until measured.
