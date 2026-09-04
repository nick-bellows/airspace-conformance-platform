# ADR 0013 — The lookahead is an operating point, and the curve gets published

**Status:** accepted
**Date:** 2026-08-17

## Context

For four milestones this repository reported "precision 0.57" as though it were
a property of the conflict detector, alongside a confident cause: thresholding a
point estimate. [ADR 0012](0012-probabilistic-conflict-detection.md) built the
principled fix for that cause and measured it honestly. It did not replicate
across families, so the stated cause was wrong.

That left the number unexplained, so the alerts were examined instead of
theorised about. `eval/analyse_false_alerts.py` records, for every alert, the
geometry the detector predicted and — from noiseless simulator state the
detector never sees — what the pair actually did.

The population is not what "false alert" suggests:

| | |
| --- | --- |
| Median true minimum separation of a false alert | **5.52 NM** (standard: 5 NM) |
| False alerts where the pair truly came within 10 NM | **21 of 31** |
| Median predicted time-to-CPA at raise | **294 s** (ceiling: 300 s) |
| Median horizontal prediction error, true alerts | −0.19 NM |
| Median horizontal prediction error, false alerts | +0.92 NM |

Most "false" alerts are pairs that genuinely got close, flagged at the very edge
of the lookahead window, against a standard that is a step function. A pair
predicted to pass at 4.8 NM that truly passes at 5.5 NM is scored as a complete
failure and is nothing of the sort.

## Decision

**Publish precision as a function of lookahead, and leave the default at 300 s.**

`eval/run_lookahead_sweep.py` sweeps the window on both scenario families:

| Lookahead | Precision (nominal) | Precision (shifted) | Recall (shifted) | Median lead |
| --- | --- | --- | --- | --- |
| 300 s ←default | 0.5571 | 0.4000 | 0.9667 | 249 s |
| 240 s | 0.6393 | 0.4667 | 0.9667 | 237 s |
| 180 s | 0.7222 | 0.5490 | 0.9667 | 180 s |
| 120 s | 0.8667 | 0.6364 | 0.9667 | 120 s |
| 60 s | 0.8667 | 0.7179 | 0.9667 | 60 s |

Re-measured on `acp-separation-v2`. Nominal recall is 1.00 at every row.

Direction and rough magnitude hold on **both** families, which is precisely what
the probabilistic threshold failed to do and the reason this is trusted where
that was not.

## Why the default does not move

It would be easy, and wrong, to adopt 120 s and report precision 0.87.

**Lead time is the product.** This system exists to give someone minutes to act.
Going from a median four-minute warning to a two-minute one to make a table look
better is changing the subject, not improving the detector.

**It used to cost recall on shifted traffic, and no longer does — this ADR was
half wrong.** The original version of this table showed shifted recall falling
from 0.933 at 300 s to 0.900 at 120 s, and that was the second of two reasons
given for keeping the default. It was an artefact of a detector defect. Until
`acp-separation-v2` the detector evaluated the vertical standard only at
horizontal closest approach, so it missed pairs that pass inside the lateral
standard while still vertically clear and then converge vertically before
separating ([ADR 0014](0014-conflict-is-an-interval-not-an-instant.md)). Fixing
that raised shifted recall to **0.9667 at every window in the sweep**, flat.

So the recall argument is withdrawn. **The default stands on lead time alone**,
which is sufficient: at 300 s the median warning is around four minutes and at
120 s it is two, and warning time is what the system is for. A detector that is
precise because it warns too late has changed the subject rather than improved.

Recording the withdrawal rather than quietly restating the table is the point.
An argument that survives only because of a bug is not an argument.

**Nothing about the detector improved.** Shortening the window does not make a
prediction better, it declines to make the hard ones. That is a legitimate
operating choice for a real deployment to make against its own tolerance for
nuisance alerts — and it is exactly the kind of choice that should be a
documented dial rather than a constant nobody questioned.

## Consequences

**The reporting defect is fixed, not the detector.** The headline number now
carries the lookahead it was measured at, and the curve is committed. This was
always the real defect: a single figure quoted without its axis described one
point as though it were the system.

**A thread is left open and named.** 9 of the 31 false alerts involve pairs that
breached the lateral standard for real but were vertically clear at closest
approach. Those are vertical prediction errors, not lookahead cost, and this ADR
does not explain them. Constant vertical rate over a five-minute horizon is the
obvious suspect — an aircraft levelling off mid-climb gets predicted straight
through a flight level it never reaches — but ADR 0012 is a standing reminder
that an obvious suspect is not a cause until it has been measured on traffic it
was not chosen against. It is written in `future-work.md` as a hypothesis.

**The analysis runner is committed and reproducible**, so the population can be
re-examined after any detector change rather than re-argued.
