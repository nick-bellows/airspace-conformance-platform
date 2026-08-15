# Limitations

Status: `draft` — kept current as milestones land. Every number this project
publishes is qualified here.

Read [`safety-notes.md`](safety-notes.md) first for what the system is and is
not. This document is narrower: what the *measurements* do and do not support.

---

## 1. The data is synthetic, and that is the main constraint

Every position this system has ever processed came from `src/acp/sim/`. It has
never been connected to ADS-B, radar, multilateration, or any operational feed.
[ADR 0002](adr/0002-synthetic-traffic-only.md) records why.

**What that costs, precisely:**

- The **manoeuvre distribution is invented.** Aircraft turn, climb, and change
  speed according to probabilities chosen to make interesting traffic, not
  measured from anything. Any result that depends on how often aircraft
  manoeuvre inherits that invention.
- The **sensor error model is a plausible guess.** 30 m position sigma and a 2%
  dropout rate are loosely representative of ADS-B; they are not a calibration.
- The **encounter rate is wildly unrealistic.** The evaluation scenario set
  stages a close encounter in essentially every scenario. Real airspace produces
  vastly fewer conflicts per flying hour. **The false-alert-per-hour figure in
  `eval/results/conflict_detection.md` is therefore not comparable to an
  operational rate** — it is a rate per hour of deliberately adversarial traffic.

**What it does not cost.** The conflict-detection metrics are not circular. The
detector consumes only the noisy observation stream; ground truth is computed
from noiseless simulator state that no part of the pipeline sees. It is a real
algorithm on degraded input, scored against what actually happened. What is
invented is the *traffic*, not the *measurement*.

**What would close the gap.** A local capture of real ADS-B, retraining and
re-scoring against it, and reporting both numbers side by side. The data would
stay outside version control. This is not currently planned.

---

## 2. Data association is absent by construction

Surveillance reports carry an aircraft identifier, so the tracker always knows
which aircraft a report belongs to. **Real radar does not work like this.** The
hard part of real tracking — deciding which return belongs to which track when
several aircraft are close together, and handling the ones that belong to none —
is skipped entirely.

This makes the tracker considerably easier than a real one, and it means the
track-continuity results here say nothing about performance in dense traffic.

---

## 3. There is no intent data

No flight plans, no clearances, no controller inputs, no airspace structure.

Consequences that show up directly in the numbers:

- The conflict detector extrapolates at **constant velocity**. It cannot know an
  aircraft is about to turn at a waypoint, so a conflict that only exists
  because of a planned turn is invisible to it, and a conflict that a planned
  turn would resolve is reported anyway.
- "Non-conformance" can only mean *the aircraft did not do what constant-velocity
  physics predicted*. It cannot mean *the aircraft did not do what it was told*,
  because nothing here knows what it was told.

Per the evaluation report, this is the single largest remaining source of false
alerts.

---

## 4. Known weaknesses in the current detector

From `eval/results/conflict_detection.md`, and stated here so they are not
only visible to someone who opens the report:

- **Precision is around 0.57.** Roughly two in five alerts concern a pair that
  never actually loses separation. The cause is thresholding a point estimate: a
  predicted 4.9 NM miss alerts and a 5.1 NM miss does not, while the velocity
  estimate behind that prediction carries enough noise to move the answer across
  the line. The principled fix is probabilistic detection using the covariance
  the Kalman filter already maintains.
- **Recall is high partly because the test is easy.** The generated encounters
  are mostly constant-velocity approaches, which is exactly the assumption the
  detector makes. Conflicts created by a late, unforecast turn are
  under-represented in the scenario set. Recall against those would be worse and
  is currently unmeasured.
- **The tracking filter is constant-velocity**, so it lags during turns. That is
  deliberate ([ADR 0006](adr/0006-constant-velocity-filter.md)) because the lag
  is the manoeuvre signal, but it does mean reported position is least accurate
  exactly when an aircraft is manoeuvring.

---

## 5. Scale and operations

- **Track identity is simplified.** One track per aircraft address, permanently.
  A real system issues a fresh track number when an aircraft reappears after a
  long absence; here two separate flights by the same airframe would share a
  track.
- **No dead-letter queue.** A message that fails validation is logged and
  skipped so one bad record cannot stall the airspace picture, but it is then
  gone. A production deployment would route it somewhere.
- **Two services share a database schema** ([ADR 0004](adr/0004-shared-read-model-between-tracker-and-api.md)),
  which is a recognised coupling. Tolerable here because exactly one service
  writes.
- **Everything releases together.** The services are independently deployable
  but not independently versioned, so this does not exercise staged rollout of a
  contract change ([ADR 0003](adr/0003-shared-library-with-enforced-service-isolation.md)).
- **Throughput and latency are unmeasured.** The scan interval and the pairwise
  search are the obvious bottlenecks and neither has been profiled. That is M4.

---

## 6. What is measured, and where

| Claim | Evidence | Caveat |
| --- | --- | --- |
| Conflict detection recall, precision, lead time | `eval/results/conflict_detection.md` | Synthetic traffic; unrealistic encounter rate |
| The filter reduces position error | `tests/unit/test_kalman.py` | Against the simulator's noise model only |
| Services do not import each other | `tests/unit/test_architecture.py` | Static import graph; says nothing about runtime coupling |
| Idempotent writes under redelivery | `tests/unit/test_runners.py`, live database check | Not yet tested against real consumer restarts (M5) |
| End-to-end pipeline works | Manual verification, screenshots | Not yet automated (M4) |

No metric appears in the README until its runner is committed and reproducible
from a recorded seed.
