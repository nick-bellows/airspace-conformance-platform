# M3 — Trajectory prediction

What exists, why it was built that way, and where it is weak.

## The one-sentence version

A PyTorch model now predicts where each aircraft will be a minute from now, and
the system raises an advisory when reality disagrees. During turns — the only
regime where that prediction is hard — it roughly halves the error of the physics
baseline, and it keeps most of that advantage when the airspace it is flying over
changes.

## The headline result

Horizontal error while **turning**, 60 s horizon, median:

| Split | Dead reckoning | Neural | Skill | Samples |
| --- | --- | --- | --- | --- |
| Unseen scenarios, same family | 2.412 NM | **1.220 NM** | **+49.4%** | 4,495 |
| Shifted family (different airspace) | 2.024 NM | **1.261 NM** | **+37.7%** | 3,830 |

At 30 s: +59.1% / +47.2%. At 120 s: +46.3% / +40.8%.

**The gap between the two rows is the number worth discussing.** Skill drops by
about a quarter when the model flies over an airspace with different flight
levels, densities, and manoeuvre patterns. It does not collapse. That is the
honest cost of training on one traffic distribution and operating in another, and
it is only visible because the evaluation was built with a second, deliberately
different scenario family from the start.

## What the model is not good at, stated first

- **Cruise and climb, horizontally.** Dead reckoning is already accurate to tens
  of metres. There is nothing to win.
- **Altitude at 30 s, where the model is *worse* than the baseline** — 31 ft for
  physics against 98 ft for the model. It only earns its place vertically at
  60 s (563 → 305 ft) and 120 s (1,828 → 450 ft). The model card says which to
  use.
- **The constant-turn baseline is worse than assuming straight flight** (3.28 NM
  against 2.66 NM at 60 s). Extrapolating the *estimated* turn rate for a minute
  amplifies its noise into miles of arc error. The obvious physics improvement
  over dead reckoning makes things worse, which is a useful thing to know.

## What was built

### 1. Residual learning — `src/acp/ml/`

The model predicts the **correction** to a dead-reckoning prediction, not a
position: along-track, cross-track, and altitude error.

**A model that outputs zero is exactly dead reckoning.** That single property is
why every failure path in the serving code can say "return the baseline" and mean
something safe. A direct position predictor's failure mode is unbounded, and an
arbitrary position inside a conflict detector is worse than no prediction at all.
[ADR 0007](../adr/0007-residual-learning-over-direct-prediction.md).

The along/cross frame matters as much as the residual: the components mean the
same thing anywhere on earth, and they separate being wrong about *speed* from
being wrong about *heading*, which a latitude/longitude target would smear
together. A sign error there would be invisible in the loss and would put every
prediction on the wrong side of the aircraft, so the decomposition is tested to
round-trip across every quadrant.

### 2. Ridge as the control

Both models see identical features and predict the same target. The winner is
chosen on validation, on the turning stratum, and only if it beats ridge by at
least 3%.

Neural won at every horizon — +32.7%, +23.5%, +17.6% — with the margin narrowing
as the horizon grows. **If a deployment valued inspectability over that margin,
shipping ridge would be defensible on these numbers**, and its weights are
recorded in the report so that argument can actually be had.

### 3. The features cannot see intent

The simulator generates flight plans. Those plans are never published on any
topic. The feature extractor takes a sequence of `TrackUpdate` and nothing else,
so the model cannot recover intent — it can only learn how aircraft in this
airspace tend to behave.

This is structural rather than a matter of discipline, and it is what stops the
whole exercise being an inversion of the generator.

The most interesting feature is `innovation_nm` — the Kalman filter's surprise,
straight off M2's deliberately-weak constant-velocity filter. A rising innovation
is the earliest observable sign that an aircraft is doing something physics will
get wrong. **M2's design decision to keep a filter that lags through turns is
what makes this feature exist.**

### 4. Conformance monitoring — `src/acp/services/conformance/monitor.py`

Every track gets a prediction; a minute later it is checked against where the
aircraft actually went. A large gap raises a `NON_CONFORMANCE` advisory.

Verified end to end on the `unannounced-turn` scenario: the turn is commanded at
t=240 s, and the advisory appears at **t=274 s** — the prediction made at t=214 s
maturing and finding the aircraft 3.7 NM from where it was supposed to be.

The alert's own summary text says *"no flight plan is available, so this may be
an entirely normal manoeuvre"*. That caveat belongs in the alert an operator
reads, not only in a document they will not open. Severity is **always advisory**;
escalating would imply a judgement about the flight that a system with no
clearance data cannot make.

