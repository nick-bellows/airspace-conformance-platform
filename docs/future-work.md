# If this were to continue

Everything here is **unbuilt and unscheduled**. It is written down because the
gaps are known — a project that lists only what it did is less trustworthy than
one that says where it stops and why.

The repository is deliberately finished as a portfolio piece. Nothing below is
required to understand what it does or how well it does it; those questions are
answered by [`../README.md`](../README.md),
[`limitations.md`](limitations.md), and the committed evaluation reports.

---

## 1. The one thing worth building next

**Probabilistic conflict detection.**

Precision is 0.57: two in five alerts concern a pair that never actually loses
separation. The cause is thresholding a point estimate — a predicted 4.9 NM miss
alerts, 5.1 NM does not, and the velocity estimate behind that prediction carries
enough noise to move the answer across the line.

The Kalman filter already maintains the covariance needed to ask a better
question: *what is the probability these two violate separation* rather than
*will the predicted miss distance be under 5 NM*. Propagating both covariances to
the closest point of approach and integrating over the protected zone replaces a
threshold with a probability, which is also the right input for a
severity ranking.

**Why this one and not the others.** It attacks the weakest published number in
the repository with a method the system already has the inputs for, and the
result is publishable either way — rerunning the committed evaluation unchanged
and reporting an honest failure to improve 0.57 would be a better artefact than
another feature. Everything else on this page is either scale work nobody can
evaluate from a repository, or decoration.

If it lands, one visualisation earns its place alongside it: **1-sigma
uncertainty ellipses on the display**, because they make the argument visible
rather than theoretical. Not otherwise.

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
- **Startup ordering for the feed.** It has no dependency on Redpanda in the
  Kubernetes manifests and crash-loops until the broker accepts connections. An
  init container mirroring `acp-wait-for-schema` would close it; crash-loop-until-
  ready is a defensible pattern, so this is a tidiness and cold-start-time
  question rather than a correctness one.
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
