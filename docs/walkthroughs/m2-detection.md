# M2 — Detection

What exists, why it was built that way, and where it is weak.

## The one-sentence version

The system now detects things. Aircraft manoeuvre, a Kalman filter smooths the
noise out of tracking them, a separation monitor predicts losses of separation
five minutes ahead, and an alert lifecycle publishes those without flooding
anyone. And there is a **number**: scored against ground truth the detector never
sees, it catches every real conflict in the evaluation set, with a median 249
seconds of warning — and raises a false alarm roughly once per airspace hour.

## Prove it works

```powershell
.\scripts\demo.ps1
```

Wait about three minutes. ACP101 and ACP202 get ringed in red on the scope and an
advisory appears in the panel. ACP303 stays green — that aircraft is in the
scenario specifically to catch a detector that forgets to check altitude, and at
T+7:58 it passes within 0.2 NM of both, 4,000 ft above.

> That last sentence was false for most of this project's life. ACP303 was
> placed on the crossing point but arrived 223 seconds early, so it never came
> within 18.8 NM and a detector with the altitude logic deleted still passed
> every test. Found and fixed after M6 by deleting that logic to see what
> failed; nothing did. See `tests/unit/test_scenarios.py`.

Verified on this machine at the end of M2: 4 tracks, 1 active alert reading
*"predicted separation 0.3 NM / 16 ft in 289 s"*, zero `ERROR` lines across seven
containers.

## The headline number, and how to read it

From `eval/results/conflict_detection.md`, over 122 scenarios and 20.5 hours of
simulated airspace:

| Metric | Value |
| --- | --- |
| Real losses of separation | 39 |
| Recall | 1.00 |
| Precision | 0.57 |
| False alerts per airspace hour | 1.41 |
| Median lead time | 249 s (p10 146 s) |

**Why this is not circular.** The detector consumed only noisy surveillance
reports — the same degraded stream it gets in production. Ground truth came from
noiseless simulator state that no part of the pipeline observes. The *traffic* is
invented; the *measurement* is not. This is the single most important thing to be
able to say about the project, and it only works because M1 kept `truth()` and
`observe()` on separate topics.

**Recall of 1.00 is weaker evidence than it looks, and you should say so first.**
The generated encounters are mostly constant-velocity approaches developing over
four to seven minutes, and the detector extrapolates at constant velocity over
five minutes. It is being tested largely on the case its model fits exactly. The
hard case — a conflict created by a late, unforecast turn — is under-represented.
Read it as "the geometry is implemented correctly", not "this never misses".

**Precision of 0.57 is the real result.** 29 of 68 alerts were for pairs that
never actually lost separation. The cause is structural, not a defect: the
detector applies a hard threshold to a point estimate. A pair predicted to miss
by 4.9 NM alerts and one predicted at 5.1 NM does not, while the velocity
estimate behind that prediction carries a couple of knots of noise — which over
300 seconds of extrapolation is more than a nautical mile of position
uncertainty. Encounters engineered to pass at 5–6 NM fall either side of the
line close to arbitrarily.

The fix, in order of value: **probabilistic detection** (the filter already has a
covariance; propagate it and alert on probability of violation rather than
thresholding a point), then **persistence** (require several consecutive scans
before raising — the alert lifecycle already has hysteresis machinery, applied
on clearing but not on raising), then **intent data**, which is unavailable here
by construction.

## What was built

### 1. A flight model that manoeuvres — `src/acp/sim/`

Aircraft now follow a **plan**: a list of timed commands to turn to a heading,
change level, change speed, or change squawk. Each axis chases its target at a
bounded rate and stops exactly on it rather than overshooting.

The design point worth understanding: **the plan is intent, and intent is never
published.** The pipeline sees only the positions that result. A predictor
cannot recover the plan — it can only learn how aircraft in this airspace tend
to behave. That is what will stop the M3 model being a trivial inversion of the
simulator, and it is why the ML result will be worth anything at all.

One small detail with an outsized effect: an aircraft moves along the *average*
of its entry and exit heading for each step. Using either endpoint alone biases a
turning aircraft consistently to one side, and the error accumulates into a
visible position offset over a long turn.

`SIM_VERSION` went from `v1` to `v2`, which is a deliberate act: any result
recorded against v1 describes a different problem and must not be compared with
a v2 number.

### 2. A scenario generator — `src/acp/sim/generator.py`

Two hand-written scenarios are enough to demo and nowhere near enough to measure
precision and recall. This produces families of encounters from a seed.

**The decision that makes the metric meaningful:** encounters are generated with
a *randomised miss distance* straddling the 5 NM standard, and no attempt is made
to force a violation. Roughly 40% end up as genuine conflicts; the rest are close
passes the detector is supposed to stay quiet about. Generating only violations
would make recall trivially perfect and precision meaningless — a detector that
alerts on everything scores 100% against a set containing only positives.

There is also a `SHIFTED` family with different flight levels, densities, and
manoeuvre rates. That exists for M3's distribution-shift test: train on one,
evaluate on the other, report the gap.

### 3. The Kalman filter — `src/acp/services/track/kalman.py`

Six states, linear, constant velocity. [ADR 0006](../adr/0006-constant-velocity-filter.md)
explains the choice, and it is the most interesting decision in this milestone:

**The filter was chosen for its weakness.** A constant-velocity model lags during
a turn, and the size of that lag — the *innovation*, the gap between prediction
and report — spikes exactly when an aircraft does something unmodelled. That is
the manoeuvre signal, published on the wire as `innovation_nm`. An IMM filter
would track the turn smoothly and its innovation would stay small, because one
of its internal models would have anticipated the manoeuvre. **A filter that
tracks manoeuvres perfectly hides the very thing this system exists to detect.**

