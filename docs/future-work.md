# If this were to continue

Everything here is **unbuilt and unscheduled**. It is written down because the
gaps are known — a project that lists only what it did is less trustworthy than
one that says where it stops and why.

The repository is deliberately finished as a portfolio piece. Nothing below is
required to understand what it does or how well it does it; those questions are
answered by [`../README.md`](../README.md),
[`limitations.md`](limitations.md), and the committed evaluation reports.

---

## 1. The one thing worth building next — built, and it did not work

**Probabilistic conflict detection.** This section used to be the plan. It has
been built, measured, and the result is a negative one, so it stays here as a
finding rather than moving to the feature list.

The reasoning was sound: precision is 0.57 because the detector thresholds a
point estimate — a predicted 4.9 NM miss alerts, 5.1 NM does not, and the
velocity estimate carries enough noise to move the answer across that line. The
Kalman filter already maintains the covariance needed to ask *what is the
probability these two violate separation* instead. So it now does, and
[`eval/results/detector_comparison.md`](../eval/results/detector_comparison.md)
scores both detectors on identical traffic across a swept threshold.

**On the nominal family one threshold won.** `p >= 0.5` raised precision from
0.574 to 0.609 at unchanged recall and lead time — +0.036, scenario-clustered
95% CI [+0.008, +0.076], excluding zero.

**On the shifted family it did not replicate:** +0.006, CI [+0.000, +0.021],
spanning zero. The nominal gain was mostly a threshold fitted to the family it
was measured on. Details and the two findings that did survive are in
[ADR 0012](adr/0012-probabilistic-conflict-detection.md).

So the deterministic detector remains the default, the probabilistic one ships
as an option nobody is told to turn on, and **precision 0.57 remains the weakest
number in the repository with its cause now genuinely unknown.** The obvious
explanation was tested and is not sufficient.

**What this changes about what to do next.** Not "tune the threshold" — that is
the move this result argues against. The useful next step is to stop guessing at
the cause and characterise the false alerts directly: cluster the 29 by geometry,
lookahead, and manoeuvre state and find out what they actually have in common.
That is a day of analysis rather than a feature, and it is now better motivated
than any of it was before.

The visualisation this section used to promise — **1-sigma uncertainty ellipses
on the display** — was conditional on the detector landing. It did not land, so
they are not built. Drawing uncertainty that the detector is not permitted to
act on would be decoration arguing for a conclusion the evidence declined.

---

## 2. Engineering that scale would force

Real and unbuilt. Each is a known limitation, not an oversight.

| | What is missing | Why it is not built |
| --- | --- | --- |
| **Sharding the conformance service** | It holds the whole airspace picture, so it runs as exactly one replica and duplicate alerts are the failure mode of raising that. Sector partitioning is the answer | Substantial work whose benefit is invisible below the traffic volumes this project simulates |
| **A dead-letter topic** | Messages failing contract validation are logged, counted, and dropped. A real deployment would route them somewhere inspectable | One-line behaviour change, several days of surrounding machinery |
| **Filter state transfer on rebalance** | A reassigned aircraft re-initiates with a wide covariance and takes seconds to converge ([ADR 0011](adr/0011-partition-ownership-is-state.md)) | Needs an inter-replica protocol and a new consistency problem, to save a few seconds during deployments |
| **At-least-once *across* a rebalance** | Offset commits for a revoked partition raise `CommitFailedError`, and that path is untested | Predates the rebalance work; correctness here needs a real two-consumer integration test |
| **An outbox for track history** | Offsets commit before the batched database flush, so a hard kill loses up to one flush interval of *history*. The live picture is unaffected | An outbox table is real work for a bounded loss of historical rows |

---

## 3. Operations that production would require

- **Alerting.** Prometheus scrapes and Grafana draws; nothing pages. Two rules
  would close it — consumer lag rising for five minutes, and
  `acp_trajectory_model_loaded == 0` for an hour.
- **A rolling upgrade under live traffic.** A new migration, a pod rollout, and a
  Kafka rebalance happening at once while reports keep arriving. Scaling the
  tracker 2 → 3 against a real broker has now been observed working — revoke,
  release, reassign, with the aircraft count unchanged — but not while a
  migration and a rollout are also in flight.
- **Startup ordering for the migration Job.** The Kafka clients got
  `acp-wait-for-kafka` init containers when crash-loop backoff pushed a rollout
  past its timeout in CI. The migration Job still relies on `backoffLimit`
  instead, which works and is what a Job's retry is for, but is inconsistent
  with its neighbours.
- **Supply-chain attestation.** CI builds once and every consumer asserts the
  image ID matches, so the published artefact is provably the tested one. There
  is no signature and no SLSA provenance; `cosign attest` on the published digest
  is the next honest step.
- **Durability, secrets, network policy, TLS.** Ephemeral storage, a committed
  development credential, no NetworkPolicy, no TLS. Every one is flagged in the
  manifests themselves and in [`limitations.md`](limitations.md).
- **Latency measured through the broker.** The published budget covers the
  compute path by design and says so. Nobody has measured Kafka, Postgres, and
  Redis in the loop.

---

## 4. Deliberately declined

Listed so the omissions read as decisions.

| Not built | Why not |
| --- | --- |
| **Real ADS-B ingestion** | The synthetic simulator is the only source *because* it provides ground truth the detector never sees. Real data removes the one genuinely non-circular measurement in the repository ([ADR 0002](adr/0002-synthetic-traffic-only.md)) |
| **Radar-style data association** | Reports carry aircraft identifiers; real surveillance does not. Months of work whose results could not be validated against synthetic data — importing exactly the credibility problem this project avoids |
| **A hosted public instance** | Authentication, TLS, rate limiting and a cost ceiling are a product, not a demonstration |
| **An LLM in the runtime path** | A non-deterministic generative model in a safety-adjacent decision loop is the wrong tool, and saying so is the point ([ADR 0008](adr/0008-no-generative-model-in-the-safety-path.md)) |
| **Cloud deployment** | Compose and Kubernetes manifests demonstrate packaging and orchestration. A cloud bill demonstrates a cloud bill |
| **Conflict *resolution* advisories** | Detection is advisory and says so. Proposing manoeuvres is a different system with a much higher bar |
| **Vertical profile, time scrubbing, embedded dashboards** | Each is real; none teaches a reader anything the existing display does not |
