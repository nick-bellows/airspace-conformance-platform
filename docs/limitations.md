# Limitations

Every number this project publishes is qualified here.

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
  the line. **That explanation was tested and is not sufficient** — a
  probabilistic detector using the filter's covariance gains +0.006 precision,
  confidence interval spanning zero, on traffic it was not tuned against
  ([ADR 0012](adr/0012-probabilistic-conflict-detection.md)).
- **Precision without a lookahead attached is not a property of the system.**
  Examining the alerts rather than theorising about them found the median false
  alert is a pair that genuinely closed to 5.52 NM against a 5 NM standard, at a
  predicted time-to-CPA of 296 s against a 300 s ceiling. Shortening the
  lookahead to 120 s takes precision to 0.87 nominal and 0.65 shifted. The 0.57
  headline is one point on a curve, published in
  [`lookahead_tradeoff.md`](../eval/results/lookahead_tradeoff.md), and quoting
  it alone was the actual reporting defect.
- **The shorter lookahead is not a free win, which is why the default stands.**
  On shifted traffic a 120 s window misses a loss of separation that the 300 s
  window catches, and halves the median warning. Lead time is what the system is
  for.
- **The probabilistic detector's covariance model is isotropic.** It treats each
  track's horizontal uncertainty as circular when the filter's is mildly
  elliptical, and does not propagate turn rate, so a turning aircraft's future
  position is more uncertain than the model says. Neither error is quantified.
  Given the detector is not the default, they are recorded rather than fixed.
- **"Recall is high because the test is easy" was written here for four
  milestones and is not supported.** The claim was that constant-velocity
  encounters flatter a constant-velocity detector, so recall would fall on
  manoeuvring traffic. Sweeping manoeuvre density with every other parameter
  held fixed, recall stays at 1.00 and is *lowest* (0.977) with no manoeuvres at
  all. The detector re-evaluates every second against a five-minute horizon, so
  it only has to observe a turn, not forecast it, and it gets many chances per
  encounter. [`manoeuvre_sensitivity.md`](../eval/results/manoeuvre_sensitivity.md).
- **What manoeuvring traffic does cost is precision** — 0.78 with no manoeuvres
  down to 0.39 with every staged aircraft manoeuvring — and lead time, 276 s
  down to 211 s. Together with the lookahead sweep this is one effect seen
  twice: precision tracks how much constant-velocity error accumulates before
  closest approach, whether that comes from extrapolating further or from
  traffic that extrapolates worse.
- **Recall remains a small-sample figure.** 27 to 44 real events per
  configuration, so one detection either way moves it by two to four points. It
  is not measured precisely enough to distinguish 1.00 from 0.97.
- **The tracking filter is constant-velocity**, so it lags during turns. That is
  deliberate ([ADR 0006](adr/0006-constant-velocity-filter.md)) because the lag
  is the manoeuvre signal, but it does mean reported position is least accurate
  exactly when an aircraft is manoeuvring.
- **The conflict geometry has a range limit.** Every track is projected into one
  flat plane centred on the mean of the picture, and that distorts the distance
  between two aircraft in proportion to their distance from the centre:

  | Distance from centre | Error on a 4 NM gap |
  | --- | --- |
  | 100 NM | 0.05 NM |
  | 300 NM | 0.17 NM |
  | 800 NM | 0.61 NM |

  Against a 5 NM standard, past a few hundred miles this is no longer a rounding
  error. The monitor logs a warning above 300 NM rather than failing, because
  degraded detection beats none. The right answer at that scale is one monitor
  per airspace sector — which is a large part of why sectors exist.

  Found during the M2 review: the original docstring claimed "under 0.1% within
  100 NM", which was true of distance *from* the reference point but not of the
  distance *between two points both offset from it* — the quantity the detector
  actually computes. Measured, corrected, and pinned by a test.

---

## 4a. Known weaknesses in trajectory prediction

From `eval/results/trajectory_prediction.md` and the
[model card](cards/model-trajectory-predictor.md):

- **The model is worse than the baseline at predicting altitude 30 s ahead** —
  31 ft for dead reckoning against 98 ft. It only helps vertically at 60 s and
  beyond. A deployment that cared about short-horizon altitude should use
  physics.
- **It adds nothing in cruise or climb horizontally**, where a straight line is
  already accurate to tens of metres. All of the reported skill comes from turns.
