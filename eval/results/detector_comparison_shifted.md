# Deterministic vs probabilistic conflict detection

Generated 2026-09-04T19:14:16.648336+00:00 · seed `20260815` · 120 scenarios · 30.0 simulated hours

Scenario set SHA-256/16 `c973a5d83326c7ed` · generator `acp-gen-v1` · detector `acp-separation-v2`

Reproduce with `python eval/run_detector_comparison.py --scenarios 120 --seed 20260815 --family shifted`.

## The result

| Detector | Recall | Precision | Alerts | False | False/hr | Median lead | Precision vs deterministic (95% CI) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| deterministic | 0.9667 | 0.4 | 70 | 42 | 1.4 | 155.0 s | baseline |
| probabilistic p>=0.01 | 0.9667 | 0.2593 | 108 | 80 | 2.67 | 244.0 s | -0.1407 [-0.2003, -0.0913] |
| probabilistic p>=0.05 | 0.9667 | 0.2828 | 99 | 71 | 2.37 | 237.0 s | -0.1172 [-0.1733, -0.0717] |
| probabilistic p>=0.1 | 0.9667 | 0.2947 | 95 | 67 | 2.23 | 216.0 s | -0.1053 [-0.1583, -0.0620] |
| probabilistic p>=0.2 | 0.9667 | 0.3182 | 88 | 60 | 2.0 | 200.0 s | -0.0818 [-0.1292, -0.0436] |
| probabilistic p>=0.35 | 0.9667 | 0.3784 | 74 | 46 | 1.53 | 155.0 s | -0.0216 [-0.0513, +0.0000] — spans zero |
| probabilistic p>=0.5 | 0.9667 | 0.4058 | 69 | 41 | 1.37 | 155.0 s | +0.0058 [+0.0000, +0.0205] — spans zero |
| probabilistic p>=0.7 | 0.9667 | 0.459 | 61 | 33 | 1.1 | 155.0 s | +0.0590 [+0.0246, +0.1014] |

Ground truth comes from noiseless simulator state that no part of the
pipeline sees, so this comparison is not circular: both detectors consume
the same degraded observation stream and are scored against what actually
happened. Only the traffic is invented.

## Reading it

The deterministic detector is the first row: recall 0.9667, precision 0.4, 42 false alerts.
Every probabilistic row below is the *same traffic* judged by probability
instead of by which side of a line the mean landed.

A row only beats the baseline if it raises precision **without** dropping
recall. Recall is the number that matters most here -- a missed loss of
separation is the failure this system exists to prevent, and trading it for
a tidier precision would be the wrong deal at any exchange rate.

