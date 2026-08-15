# Model card — trajectory residual predictor

Status: `validated` on synthetic traffic. **Not validated on real traffic**, and
nothing here supports a claim about real-world accuracy.

| | |
| --- | --- |
| Version | `acp-residual-v1` |
| Features | `acp-features-v1` (20 inputs, 20 s window) |
| Dataset | `acp-dataset-v1` |
| Simulator / generator | `acp-sim-v2` / `acp-gen-v1` |
| Artifacts | `models/residual_neural_{30,60,120}s.pt` |
| Parameters | 3,523 per horizon |
| Framework | PyTorch 2.13 (CPU) |
| Evidence | [`eval/results/trajectory_prediction.md`](../../eval/results/trajectory_prediction.md) |

## What it does

Predicts the **residual** of a dead-reckoning prediction: how far along-track,
across-track, and vertically an aircraft will end up relative to where constant
velocity says it will be. The served prediction is dead reckoning plus that
correction.

Three separately trained models, one per horizon: 30 s, 60 s, 120 s.

## Inputs

Twenty seconds of filtered track updates — the same messages the conformance
service consumes. Twenty features: current kinematics, recent dynamics over the
window, the Kalman filter's innovation, and four coarse regime flags. The full
list is in `FEATURE_NAMES`, and the order is part of the artifact contract.

**It cannot see intent.** The simulator's flight plans are never published on any
topic. This is structural rather than a matter of discipline: the feature
extractor takes a sequence of `TrackUpdate` and nothing else.

## Results

Horizontal error while **turning**, which is the only regime where horizontal
prediction is hard (60 s horizon, median):

| Split | Dead reckoning | Neural | Skill | Samples |
| --- | --- | --- | --- | --- |
| Same family, unseen scenarios | 2.412 NM | **1.220 NM** | **+49.4%** | 4,495 |
| Shifted family (different airspace) | 2.024 NM | **1.261 NM** | **+37.7%** | 3,830 |

At 30 s: +59.1% / +47.2%. At 120 s: +46.3% / +40.8%.

The gap between the two columns is the cost of distribution shift, and it is
real: skill drops by about a quarter when the airspace changes. It does not
collapse, which is the useful finding.

## Where it does not help, and where it hurts

- **Cruise and climb, horizontally.** Dead reckoning is already accurate to a few
  tens of metres. There is nothing to win and the model wins nothing.
- **Altitude at 30 s: the model is worse than the baseline.** 31 ft for dead
  reckoning against 98 ft for the model. Vertical prediction at short horizons is
  already essentially solved by physics and the model adds noise. It earns its
  place vertically only at 60 s (563 → 305 ft) and 120 s (1,828 → 450 ft). **A
  deployment that cared about 30 s altitude should use the baseline.**

## Why a neural network rather than the linear model

Both were trained on identical features against the same target. The winner is
chosen on the validation split, on the turning stratum, and only if it beats
ridge by at least 3%.

Neural won at every horizon: +32.7% at 30 s, +23.5% at 60 s, +17.6% at 120 s. The
margin narrows as the horizon grows. **If a deployment valued inspectability over
that margin, shipping ridge would be defensible on these numbers** — the linear
model's weights are readable and are recorded in the JSON report.

## How it fails

- **No model artifact, corrupt artifact, or version mismatch** → the predictor
  logs loudly and returns dead reckoning. The service starts and conflict
  detection is unaffected.
- **Inference raises, or the output contains NaN or infinity** → dead reckoning,
  logged.
- **Track shorter than the 20 s window** → dead reckoning.

Every prediction records which path produced it, and that reaches the alert
evidence, so an alert raised while the model was unavailable is identifiable
afterwards rather than indistinguishable.

The worst case is by construction the physics baseline, because the model
predicts a correction rather than a position. A model that outputs zeros *is*
dead reckoning.

## Training

- 120 manoeuvre-rich scenarios, split **by scenario** into 72 train / 18
  validation / 30 test, plus 40 shifted-family scenarios as a second test set.
  Splitting by sample would put near-duplicate windows on both sides and make
  the held-out numbers meaningless.
- Huber loss, not squared error: trajectory residuals have a long tail and a
  handful of hard turns would otherwise dominate every gradient.
- Targets scaled per-axis, because altitude error in feet and position error in
  nautical miles differ by orders of magnitude and an unscaled loss would train
  almost entirely on altitude.
- Early stopping on validation loss, patience 15.
- Feature standardisation fitted on the **training split only**.
- Deterministic: the seed fixes scenario generation, the split, and torch's
  initialisation and batch order. Re-running reproduces the numbers exactly.

## Why the artifacts are committed

They are 17 KB each, derived entirely from this project's own synthetic data
(no licence question), and the conformance service needs one at runtime.
Training during the image build would be slow and would produce a different file
on every build. Reproducibility comes from the recorded seed and versions, not
from re-training.

## Limitations

**Simulated aircraft hold heading and speed exactly.** There is no wind, no
turbulence, and no variation between autopilots or crews. Real 60-second
trajectory prediction error is substantially larger than anything reported here,
and the size of that gap is unmeasured.

**The manoeuvre distribution is invented and unrealistically dense.** The
training family gives most aircraft several manoeuvres in fifteen minutes, far
more than real en-route traffic. That was deliberate — NOMINAL produced a
held-out set with seventeen turning samples, which is not a measurement — but it
means the *proportions* between strata say nothing about real airspace.

**The model has never seen a go-around, a holding pattern, a weather deviation,
or a diversion**, because the simulator cannot produce them.

**Nothing about this model is certified.** See
[`docs/safety-notes.md`](../safety-notes.md).