- **Skill drops by about a quarter under distribution shift** (+49.4% → +37.7% at
  60 s). That is measured rather than assumed, and it is the cost of training on
  one traffic distribution and operating in another.
- **The neural network's advantage over the linear model is not statistically
  distinguishable on shifted traffic at 60 s or 120 s.** A scenario-clustered
  bootstrap over the 30 held-out scenarios gives −12.7% to +5.5% at 60 s and
  −1.0% to +13.4% at 120 s. Both models still beat dead reckoning decisively;
  what is uncertain is whether the extra capacity earns its place away from the
  training distribution. Shipping ridge would be defensible on these numbers.
- **The point estimates alone are misleading about precision.** 4,495 turning
  samples sounds like a large n, but consecutive samples are the same
  twenty-second window shifted by a second. The effective sample size is closer
  to the 30 scenarios, which is why every interval above is scenario-clustered.
- **Early stopping and model selection share one validation split**, so the
  reported validation figures carry a mild winner's curse. The test splits are
  untouched by either, so the headline numbers are unaffected — but a separate
  inner split for early stopping would be cleaner.
- **Simulated aircraft hold heading and speed exactly.** No wind, no turbulence,
  no variation between autopilots or crews. Real 60-second prediction error is
  substantially larger than anything reported here and **the size of that gap is
  unmeasured**.
- **The training family's manoeuvre density is unrealistic**, deliberately, so
  that enough turning samples existed to measure anything. Proportions between
  strata therefore say nothing about real airspace.
- **The model has never seen a go-around, a holding pattern, a weather
  deviation, or a diversion**, because the simulator cannot produce them.

### Conformance monitoring

- **It is a turn detector, not a conformance monitor.** Measured against the
  simulator's flight plan across 1,632 manoeuvres on shifted traffic, it finds
  **42% of turns, 2.4% of speed changes, and 1.2% of climbs** — overall recall
  0.196. The mechanism is structural: the monitor thresholds the *horizontal*
  distance between predicted and observed position, and a climb barely moves an
  aircraft horizontally, so nothing in the system compares predicted altitude
  against observed altitude. [`conformance_detection.md`](../eval/results/conformance_detection.md).
- **Its precision is 1.00, which is not the compliment it sounds like.** Across
  both families every advisory raised corresponded to a real manoeuvre and there
  were no unattributed advisories at all. A detector at recall 0.20 and
  precision 1.00 has a very conservative threshold: it fires only on manoeuvres
  too large to miss. The lifecycle was checked as a possible cause of the low
  recall and ruled out — later manoeuvres score *higher* than first ones.
- **The threshold factor has never been swept.** 3.0 is a judgement call. There
  is now a recall/precision measurement to sweep it against, which there was not
  before, but the sweep itself is unbuilt.
- **Median lag to notice a turn is 49.6 s**, p90 60.3 s — bounded by the 60 s
  prediction horizon, since a manoeuvre cannot surface until a prediction made
  before it matures. That is inherent to the design, not a tuning failure.
- **Thresholds are global constants per horizon, not per-prediction
  uncertainty.** They are calibrated to the *typical* turning error, so they are
  loose exactly where prediction is hardest and tight where it is easy. The
  principled fix is the same one the conflict detector needs: predict a
  distribution rather than a point — a quantile head via pinball loss, or a small
  ensemble — and threshold on probability. **This is the highest-value ML work
  remaining in the project** and it is not done.
- **A non-conformance alert cannot mean what an operator will read into it.** It
  says the aircraft did not do what *our model* predicted. Without flight plans
  or clearances, a perfectly normal turn onto a cleared heading is
  indistinguishable from an unexplained one. The alert text says so; that does
  not guarantee it will be read that way.

---

## 5. Scale and operations

These qualify the *system*, not the numbers above. What would be built to fix
them, and what is deliberately declined, is in [`future-work.md`](future-work.md).

- **Track identity is simplified.** One track per aircraft address, permanently.
  A real system issues a fresh track number when an aircraft reappears after a
  long absence; here two separate flights by the same airframe share a track.
- **No dead-letter queue.** A message failing validation is logged, counted, and
  skipped so one bad record cannot stall the airspace picture — but it is then
  gone.
- **The tracker commits Kafka offsets before its batched database flush.** A hard
  kill in between loses up to one flush interval — a second, or fifty updates —
  of track *history*, with no redelivery to recover it. The live picture is
  unaffected because it is rebuilt from the next report.
