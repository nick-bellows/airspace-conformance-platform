# Conformance monitoring, measured

Generated 2026-08-18T00:14:09.390755+00:00 · seed `20260815` · generator `acp-gen-v1`

Reproduce with `python eval/run_conformance_eval.py --scenarios 120`.

Ground truth is the simulator's flight plan — `TurnTo`, `ClimbTo` and
`ChangeSpeed` commands at known times — which no part of the pipeline
observes. A manoeuvre counts as detected if a `NON_CONFORMANCE` advisory
was raised for that aircraft within 240 s of it beginning.

**Lag, not lead.** Conformance monitoring cannot know about a turn until
the aircraft has failed to be where it was predicted, so it is scored on
how long it takes to notice. This is the opposite of the conflict
detector, and a positive number here is not the failure a negative lead
time would be.

### Shifted family — the headline

120 scenarios · 30.0 simulated hours · 1632 manoeuvres in the plans

| Metric | Value |
| --- | --- |
| Manoeuvres detected | 320 / 1632 |
| Recall | 0.1961 |
| Recall, first manoeuvre per aircraft | 0.1471 (816) |
| Recall, later manoeuvres | 0.2451 (816) |
| Advisories raised | 377 |
| Precision | 1.0 |
| Unattributed advisories per hour | 0.0 |
| Median lag to notice | 49.6 s |
| p90 lag | 60.3 s |

| Manoeuvre | Count | Detected | Recall |
| --- | --- | --- | --- |
| ChangeSpeed | 338 | 8 | 0.0237 |
| ClimbTo | 568 | 7 | 0.0123 |
| TurnTo | 726 | 305 | 0.4201 |

### Nominal family — few manoeuvres, wide interval

123 scenarios · 20.75 simulated hours · 94 manoeuvres in the plans

| Metric | Value |
| --- | --- |
| Manoeuvres detected | 22 / 94 |
| Recall | 0.234 |
| Recall, first manoeuvre per aircraft | 0.2283 (92) |
| Recall, later manoeuvres | 0.5 (2) |
| Advisories raised | 22 |
| Precision | 1.0 |
| Unattributed advisories per hour | 0.0 |
| Median lag to notice | 47.4 s |
| p90 lag | 54.0 s |

| Manoeuvre | Count | Detected | Recall |
| --- | --- | --- | --- |
| ChangeSpeed | 22 | 0 | 0.0 |
| ClimbTo | 27 | 0 | 0.0 |
| TurnTo | 45 | 22 | 0.4889 |

## What this says

**It is a turn detector, not a conformance monitor.** Across 1,632
manoeuvres it finds 42% of turns, 2% of speed changes, and 1% of climbs.
The mechanism is not subtle once measured: the monitor thresholds the
*horizontal* distance between where an aircraft was predicted to be and
where it is. A climb barely moves an aircraft horizontally, so a purely
horizontal error metric is close to blind to it by construction. Nothing
in the system compares predicted altitude against observed altitude.

**It never cries wolf.** Precision is 1.00 — every advisory raised across
both families corresponded to a real manoeuvre, and there were no
unattributed advisories at all. That is worth something, but a detector
with recall 0.20 and precision 1.00 has chosen a very conservative
threshold, and the honest reading is that it fires only on the manoeuvres
large enough to be unmissable.

**It is not the alert lifecycle suppressing it.** An aircraft can hold only
one active non-conformance advisory, so a second manoeuvre during the first
could have been structurally unable to raise one. It is not: recall on
later manoeuvres is *higher* than on first ones (0.245 vs 0.147 on
shifted). The confound was checked rather than assumed away.

**Shifted is the number to quote.** The trajectory predictor inside the
monitor was trained on the training family, so scoring there flatters it;
shifted is a manoeuvre mix nothing in model selection touched.

**Nominal has a small denominator.** 94 manoeuvres against shifted's 1,632,
so its recall is estimated from far fewer events and should not be compared
to shifted as though the intervals were similar. That the two agree to
within a few points is reassuring rather than conclusive.

