# ADR 0006 — A constant-velocity Kalman filter, chosen for its weakness

Status: accepted · Date: 2026-08-15

## Context

Surveillance reports are noisy. Something has to turn them into a state estimate
good enough to extrapolate five minutes forward and compare against another
aircraft. The standard options, in increasing order of fidelity and cost:

1. **Constant velocity (CV).** Linear, six-state, one matrix inversion per
   update. Assumes the aircraft keeps doing what it is doing.
2. **Constant turn rate (CTRV).** Tracks a turning aircraft much better. The
   dynamics are non-linear, so it needs an extended or unscented filter.
3. **Interacting Multiple Model (IMM).** Several models in parallel with a
   probability assigned to each, blended per update. This is what production
   trackers actually use.

On accuracy alone the ordering is obvious: IMM beats CTRV beats CV, and the gap
is largest exactly when it matters, during a turn.

## Decision

Constant velocity, six states `[east, north, v_east, v_north, altitude,
vertical_rate]`, in a local tangent plane.

## Consequences

**The reason is not cost.** It is that a CV filter's failure mode is the signal
this system needs.

A constant-velocity model **lags during a turn**, and the size of that lag is
measurable: the *innovation*, the gap between where the filter predicted the
aircraft would be and where the next report put it, spikes precisely when the
aircraft does something the model does not anticipate. That is published on
`tracks.updates.v1` as `innovation_nm` and is the manoeuvre signal the
conformance monitor is built on.

An IMM would track the turn smoothly. Its innovation would stay small, because
one of its internal models would have anticipated the manoeuvre. **A filter that
tracks manoeuvres perfectly hides the very thing this system exists to detect.**
Recovering the signal would then require reaching inside the filter for the
model-probability vector — a more complicated route to a worse-conditioned
version of a number CV hands over for free.

So the simple model is not a compromise here. It is the right instrument.

**What it costs, honestly:**

- Reported position is least accurate exactly when an aircraft is manoeuvring,
  which is also when a conflict is most likely to be developing. The lag is
  bounded by the process noise tuning but it is real.
- The conflict detector inherits the constant-velocity assumption twice: once in
  the filter and once in its own five-minute extrapolation. A turning aircraft
  is therefore doubly mispredicted, and this is a material contributor to the
  false-alert rate reported in `eval/results/conflict_detection.md`.
- The filter does not know it is wrong. It reports a covariance that assumes its
  own model is correct, so during a turn the position uncertainty it publishes
  is optimistic.

**When to revisit.** If the conflict detector moves to probabilistic detection —
propagating the covariance and alerting on probability of violation rather than
thresholding a point estimate — then the covariance has to be trustworthy during
manoeuvres, and CV's optimism becomes a real problem. That is the point at which
an IMM earns its complexity, and the manoeuvre signal would have to be sourced
from the model probabilities instead.

## Implementation notes worth keeping

- **Joseph form** for the covariance update. The textbook `(I - KH)P` is
  algebraically identical but loses symmetry to floating-point error over
  thousands of updates; an asymmetric covariance eventually goes
  non-positive-definite and takes the filter with it. Pinned by a 2000-update
  test.
- **Variable measurement dimension.** The measurement vector is assembled from
  whichever fields the report actually contained. Real surveillance drops
  fields, and the alternatives are to invent the missing ones — which biases the
  estimate — or to discard the report, which throws away a real observation.
- **Velocity is carried in knots**, not nautical miles per second, so the tuning
  constants read in the units an aviator would use and a wrong one is visible on
  inspection.
