# ADR 0012 — Probabilistic conflict detection, alongside the deterministic one

**Status:** accepted
**Date:** 2026-08-16

## Context

The committed evaluation reports a recall of 1.00 and a **precision of 0.57**.
The precision has been the repository's weakest published number since M2, and
`limitations.md` has always named the cause rather than the symptom: the
detector thresholds a point estimate.

It projects both tracks to the closest point of approach, takes the predicted
miss distance, and compares it to 5 NM and 1000 ft. A predicted 4.9 NM miss
raises an alert at full confidence; 5.1 NM raises nothing. The velocity estimate
carries enough noise to move a pair across that line, so a large share of the
false alerts are geometries the detector was never in a position to call.

Meanwhile the Kalman filter maintains a full covariance. It grows during
dropouts, grows during manoeuvres, and — the part that matters most — the error
in a *predicted* position grows with the lookahead, because velocity error
integrates. A conflict predicted five minutes out is a far weaker claim than the
same geometry thirty seconds out. None of that reached the decision.

## Decision

Add a probabilistic detector that computes **P(both standards breached at
closest approach)** from the covariance, and **keep the deterministic one as the
default** until the evidence says otherwise.

The relative position at closest approach is modelled as Gaussian:

    sigma_h(t)^2 = sp1^2 + sp2^2 + t^2 * (sv1^2 + sv2^2)

Horizontal breach is then the probability that a 2-D isotropic Gaussian lands
inside a disc of radius 5 NM — a non-central chi-squared with two degrees of
freedom. Vertical breach is a 1-D normal integrated across ±1000 ft. A conflict
needs both, so the two multiply.

`SeparationMonitor(probability_threshold=...)` switches it on. Left unset, the
detector behaves exactly as every published number was measured on.

## Alternatives considered

**Replace the deterministic detector outright.** Rejected, and this is the
important half of the decision. The project's rule since M3 is that a new
method ships beside its baseline and wins on published evidence or does not
ship — that is how the neural trajectory predictor was handled, including
reporting that it barely beats ridge regression. A detector change that alters
the headline safety metric deserves at least that much scepticism.
`eval/results/detector_comparison.md` is the head-to-head, threshold swept in
full, losing rows included.

**Widen the standards instead.** Alerting at 7 NM rather than 5 NM would also
catch the marginal geometries, and would catch them everywhere — including for
pairs the filter knows perfectly well. That trades precision for recall by
brute force and gives an operator no way to tell a firm warning from a guess.

**Report the miss distance in sigmas.** Cheaper, and most of the benefit. But a
sigma count is not a quantity anyone can set an operational threshold on
without further translation, whereas a probability is.

**Take the covariance from the conformance service's own predictor.** It has
one, but it is a different model with different assumptions. The filter's
covariance is the one that actually produced the positions being projected.

## The measured outcome, which is not the hoped-for one

**It did not fix the precision problem, and the deterministic detector stays the
default.** This was the top item in `future-work.md` — "the one thing worth
building next" — and building it produced a negative result worth more than the
feature would have been.

On the nominal family, one threshold beat the baseline: `p >= 0.5` raised
precision from 0.574 to 0.609 at unchanged recall and lead time, a difference of
+0.036 with a scenario-clustered 95% CI of [+0.008, +0.076] that excludes zero.
Every lower threshold was significantly *worse* — down to 0.41 at `p >= 0.01`.

Then it was validated on the **shifted** family, which the threshold was not
chosen on:

| Family | deterministic | `p >= 0.5` | Difference (95% CI) |
| --- | --- | --- | --- |
| nominal | 0.5735 | 0.6094 | +0.0358 [+0.0079, +0.0762] |
| shifted | 0.4000 | 0.4058 | +0.0058 [+0.0000, +0.0205] — spans zero |

**The advantage does not replicate.** On traffic the threshold was not selected
against, it is +0.006 and statistically indistinguishable from zero. The
nominal-family result was mostly the threshold being fitted to the family it was
measured on — which is exactly what the pre-registered warning in this ADR said
to watch for, and the reason the shifted run was done at all rather than
declaring victory on the first table.

Two findings survive and are worth keeping:

- **Low thresholds buy recall with precision.** On shifted traffic, `p <= 0.2`
  recovers a real loss of separation the deterministic detector misses (recall
  0.933 → 0.967) at roughly half the precision. For a system whose stated
  priority is never missing a conflict, that trade is at least arguable, and it
  is a knob the deterministic detector does not have.
- **`p >= 0.7` looks good on shifted and is not safe.** It gains +0.059
  precision there with no recall cost, but on nominal it drops recall to 0.974 —
  it misses a real conflict. A threshold that is safe on one family and not the
  other is not a threshold, it is a coincidence.

The honest summary for the README: the covariance was already there, using it is
principled, and it bought almost nothing. The precision of 0.57 is not caused by
the thing everyone assumed caused it.

## Consequences

**The wire contract grew three optional fields** —
`velocity_uncertainty_kt`, `altitude_uncertainty_ft`,
`vertical_rate_uncertainty_fpm`. Optional, so the addition is backward
compatible and the schema version does not move; the backward-compatibility gate
in CI checks that claim rather than trusting it. A track arriving without them
is judged deterministically instead, so an older producer degrades rather than
breaking — and so does a message replayed from before the change.

**The cheap vertical pre-filter had to be widened.** `_test_pair` rejects pairs
whose altitudes cannot converge within the lookahead, which is a large share of
the pair-test savings. Under the probabilistic detector that rejection has to
account for altitude uncertainty, or a fast pre-filter would silently overrule
the model and produce false negatives — the worst failure available here.

**A special function is implemented by hand.** The conformance service runs with
numpy only: scipy arrives with scikit-learn, and the `degradation` job proves
the service works with the ml extra genuinely uninstalled. The non-central
chi-squared CDF is therefore computed in `probability.py`, written as a race
between two Poisson variables so nothing overflows, and checked against
`scipy.stats.ncx2` in the test suite, where scipy is available as an oracle.

**What it still does not model.** The covariance is not rotated into the
along-track/cross-track frame, so an elliptical uncertainty is treated as
isotropic. Turn rate is not propagated, so a turning aircraft's future position
is more uncertain than this says — the innovation signal is the system's
separate answer to that. Both are in `limitations.md`.

**A threshold is a dial, and dials invite fitting.** The sweep is measured on
the same nominal family the committed report uses. Choosing a threshold from it
and then quoting that row as the system's performance would be selection on the
test set; validating a chosen threshold needs a family it was not chosen on, and
that is recorded in `future-work.md` rather than quietly skipped.