- **Two services share a database schema**
  ([ADR 0004](adr/0004-shared-read-model-between-tracker-and-api.md)), which is a
  recognised coupling. Tolerable here because exactly one service writes.
- **Everything releases together.** The services are independently deployable but
  not independently versioned, so this does not exercise staged rollout of a
  contract change.
- **Throughput is measured for the compute path only.** 500 aircraft, cycle p95
  251 ms against a 500 ms budget — see [`latency-budget.md`](latency-budget.md).
  Kafka, Postgres, and Redis are excluded, and **no deployed system has been
  measured**.
- **The live stream is server-side polling with push**, not event-driven. Update
  latency is bounded by the poll interval
  ([ADR 0009](adr/0009-one-reader-fans-out-to-many-viewers.md)).
- **The conformance service cannot be scaled out.** Every instance would hold the
  whole airspace picture and duplicate every alert.
- **Rebalance is handled, but filter state is not transferred.** A tracker replica
  that loses a partition releases those aircraft without terminating them
  ([ADR 0011](adr/0011-partition-ownership-is-state.md)); the new owner builds a
  fresh filter from the next report, so those tracks re-initiate with a wide
  covariance and take seconds to converge. A real accuracy dip during any
  deployment that moves partitions, and a deliberate trade against an
  inter-replica state-transfer protocol.
- **At-least-once *across* a rebalance is untested.** A report in flight when its
  partition is revoked is discarded rather than processed — the offset was never
  committed, so the new owner replays it — but the generator still commits
  afterwards, and a commit for a revoked partition raises `CommitFailedError`.
  That path predates the rebalance work and is asserted nowhere.
- **The rebalance fixes have only ever been exercised by unit tests**, which drive
  the interleavings deterministically with a fake. No test has run two real
  tracker replicas against a real broker and forced a real reassignment.
- **The perf job is `continue-on-error` in CI.** A budget that fails the build on
  a noisy shared runner teaches people to ignore the job — which does mean a
  genuine regression could land unblocked.

---

## 5a. Observability and deployment

- **The pipeline is green, and was unexecuted for six milestones.** Three
  external reviews found eighteen defects in it during that time, and the first
  four real runs found five more. Everything CI now asserts is asserted on
  every push; nothing here is a claim about a YAML file any more. What CI does
  *not* cover is unchanged and listed below.
- **The migration Job still crash-loops briefly on a cold start.** It has no
  init container, so `alembic upgrade head` fails until Postgres accepts
  connections and the Job's `backoffLimit` retries it. That is the mechanism a
  Job has for exactly this, and it completes — but the feed and the Kafka
  clients got `wait-for-kafka` init containers for the same problem, so the
  inconsistency is a tidiness gap rather than a considered difference.
- **One termination event during a local `kind` run is unexplained.** Two
  seconds after the first rebalance, a tracker replica logged `terminated stale
  tracks` for two aircraft that were still reporting. The most likely cause is
  the feed crash-loop that the `wait-for-kafka` init container has since fixed,
  leaving a gap longer than the 30 s termination timeout — which would make the
  termination *correct* — but a controlled test (stop the tracker, build a 90 s
  backlog, restart) did not reproduce it. Recorded as observed and not
  diagnosed rather than explained away.
- **Reproducibility is to a tolerance, not to the byte.** The scenario
  fingerprint that guards the committed evaluation rounds to nine decimal places
  before hashing, because `math.sin`, `cos`, `asin` and `atan2` differ in the
  last unit in the last place between MSVC and glibc. Sub-millimetre differences
  cannot move a 5 NM verdict, but "the same scenarios" here means to within
  0.1 mm rather than byte-identical files.
- **Nothing alerts.** Prometheus scrapes and Grafana draws, but there are no
  alerting rules and no Alertmanager, so "consumer lag is climbing" is visible
  only to someone already looking at the page.
- **Traces are sampled at 100%** and metrics are per process. Fine at four
  aircraft, wrong at real volume. `acp_live_tracks` from two tracker replicas is
  two partial pictures, which is why the panel sums them and why the workers are
  discovered by DNS rather than as a single static target.
- **The trace linking a report to its alert is a link, not a parent.** The scan
  runs on a timer over the whole picture, so it has no single cause. A reader
  expecting one continuous parent-child trace from feed to alert will not find
  one.
- **The per-report histogram measures the filter stage only** —
  `acp_report_filter_seconds` covers the Kalman update, not Kafka publication or
  the database write, matching the boundary `latency-budget.md` draws.
