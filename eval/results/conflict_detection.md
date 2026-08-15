# Conflict detection evaluation

Generated: 2026-08-15T19:03:28.605636+00:00

| Component | Version |
| --- | --- |
| Simulator | `acp-sim-v2` |
| Scenario generator | `acp-gen-v1` |
| Track filter | `acp-kf-cv-v1` |
| Detector | `acp-separation-v1` |

| Input | Value |
| --- | --- |
| Scenario family | `nominal` |
| Scenarios | 123 |
| Simulated airspace time | 20.75 hours |
| Scenario set SHA-256 (first 16) | `d7f7c5559189b760` |
| Horizontal standard | 5.0 NM |
| Vertical standard | 1000.0 ft |
| Lookahead | 300.0 s |

## Results

| Metric | Value |
| --- | --- |
| Truth conflict events | 39 |
| Detected before violation | 39 |
| **Recall** | **1.0** |
| Alerts raised | 68 |
| False alerts | 29 |
| **Precision** | **0.5735** |
| False alerts per airspace hour | 1.4 |
| Median warning lead time | 249.0 s |
| 10th percentile lead time | 146.0 s |
| Lead time range | 81.0 - 316.0 s |

## How to read this

The detector consumed **only** noisy surveillance reports. Ground truth was
computed from noiseless simulator state that no part of the pipeline observes.
The measurement is therefore not circular: a real algorithm, on degraded input,
scored against what actually happened.

**Recall** is the fraction of real losses of separation for which an alert was
raised *before* separation was lost. An alert raised after the fact is not
counted as a detection.

**Precision** is the fraction of raised alerts that concerned a pair which
really did lose separation. Encounters are generated with randomised miss
distances straddling the 5 NM standard, so the set contains close passes the
detector is supposed to stay quiet about. Without those, precision would be
meaningless.

**Lead time** is how long before the violation the alert appeared. The 10th
percentile matters more than the median: it is the bad case.

## Interpretation

**Recall of 1.0 is weaker evidence than it looks.** The generated
encounters are mostly constant-velocity approaches developing over four to seven
minutes, and the detector extrapolates at constant velocity over a
300 s window. It is being tested largely on the case its
model fits exactly. The hard case -- a conflict created by a late, unforecast
turn -- is under-represented, because the generator's manoeuvres fire between 60
and 240 seconds, usually before the encounter geometry matures. Read this number
as "the geometry is implemented correctly", not as "this detector never misses".

**Precision of 0.5735 is the real result, and it is not good.**
29 of 68 alerts were raised for pairs
that never actually lost separation. The cause is structural rather than a
defect: the detector applies a hard threshold to a point estimate. A pair
predicted to miss by 4.9 NM alerts and a pair predicted at 5.1 NM does not,
while the velocity estimate driving that prediction carries a couple of knots of
noise -- which over 300 s of extrapolation is more than a
nautical mile of position uncertainty. Encounters engineered to pass at 5-6 NM
therefore fall either side of the line close to arbitrarily.

Three changes would improve it, in descending order of value:

1. **Probabilistic detection.** The Kalman filter already maintains a position
   covariance. Propagating it through the extrapolation and alerting on the
   *probability* of violation, rather than thresholding a point estimate, is the
   principled fix and would let the threshold be set by an acceptable false
   alert rate instead of by geometry.
2. **Persistence.** Require the same pair to be detected on several consecutive
   scans before raising. Cheap, and removes alerts caused by one noisy estimate.
   The alert lifecycle already has the hysteresis machinery for it, applied on
   clearing but not on raising.
3. **Intent.** Flight plans would remove the constant-velocity assumption
   entirely. Unavailable here by construction -- the simulator's plans are never
   published -- and the largest single source of remaining error.

**1.4 false alerts per airspace hour** is the number
to quote operationally: roughly one spurious alert per hour of traffic. That is
too many for anyone to use, and improving it is the obvious next piece of work
on this detector.

## What this does not measure

Real-world performance. The traffic, the manoeuvre distribution, the encounter
rate, and the sensor error model are all inventions of this project -- and real
airspace contains vastly fewer conflicts per flying hour than this scenario set
does, by design, so the false-alert rate here is not comparable to an
operational one. These numbers characterise the algorithm and the pipeline, not
the airspace. See `docs/limitations.md`.

## Reproduce

```
python eval/run_conflict_eval.py --scenarios 120 --family nominal --seed 20260815 --include-committed
```