Two implementation details that matter more than they look:

- **Joseph form** for the covariance update. The textbook `(I − KH)P` is
  algebraically identical but loses symmetry to floating-point error over
  thousands of updates, and an asymmetric covariance eventually goes
  non-positive-definite and takes the filter with it. Pinned by a 2,000-update
  test.
- **Variable measurement dimension.** The measurement vector is assembled from
  whichever fields the report actually contained. The alternatives are to invent
  the missing ones, which biases the estimate, or discard the report, which
  throws away a real observation.

You can see the filter working on the display: in M1 a level aircraft's flight
level flickered between FL349 and FL350 because 25 ft of altitude noise passed
straight through. It does not any more. The position uncertainty on the wire is
now a real covariance too, so it grows on its own while a track coasts — the
ad-hoc growth constant M1 needed is gone.

### 4. Separation monitoring — `src/acp/services/conformance/separation.py`

Both aircraft are projected forward at constant velocity in a shared tangent
plane. For relative position `p` and relative velocity `v`, separation over time
is a parabola whose minimum is at `t = −(p·v)/(v·v)` — the time of closest point
of approach. Clamp to the lookahead window and evaluate there.

**A conflict requires both standards breached at the same moment**: under 5 NM
laterally *and* under 1,000 ft vertically. Either alone is routine and legal.
This is the most common mistake in a naive implementation and there is a test
named after it.

The pairwise search uses a uniform grid keyed by the interaction radius. At the
scale this runs today, plain O(n²) would be fine — the grid is there because M4's
latency budget puts 500 aircraft through this path every second, where 125,000
pair tests per scan is not fine. There is a test asserting **the grid finds
exactly the same conflicts as an exhaustive search**, because a spatial index
that changes the answer is a bug rather than an optimisation.

### 5. The alert lifecycle — `src/acp/services/conformance/alerts.py`

Detectors are stateless: they answer "is this a conflict right now?" several
times a second. Publishing that directly would be unusable — a marginal geometry
sitting on the threshold produces a new alert every scan.

This turns detections into *state changes*: NEW once, SUSTAINED as a heartbeat
while it persists, CLEARED when it stops.

**The asymmetry is the point.** Raising is instant; clearing requires the
condition to be absent for several consecutive scans. Being late to warn is
dangerous; being late to stop warning is merely annoying. A conflict that
disappears for one scan is almost always noise, and clearing then re-raising a
second later trains whoever is watching to ignore the display — a worse failure
than a slightly stale alert.

There is also `forget()`, for when a track simply vanishes. A terminated aircraft
cannot resolve its own conflict by flying away; without this its alert would sit
at the top of the list forever, describing two aircraft one of which the system
no longer believes exists.

### 6. Severity by urgency, not by severity

A predicted 0.1 NM miss five minutes out is **less** urgent than a 4 NM miss
thirty seconds out, because there is still time to do something about the first.
Time is the scarce resource, so time drives severity. This surprises people and
is worth saying out loud in an interview.

## Where this is weak

- **Precision is 0.57.** Named above, in the report, and in `limitations.md`.
- **The evaluation's encounter rate is wildly unrealistic** — nearly every
  scenario stages a close encounter. The false-alerts-per-hour figure is
  therefore *not* comparable to an operational rate. Stated in the report.
- **The detector inherits the constant-velocity assumption twice**, once in the
  filter and once in its own extrapolation. A turning aircraft is doubly
  mispredicted.
- **Conflict resolution is out of scope and always will be.** The system reports
  geometry and stops.
- **The conformance service's in-memory state is not idempotent under replay**
  the way the database writes are. A crash could re-raise an alert with a new
  id. Consumers key on `alert_key` so the visible outcome is right, but the
  event stream would contain a duplicate. Written down in ADR 0005 rather than
  hidden.
- **Still no automated end-to-end test.** The verification above was done by
  hand. That is M4.

## Questions a reviewer might ask

**"Recall of 1.00 — isn't that suspicious?"**
Yes, and the report says so before it says anything else. It reflects an
evaluation set dominated by straight-line approaches, which is the case the
detector's model fits exactly. The number to judge the detector on is precision.

**"Why not use an IMM filter? That's what real trackers use."**
Correct, and for tracking accuracy it would be better. But the innovation from a
constant-velocity filter *is* the manoeuvre signal this system needs, and an IMM
would smooth it away — recovering it would mean reaching inside for the
model-probability vector. ADR 0006 also says when that trade flips: if conflict
detection goes probabilistic, the covariance has to be trustworthy during
manoeuvres, and CV's optimism becomes a real problem.

**"Why scan on a timer instead of on every track update?"**
A conflict is a property of the *set* of aircraft, not of any one of them.
Re-running the pairwise geometry on each individual update would repeat almost
identical work hundreds of times a second and still not answer a different
question. The scan interval is the real latency knob, and M4 measures it.

**"How do you know the spatial grid didn't break anything?"**
A test runs the grid-based scan and an exhaustive pairwise scan over the same
twelve aircraft, including pairs straddling cell boundaries, and asserts the
results are identical.

**"What stops the evaluation numbers going stale?"**
Every report is stamped with the simulator, generator, filter, and detector
versions plus a SHA-256 of the scenario set. A meta-test runs the harness end to
end and asserts those fields are present, and pins the definitions of "detected"
and "false alert" so relaxing them has to break a test.

## Next

M3 — trajectory prediction: physics baselines, a ridge regression on the
residual, a PyTorch model on the same residual, and a distribution-shift
evaluation against the `SHIFTED` family. The ridge baseline ships alongside the
neural network, and if the network does not clearly beat it, the model card says
so and the linear model is what runs.