- **Build provenance is an image ID, not an attestation.** CI builds once and
  every consumer asserts the loaded image matches, so the published artefact is
  demonstrably the tested one. There is no signature and no SLSA provenance, so
  the chain holds within one workflow run and is unverifiable outside it.
- **The `kind` job proves the manifests start and pass traffic, not that they
  are production-shaped.** No node failure, no rolling update under load, no HPA.
  It did, however, find real resource pressure: CPU requests sized from the
  500-aircraft latency budget rather than the four aircraft the stack carries
  left the second tracker replica unschedulable on a two-core runner.
- **Infrastructure runs as Deployments with ephemeral storage**, so a Postgres
  restart loses track history. A StatefulSet would imply a durability this stack
  does not have.
- **The Postgres password is committed** so a clean checkout runs. It guards
  synthetic data on a loopback-bound port and is the wrong pattern for anything
  real. gitleaks allows that one literal and nothing else — verified by
  substitution, including inside a DSN and at two characters, both of which
  earlier versions of the rules missed.
- **Every workload names a mutable image tag.** `acp:dev` exists so a clean
  checkout runs with no registry; the cost is that redeploying needs
  `rollout restart` rather than `kubectl apply` alone.
- **There is no Ingress, NetworkPolicy, TLS, or authentication.** The WebSocket
  origin check exists because it becomes a Cross-Site WebSocket Hijacking
  vulnerability the moment authentication is added.

---

## 6. What is measured, and where

| Claim | Evidence | Caveat |
| --- | --- | --- |
| Conflict detection recall, precision, lead time | `eval/results/conflict_detection.md` | Synthetic traffic; unrealistic encounter rate; **one point on the lookahead curve** |
| Precision is largely a function of lookahead | `eval/results/lookahead_tradeoff.md` | Replicates on both families; default deliberately unchanged |
| False alerts are mostly genuine near-misses | `eval/results/false_alert_analysis.json` | Nominal family; descriptive, no fix proposed |
| Probabilistic detection does not beat the baseline | `eval/results/detector_comparison{,_shifted}.md` | Threshold chosen on nominal, invalidated on shifted |
| Recall is robust to manoeuvre density | `eval/results/manoeuvre_sensitivity.md` | 27–44 events per row; populations differ between rows |
| Conformance monitoring recall by manoeuvre type | `eval/results/conformance_detection.md` | Truth is simulator intent; shifted family is the headline |
| Trajectory model halves turning error | `eval/results/trajectory_prediction.md` | Turning stratum only; aircraft fly exactly |
| Skill survives distribution shift | Same report, `test_shifted` split | One shifted family, not a sweep |
| The model degrades to physics on failure | `tests/unit/test_ml.py`, `degradation` CI job | Covers missing, corrupt, NaN, and raising models |
| Features cannot see simulator intent | `tests/unit/test_ml.py`, contract shape | Structural, not a runtime check |
| The filter reduces position error | `tests/unit/test_kalman.py` | Against the simulator's noise model only |
| Services do not import each other | `tests/unit/test_architecture.py` | Static import graph |
| Idempotent writes under redelivery | `tests/unit/test_runners.py`, `tests/integration/` | Real consumer restart against the real constraint |
| Committed reports match their inputs | `tests/unit/test_generator.py` fingerprint | Covers NOMINAL only |
| The API obeys its own OpenAPI spec | `tests/contract/test_openapi.py` (schemathesis) | Fake stores; HTTP layer only |
| Schema changes stay backward compatible | `tests/contract/test_compatibility.py` | Diffs against HEAD, not a release tag |
| Partition assignment, consumer resume | `tests/integration/` on real containers | Same image versions as compose |
| Releasing a partition publishes no termination | `tests/unit/test_rebalance.py` | Deterministic fake; never a real broker rebalance |
| End-to-end pipeline works | `tests/e2e/` under docker compose | Single-node, one scenario |
| Throughput at 500 aircraft | `tests/perf/`, `latency-budget.md` | Compute path only; no transport |
| Observability degrades without its extra | `degradation` CI job, `tests/unit/test_metrics.py` | Proves the fallback runs, not that it is equivalent |
| Deployment artefacts agree with the code | `tests/unit/test_deployment.py` | Static agreement; says nothing about whether the values are right |

No metric appears in the README until its runner is committed and reproducible
from a recorded seed.
