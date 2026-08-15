# Trajectory prediction evaluation

Generated: 2026-08-15T18:38:42.783655+00:00

| Component | Version |
| --- | --- |
| sim | `acp-sim-v2` |
| generator | `acp-gen-v1` |
| features | `acp-features-v1` |
| dataset | `acp-dataset-v1` |
| model | `acp-residual-v1` |

Seed `20260815`. Scenario counts per split: train 72, validation 18, test_same_family 30, test_shifted 40

## What is being predicted

Where an aircraft will be after a fixed horizon, using **only filtered
track updates** -- the same messages the conformance service receives.
The simulator's flight plans are never published and no predictor can
see them, so the model cannot invert the generator; it can only learn
how aircraft in this airspace tend to behave.

Both models predict a **residual**: the correction to a dead-reckoning
prediction, decomposed into along-track, cross-track, and altitude
error. A model that outputs zero degrades exactly to dead reckoning,
so the worst case is the baseline rather than nonsense.

## How to read the strata

Results are grouped by what the aircraft was truly doing when the
prediction was made. **The turning stratum is the result**; everything
else is close to free.

In cruise and in a climb, an aircraft is going where a straight line
says it is going, and dead reckoning is already accurate to a few tens
of metres. Averaging those in produces a headline number dominated by
samples nobody needed a model for. An earlier version of this report
did exactly that and made the model look useless.

Climbs are judged on **altitude** error rather than horizontal, for the
same reason: a climbing aircraft barely moves sideways.

## Horizon 30s

Winner on validation: **neural** (neural improved on ridge by +32.7%, threshold 3%). Shipped artifact: `residual_neural_30s.pt`.

### Turning — horizontal error

**Same family (unseen scenarios)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 3.6522 NM | 4.044 NM | -262.8% | 4495 |
| `dead_reckoning` | 1.0066 NM | 1.7193 NM | +0.0% | 4495 |
| `constant_turn` | 0.6548 NM | 1.7301 NM | +34.9% | 4495 |
| `ridge` | 0.6409 NM | 1.3739 NM | +36.3% | 4495 |
| `neural` | 0.412 NM | 1.0971 NM | +59.1% | 4495 |

**Shifted family (different airspace)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 3.3635 NM | 4.4092 NM | -292.6% | 3830 |
| `dead_reckoning` | 0.8567 NM | 1.6536 NM | +0.0% | 3830 |
| `constant_turn` | 0.6045 NM | 1.6588 NM | +29.4% | 3830 |
| `ridge` | 0.5189 NM | 1.3412 NM | +39.4% | 3830 |
| `neural` | 0.4522 NM | 1.1205 NM | +47.2% | 3830 |

### Climbing or descending — altitude error

| Model | Median altitude error | Samples |
| --- | --- | --- |
| `persistence` | 516.0 ft | 12318 |
| `dead_reckoning` | 31.0 ft | 12318 |
| `constant_turn` | 31.0 ft | 12318 |
| `ridge` | 141.2 ft | 12318 |
| `neural` | 97.7 ft | 12318 |

### Cruise — horizontal error

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 3.6298 NM | 4.0686 NM | -14057.4% | 176133 |
| `dead_reckoning` | 0.0256 NM | 0.0567 NM | +0.0% | 176133 |
| `constant_turn` | 0.3197 NM | 0.7961 NM | -1147.0% | 176133 |
| `ridge` | 0.0473 NM | 0.116 NM | -84.3% | 176133 |
| `neural` | 0.0184 NM | 0.0416 NM | +28.1% | 176133 |

## Horizon 60s

Winner on validation: **neural** (neural improved on ridge by +23.5%, threshold 3%). Shipped artifact: `residual_neural_60s.pt`.

### Turning — horizontal error

**Same family (unseen scenarios)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 7.2655 NM | 8.0919 NM | -201.3% | 4495 |
| `dead_reckoning` | 2.4116 NM | 4.5098 NM | +0.0% | 4495 |
| `constant_turn` | 3.7304 NM | 7.1169 NM | -54.7% | 4495 |
| `ridge` | 1.5253 NM | 3.8767 NM | +36.8% | 4495 |
| `neural` | 1.2203 NM | 3.3817 NM | +49.4% | 4495 |

**Shifted family (different airspace)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 6.7232 NM | 8.8131 NM | -232.1% | 3830 |
| `dead_reckoning` | 2.0242 NM | 4.3013 NM | +0.0% | 3830 |
| `constant_turn` | 3.5407 NM | 6.9751 NM | -74.9% | 3830 |
| `ridge` | 1.222 NM | 3.5839 NM | +39.6% | 3830 |
| `neural` | 1.2608 NM | 3.1107 NM | +37.7% | 3830 |

### Climbing or descending — altitude error