## Three mistakes worth knowing about

These are the substance of the milestone, more than the final numbers.

### The evaluation was measuring the wrong thing, twice

The first result looked excellent: the model beat dead reckoning by 33% overall.
It was meaningless. Dead reckoning's median error at 60 s was **83 metres** —
because nearly every sample was an aircraft flying dead straight, where a
straight-line prediction is trivially near-perfect. The median was measuring the
easy case.

The first fix stratified by the *filtered* turn rate, and put three quarters of
steady cruise into the "manoeuvring" bucket, because the estimate is noisier than
the threshold — the same noise that ruins the constant-turn baseline. Stratifying
on the simulator's true phase fixed that.

The second fix still merged turns with climbs. But **a climbing aircraft barely
moves sideways**, so 3,121 easy climbing samples buried 907 genuinely hard
turning ones, and the model looked worse than useless. Splitting them apart —
judging turns on horizontal error and climbs on altitude error — is what finally
made the result legible.

**The lesson: the metric has to match the axis the aircraft is manoeuvring in.**
An aggregate that mixes regimes will be dominated by whichever regime is most
common, which is always the easy one.

### The training data had 17 usable samples

The M2 scenario family gives one manoeuvre to at most two aircraft per scenario.
A held-out set of five scenarios contained **seventeen** turning samples. Nothing
can be concluded from seventeen samples, and the numbers computed from them
swung wildly between splits.

A `TRAINING` family with manoeuvre-rich traffic fixed it, and the guard that
matters is that `NOMINAL` was left untouched — the M2 conflict report is measured
on it. That guard is a test comparing a fingerprint of the generated scenario set
against the one recorded in the committed report. **It failed the first time it
ran**, catching a reordered random draw in a refactor that would have silently
invalidated M2's published numbers.

### The conformance threshold could never fire

The first design scaled the threshold by the same sample's dead-reckoning error:
alert when the model beat physics by less than some factor. When the predictor
falls back to physics — no artifact, corrupt artifact, NaN output — the model's
error *is* the baseline's error, so the comparison reduced to `error > 3 × error`
and no alert was possible.

Conformance monitoring would have silently stopped working at exactly the moment
the model became unavailable. A test caught it. The threshold is now calibrated
against measured typical error from the evaluation report, with separate figures
for model and physics.

## Where this is weak

- **Simulated aircraft fly exactly.** No wind, no turbulence, no variation
  between autopilots or crews. Real 60-second prediction error is substantially
  larger than anything here and the gap is unmeasured.
- **The manoeuvre density is unrealistic**, deliberately. Proportions between
  strata say nothing about real airspace.
- **The threshold factor has never been swept.** 3.0 is a judgement call. There
  is no precision/recall curve for non-conformance alerts, and there should be.
- **Calibration constants are copied from the report by hand.** Retraining a more
  accurate model without updating them would make the monitor progressively less
  sensitive with nothing to indicate it. The operations manual says so; a build
  step that derived them would say it better.
- **Still no automated end-to-end test.** M4.

## Questions a reviewer might ask

**"Isn't training on data your own simulator produced circular?"**
For the conflict detector, no — that is scored against ground truth it never
sees. For the trajectory model it is a fair concern, and the answer is the
structural one: the simulator's *intent* is never published, so the model sees
only noisy positions and cannot invert the generator. What it cannot do is tell
you how it would perform on real traffic, and the model card says that.

**"Why is a 3,500-parameter network the right size?"**
Because a larger one would fit the training scenarios more closely without
predicting unseen traffic any better, and the distribution-shift split is there
to show that. The current gap between same-family and shifted skill is already
the interesting number; widening it by adding capacity would be a worse model
that looked better on one column.

**"Why not put an LLM in this?"**
[ADR 0008](../adr/0008-no-generative-model-in-the-safety-path.md). Briefly:
determinism is a property of the whole pipeline and every published number
depends on it, the latency budget does not admit an inference call, and "degrade
to physics" is not available for a component whose output is prose. A fluent,
confident, wrong narrative attached to a safety alert is the failure mode most
likely to be believed.

**"What happens if the model file is missing in production?"**
The service starts, logs `model_loaded: false`, and runs on dead reckoning.
Conflict detection is unaffected. Every alert raised in that state carries the
reason code `physics_prediction_only`, so it is identifiable afterwards rather
than indistinguishable.

## Next

M4 — the real-time surface and the full test pyramid: WebSocket streaming,
contract tests against the OpenAPI spec, integration tests on real containers,
an automated end-to-end test, and the latency budget that turns "every second
matters" into a number.
