# Deterministic vs probabilistic conflict detection

Generated 2026-09-04T19:04:52.733566+00:00 · seed `20260815` · 123 scenarios · 20.75 simulated hours

Scenario set SHA-256/16 `9c3989e981bd424b` · generator `acp-gen-v1` · detector `acp-separation-v2`

Reproduce with `python eval/run_detector_comparison.py --scenarios 120 --seed 20260815 --family nominal`.

## The result

| Detector | Recall | Precision | Alerts | False | False/hr | Median lead | Precision vs deterministic (95% CI) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic | 1.0 | 0.5571 | 70 | 31 | 1.49 | 249.0 s | baseline |
| probabilistic p>=0.01 | 1.0 | 0.4105 | 95 | 56 | 2.7 | 257.0 s | -0.1466 [-0.2092, -0.0936] |
| probabilistic p>=0.05 | 1.0 | 0.4239 | 92 | 53 | 2.55 | 257.0 s | -0.1332 [-0.1946, -0.0810] |
| probabilistic p>=0.1 | 1.0 | 0.4382 | 89 | 50 | 2.41 | 257.0 s | -0.1189 [-0.1764, -0.0691] |
| probabilistic p>=0.2 | 1.0 | 0.4535 | 86 | 47 | 2.27 | 257.0 s | -0.1037 [-0.1586, -0.0566] |
| probabilistic p>=0.35 | 1.0 | 0.5065 | 77 | 38 | 1.83 | 257.0 s | -0.0506 [-0.0917, -0.0171] |
| probabilistic p>=0.5 | 1.0 | 0.5909 | 66 | 27 | 1.3 | 249.0 s | +0.0338 [+0.0074, +0.0724] |
| probabilistic p>=0.7 | 1.0 | 0.7091 | 55 | 16 | 0.77 | 234.0 s | +0.1519 [+0.0839, +0.2321] |

Ground truth comes from noiseless simulator state that no part of the
pipeline sees, so this comparison is not circular: both detectors consume
the same degraded observation stream and are scored against what actually
happened. Only the traffic is invented.

## Reading it

The deterministic detector is the first row: recall 1.0, precision 0.5571, 31 false alerts.
Every probabilistic row below is the *same traffic* judged by probability
instead of by which side of a line the mean landed.

A row only beats the baseline if it raises precision **without** dropping
recall. Recall is the number that matters most here -- a missed loss of
separation is the failure this system exists to prevent, and trading it for
a tidier precision would be the wrong deal at any exchange rate.