| Model | Median altitude error | Samples |
| --- | --- | --- |
| `persistence` | 773.8 ft | 12318 |
| `dead_reckoning` | 562.9 ft | 12318 |
| `constant_turn` | 562.9 ft | 12318 |
| `ridge` | 356.5 ft | 12318 |
| `neural` | 304.7 ft | 12318 |

### Cruise — horizontal error

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 7.2524 NM | 8.1283 NM | -14321.4% | 169303 |
| `dead_reckoning` | 0.0503 NM | 0.1581 NM | +0.0% | 169303 |
| `constant_turn` | 1.2626 NM | 3.112 NM | -2410.7% | 169303 |
| `ridge` | 0.1244 NM | 0.3466 NM | -147.3% | 169303 |
| `neural` | 0.0347 NM | 0.1497 NM | +31.0% | 169303 |

## Horizon 120s

Winner on validation: **neural** (neural improved on ridge by +17.6%, threshold 3%). Shipped artifact: `residual_neural_120s.pt`.

### Turning — horizontal error

**Same family (unseen scenarios)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 14.5039 NM | 16.1999 NM | -185.2% | 4495 |
| `dead_reckoning` | 5.0851 NM | 11.1181 NM | +0.0% | 4495 |
| `constant_turn` | 14.3219 NM | 17.9374 NM | -181.7% | 4495 |
| `ridge` | 3.3148 NM | 9.603 NM | +34.8% | 4495 |
| `neural` | 2.7318 NM | 8.3825 NM | +46.3% | 4495 |

**Shifted family (different airspace)**

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 13.5078 NM | 17.5937 NM | -222.3% | 3830 |
| `dead_reckoning` | 4.1915 NM | 9.5807 NM | +0.0% | 3830 |
| `constant_turn` | 13.1295 NM | 19.0421 NM | -213.2% | 3830 |
| `ridge` | 2.6454 NM | 8.1028 NM | +36.9% | 3830 |
| `neural` | 2.4804 NM | 6.6111 NM | +40.8% | 3830 |

### Climbing or descending — altitude error

| Model | Median altitude error | Samples |
| --- | --- | --- |
| `persistence` | 792.7 ft | 12311 |
| `dead_reckoning` | 1828.4 ft | 12311 |
| `constant_turn` | 1828.4 ft | 12311 |
| `ridge` | 497.8 ft | 12311 |
| `neural` | 449.5 ft | 12311 |

### Cruise — horizontal error

| Model | Median | p90 | Skill vs dead reckoning | Samples |
| --- | --- | --- | --- | --- |
| `persistence` | 14.4597 NM | 16.2263 NM | -12868.3% | 155649 |
| `dead_reckoning` | 0.1115 NM | 1.858 NM | +0.0% | 155649 |
| `constant_turn` | 4.9598 NM | 11.5778 NM | -4348.3% | 155649 |
| `ridge` | 0.3836 NM | 2.0351 NM | -244.0% | 155649 |
| `neural` | 0.0962 NM | 1.9424 NM | +13.7% | 155649 |

## Findings

**The model roughly halves horizontal error during turns**, which is
the only regime where horizontal prediction is hard. It also holds up
under distribution shift: skill drops when the airspace changes, but
does not collapse. That drop is the honest cost of training on one
traffic distribution and flying in another.

**The constant-turn baseline is worse than assuming straight flight.**
Extrapolating the estimated turn rate for a minute amplifies its noise
into miles of arc error -- the turn-rate estimate is noisier than the
signal it carries. This is a useful negative result: the obvious
physics improvement over dead reckoning makes things worse.

**At the shortest horizon the model degrades altitude prediction.**
Dead reckoning's vertical error at 30 s is already a few tens of feet,
and the model adds noise rather than removing it. It only earns its
place vertically at 60 s and beyond. A deployment that cared about
30 s altitude should use the baseline.

**The neural network beats ridge, but not by enough to be obvious.**
Both are trained on identical features and predict the same residual.
The margin narrows as the horizon grows. If a deployment valued
inspectability, shipping ridge would be defensible on these numbers.

## What this does not measure

Real-world accuracy. Simulated aircraft hold heading and speed exactly
and there is no wind, no turbulence, and no variation between
autopilots or crews. Real 60-second prediction error is substantially
larger than anything here, and the gap is unmeasured. See
`docs/limitations.md`.

The manoeuvre distribution is also invented: the training family gives
most aircraft several manoeuvres in fifteen minutes, which is far more
than real en-route traffic. That was a deliberate choice to get enough
turning samples to measure anything, and it means the *proportions* in
these strata say nothing about real airspace.

## Reproduce

```
python -m acp.ml.train --scenarios 120 --seed 20260815
```

Roughly seven minutes on a laptop CPU. Deterministic: the seed fixes
scenario generation, the train/validation/test split, and torch's
initialisation and batch order.
