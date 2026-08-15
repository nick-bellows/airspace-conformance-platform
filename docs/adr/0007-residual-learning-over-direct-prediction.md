# ADR 0007 — Learn the correction to physics, not the trajectory

Status: accepted · Date: 2026-08-15

## Context

The trajectory model has to answer "where will this aircraft be in 60 seconds?"
from a window of filtered track updates. Two framings were available.

1. **Direct.** Predict the future position — latitude, longitude, altitude, or
   a displacement from the current position.
2. **Residual.** Compute a dead-reckoning prediction with physics, then predict
   only the *error* of that prediction: along-track, cross-track, and altitude.

## Decision

Residual, in the aircraft's own along/cross frame.

## Consequences

**The failure mode is bounded, and that is the main argument.** A residual model
that outputs zero is *exactly* dead reckoning — a known, sane, well-understood
prediction that the rest of the system already assumes. A direct model that
fails outputs an arbitrary position, and an arbitrary position in a conflict
detector is worse than no prediction at all. Everything the serving path does on
failure (no artifact, corrupt artifact, NaN output, short track) reduces to
"return the baseline", and that only works because the baseline is the origin of
the model's output space.

**The learning problem gets much easier.** Predicting where an aircraft will be
in a minute is mostly arithmetic that physics already does perfectly. Only the
deviation needs data. The model spends its capacity on the part that is actually
uncertain.

**Measurement becomes honest by construction.** Skill is expressed against dead
reckoning, and dead reckoning is the zero of the target space, so the baseline
and the model are scored by identical code on identical quantities. A separately
implemented baseline can drift from the model's evaluation path and quietly
flatter it.

**The frame choice matters as much as the residual choice.** Along-track and
cross-track rather than latitude and longitude, because:

- the components mean the same thing wherever on earth the aircraft is, so the
  model does not have to learn geography;
- they separate two physically different errors — being wrong about *speed* from
  being wrong about *heading* — which a lat/lon target smears together.

A sign error in that decomposition would be invisible in the loss and would put
every prediction on the wrong side of the aircraft, so the training run asserts
the decomposition round-trips.

**What it costs.** The model cannot express a prediction that is not reachable as
an offset from dead reckoning within the horizon — irrelevant at these horizons,
but it would matter if the horizon grew to where the baseline is no longer even
approximately right. `features.horizon_seconds_valid` caps the horizon at five
minutes for this reason.

**Rejected: direct prediction of absolute position.** Simpler to explain, and
strictly worse on every count above. Its unbounded failure mode alone rules it
out for a system whose output feeds a safety advisory.

**Rejected: predicting a displacement from the current position.** Better than
absolute, and still worse than residual: the model would have to relearn that an
aircraft at 450 kt covers 7.5 NM a minute, which is not a fact that needs
learning.
