# Deterministic vs probabilistic conflict detection

Generated 2026-08-17T02:13:11.858892+00:00 · seed `20260815` · 123 scenarios · 20.75 simulated hours

Scenario set SHA-256/16 `9c3989e981bd424b` · generator `acp-gen-v1` · detector `acp-separation-v1`

Reproduce with `python eval/run_detector_comparison.py --scenarios 120 --seed 20260815`.

## The result

| Detector | Recall | Precision | Alerts | False | False/hr | Median lead | Precision vs deterministic (95% CI) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic | 1.0 | 0.5735 | 68 | 29 | 1.4 | 249.0 s | baseline |
| probabilistic p>=0.01 | 1.0 | 0.4105 | 95 | 56 | 2.7 | 257.0 s | -0.1630 [-0.2302, -0.1056] |
| probabilistic p>=0.05 | 1.0 | 0.4239 | 92 | 53 | 2.55 | 257.0 s | -0.1496 [-0.2151, -0.0935] |
| probabilistic p>=0.1 | 1.0 | 0.4382 | 89 | 50 | 2.41 | 257.0 s | -0.1353 [-0.1994, -0.0815] |
| probabilistic p>=0.2 | 1.0 | 0.4535 | 86 | 47 | 2.27 | 257.0 s | -0.1200 [-0.1811, -0.0692] |
| probabilistic p>=0.35 | 1.0 | 0.5065 | 77 | 38 | 1.83 | 257.0 s | -0.0670 [-0.1150, -0.0276] |
| probabilistic p>=0.5 | 1.0 | 0.6094 | 64 | 25 | 1.2 | 249.0 s | +0.0358 [+0.0079, +0.0762] |
| probabilistic p>=0.7 | 0.9744 | 0.7091 | 55 | 16 | 0.77 | 234.0 s | +0.1356 [+0.0694, +0.2127] |

Ground truth comes from noiseless simulator state that no part of the
pipeline sees, so this comparison is not circular: both detectors consume
the same degraded observation stream and are scored against what actually
happened. Only the traffic is invented.

## Reading it

The deterministic detector is the first row: recall 1.0, precision 0.5735, 29 false alerts.
Every probabilistic row below is the *same traffic* judged by probability
instead of by which side of a line the mean landed.

A row only beats the baseline if it raises precision **without** dropping
recall. Recall is the number that matters most here -- a missed loss of
separation is the failure this system exists to prevent, and trading it for
a tidier precision would be the wrong deal at any exchange rate.

