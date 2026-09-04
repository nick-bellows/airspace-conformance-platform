# What precision costs in lead time

Generated 2026-09-04T18:59:31.229770+00:00 · seed `20260815` · generator `acp-gen-v1` · detector `acp-separation-v2`

Reproduce with `python eval/run_lookahead_sweep.py --scenarios 120`.

Both detectors here are the deterministic one. The only thing changing is
how far ahead it is asked to look.

## Nominal family

| Lookahead | Recall | Precision | Alerts | False | False/hr | Median lead |
| --- | --- | --- | --- | --- | --- | --- |
| 60 s | 1.0 | 0.8667 | 45 | 6 | 0.29 | 60.0 s |
| 120 s | 1.0 | 0.8667 | 45 | 6 | 0.29 | 120.0 s |
| 180 s | 1.0 | 0.7222 | 54 | 15 | 0.72 | 180.0 s |
| 240 s | 1.0 | 0.6393 | 61 | 22 | 1.06 | 237.0 s |
| 300 s ←default | 1.0 | 0.5571 | 70 | 31 | 1.49 | 249.0 s |

## Shifted family

The family the operating point was *not* chosen on. A result that only
held on the nominal family would be the mistake ADR 0012 already made once.

| Lookahead | Recall | Precision | Alerts | False | False/hr | Median lead |
| --- | --- | --- | --- | --- | --- | --- |
| 60 s | 0.9667 | 0.7179 | 39 | 11 | 0.37 | 58.0 s |
| 120 s | 0.9667 | 0.6364 | 44 | 16 | 0.53 | 99.0 s |
| 180 s | 0.9667 | 0.549 | 51 | 23 | 0.77 | 109.0 s |
| 240 s | 0.9667 | 0.4667 | 60 | 32 | 1.07 | 155.0 s |
| 300 s ←default | 0.9667 | 0.4 | 70 | 42 | 1.4 | 155.0 s |

## What this says

**Precision 0.57 is mostly the price of a five-minute lookahead.** Halving
the lookahead to 120 s raises precision from 0.57 to 0.87 on nominal traffic
and from 0.40 to 0.65 on shifted traffic. The direction and rough magnitude
hold on both families, which is what distinguishes this from the
probabilistic-threshold result that did not replicate.

**It is not free, and the default does not change.** Lead time is the
product. At 300 s the median warning is around four minutes; at 120 s it is
two. On shifted traffic the shorter lookahead also costs recall — a real
loss of separation goes undetected that the longer lookahead catches. A
conflict detector that is precise because it warns too late has not
improved, it has changed the subject.

**What was actually wrong was the reporting, not the detector.** A single
precision figure quoted without the lookahead it was measured at describes
one point on this curve as though it were a property of the system. That is
the defect this report fixes.

