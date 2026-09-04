# Airspace Conformance Platform

[![quality](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml/badge.svg)](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml)

**[Start the live 60-second engineering tour](https://nick-bellows.github.io/airspace-conformance-platform/#tour)** — replay the system, inspect representative code and tests, and read the measured results and caveats. No install required.

Event-driven monitoring of simulated air traffic. Four services connected by
Kafka turn noisy aircraft position reports into smoothed tracks and advisory
safety alerts: predicted losses of separation, unmodelled maneuvers, and
emergency transponder codes.

![Two aircraft converging head-on at FL350. A predicted-conflict advisory fires
five minutes before closest approach, naming both aircraft and the predicted
separation.](docs/assets/demo.gif)

*Rendered by [`scripts/make_demo.py`](scripts/make_demo.py) from the committed
`head-on-conflict` scenario — the same simulator, Kalman filter, and separation
monitor the services run, not a mock-up. Regenerate it with
`python scripts/make_demo.py`.*

> **A portfolio project, not a product.** It exists to demonstrate how a
> distributed, tested, observable system is built, using air traffic conformance
> monitoring as the problem domain. What it deliberately does *not* do, and why,
> is in [`docs/future-work.md`](docs/future-work.md) — worth reading before
> concluding anything is missing by accident.
>
> **Status: complete, M0–M6.** `docker compose up` from a clean clone produces
> manoeuvring traffic on a live display streamed over a WebSocket, conflict
> alerts, and conformance advisories. Tested at every level it claims to work at
> — unit, contract, integration against real infrastructure, end to end — plus a
> measured latency budget, Prometheus metrics, distributed tracing across Kafka,
> Kubernetes manifests applied to a real `kind` cluster, four-tool security
> scanning, and an image published to GHCR.
>
> **All thirteen CI jobs are green**, which took four attempts — the pipeline had
> never executed until this repository got a remote, and the five defects that
> first run exposed (plus the eighteen three external reviews found before it) are
> in [`how-it-was-built.md`](docs/how-it-was-built.md).
>
> **Built with AI assistance, and that is documented rather than hidden.**
> [`ai-assisted-development.md`](docs/ai-assisted-development.md) records how
> Claude Code was used for implementation, refactoring and test creation, the
> guardrails applied, and what it got wrong — the dominant failure mode was
> confident false prose, not broken code, which is why the definition of done
> forbids any claim in prose that nothing checks.

> **Not an air traffic control system.** Advisory output only, synthetic data
> only, no certification of any kind. Read [`docs/safety-notes.md`](docs/safety-notes.md)
> before drawing any conclusion about what this does.

## Architecture

```mermaid
flowchart LR
    SIM[["sim<br/>seeded flight model"]] --> FEED
    FEED[feed-service<br/>sensor gateway]
    TRACK[track-service<br/>Kalman + lifecycle]
    CONF[conformance-service<br/>predict · separation · rules]
    API[api-service<br/>REST · WebSocket · display]

    FEED -- surveillance.reports.v1 --> TRACK
    TRACK -- tracks.updates.v1 --> CONF
    TRACK -- tracks.updates.v1 --> API
    CONF -- airspace.alerts.v1 --> API
    TRACK --> PG[(Postgres<br/>track history)]
    TRACK --> RD[(Redis<br/>live picture)]
    FEED -. sim.truth.v1<br/>evaluation only .-> EVAL[["eval harness"]]
```

Reports are keyed by aircraft address, so Kafka orders every report for a given
aircraft while processing different aircraft in parallel — the exact guarantee
the tracker needs and no more. `sim.truth.v1` carries noiseless simulator state
and is consumed only by the evaluation harness; no production path reads it.

## Why it is built this way

Each significant choice has a record in [`docs/adr/`](docs/adr/) with the
alternatives and why they lost:

| ADR | Decision |
| --- | --- |
| [0001](docs/adr/0001-event-streaming-over-request-response.md) | Kafka topics between services rather than HTTP calls |
| [0002](docs/adr/0002-synthetic-traffic-only.md) | Synthetic traffic only — including what that costs |
| [0003](docs/adr/0003-shared-library-with-enforced-service-isolation.md) | One repo, one shared library, service isolation enforced by a test |
| [0004](docs/adr/0004-shared-read-model-between-tracker-and-api.md) | A shared storage layer: the tracker writes, the API reads |
| [0005](docs/adr/0005-at-least-once-delivery-and-idempotent-consumers.md) | At-least-once delivery, with idempotency enforced by a database constraint |
| [0006](docs/adr/0006-constant-velocity-filter.md) | A constant-velocity Kalman filter, chosen *for* its weakness |
| [0007](docs/adr/0007-residual-learning-over-direct-prediction.md) | The model learns the correction to physics, not the trajectory |
| [0008](docs/adr/0008-no-generative-model-in-the-safety-path.md) | No generative model in the runtime path, and why |
| [0009](docs/adr/0009-one-reader-fans-out-to-many-viewers.md) | One reader fans out to many viewers, so load tracks the airspace not the audience |
| [0010](docs/adr/0010-observability-degrades-rather-than-blocks.md) | Observability degrades to no-ops rather than blocking startup — and how a trace crosses a broker |
| [0011](docs/adr/0011-partition-ownership-is-state.md) | Partition ownership is state: a consumer that loses a partition releases its aircraft without terminating them |
| [0012](docs/adr/0012-probabilistic-conflict-detection.md) | Probabilistic conflict detection, built alongside the deterministic one — and the measurement that says it does not win |
| [0013](docs/adr/0013-lookahead-is-an-operating-point.md) | The lookahead is an operating point, so the precision curve gets published rather than one point on it |
| [0014](docs/adr/0014-conflict-is-an-interval-not-an-instant.md) | A conflict is both standards breached at the same *moment*, which is an interval-overlap test rather than a check at closest approach |

## Results

Both evaluations are reproducible from a recorded seed and stamped with every
component version.

### Conflict detection

Scored against simulator ground truth the detector never sees, while consuming
only the noisy observation stream.
[Full report](eval/results/conflict_detection.md).

| Metric | Value |
| --- | --- |
| Scenarios / simulated airspace time | 123 / 20.75 hours |
| Real losses of separation | 39 |
| Recall (alerted before violation) | 1.00 |
| Precision | 0.56 |
| False alerts per airspace hour | 1.49 |
| Median warning lead time | 249 s |

**Precision 0.56 is an operating point, not a defect** — and quoting it without
the lookahead it was measured at was the actual reporting error. The median
"false" alert is a pair that genuinely closed to 5.52 NM against a 5 NM standard,
raised at 296 s against a 300 s ceiling. They are not errors; they are the cost
of extrapolating constant velocity for five minutes.

| Lookahead | Precision (nominal) | Precision (shifted) | Median lead |
| --- | --- | --- | --- |
| 300 s ←default | 0.56 | 0.40 | 249 s |
| 180 s | 0.72 | 0.55 | 180 s |
| 120 s | 0.87 | 0.65 | 120 s |

The default stands: lead time is the product, and on shifted traffic the shorter
window also costs a real detection.
[`lookahead_tradeoff.md`](eval/results/lookahead_tradeoff.md) has the curve.

**Two things this README used to claim were tested and found wrong.** That
precision was caused by thresholding a point estimate — the principled fix was
built and did not replicate across scenario families
([ADR 0012](docs/adr/0012-probabilistic-conflict-detection.md)). And that recall
1.00 was flattered by constant-velocity traffic — sweeping manoeuvre density,
recall is *lowest* when nothing manoeuvres at all
([report](eval/results/manoeuvre_sensitivity.md)). Both corrections point the
same way: **precision tracks accumulated constant-velocity error; recall does
not**, because the detector re-evaluates every second and only has to be right
once.

### Conformance monitoring

The project's namesake, and until now the only detector in it with no published
numbers. Scored against the simulator's flight plan — turns, climbs and speed
changes at known times that no part of the pipeline observes.
[Full report](eval/results/conformance_detection.md).

| Manoeuvre | Count (shifted family) | Recall |
| --- | --- | --- |
| Turns | 726 | 0.42 |
| Speed changes | 338 | 0.02 |
| Climbs | 568 | **0.01** |
| **Overall** | **1,632** | **0.20** |

**It is a turn detector.** The monitor thresholds the *horizontal* distance
between where an aircraft was predicted to be and where it is — and a climb
barely moves an aircraft horizontally, so it is blind to vertical manoeuvres by
construction. Nothing in the system compares predicted altitude against observed
altitude.

Precision is **1.00** across both families: every advisory it raised
corresponded to a real manoeuvre. That is not the compliment it sounds like at
recall 0.20 — it fires only on manoeuvres too large to miss. Median lag to
notice a turn is 49.6 s, bounded by the 60 s prediction horizon.

### Trajectory prediction

A PyTorch model predicting the **residual** of a dead-reckoning prediction, from
filtered track updates only. [Full report](eval/results/trajectory_prediction.md),
[model card](docs/cards/model-trajectory-predictor.md).

Horizontal error while **turning** — the only regime where horizontal prediction
is hard — at a 60 s horizon:

| Split | Dead reckoning | Neural | Skill | Samples |
| --- | --- | --- | --- | --- |
| Unseen scenarios, same family | 2.412 NM | **1.220 NM** | **+49.4%** | 4,495 |
| Shifted family (different airspace) | 2.024 NM | **1.261 NM** | **+37.7%** | 3,830 |

The gap between those rows is the cost of distribution shift. **In cruise and
climb the model adds nothing**, because a straight line is already accurate to
tens of metres — and at a 30 s horizon it makes *altitude* prediction worse than
the baseline (31 ft → 98 ft), which the model card says outright.

**Confidence intervals complicate the verdict, and that is the most interesting
result here.** A scenario-clustered bootstrap (400 resamples over 30 held-out
scenarios — consecutive samples from one flight are near-duplicates, so the
effective n is flights, not samples) shows the neural network's advantage over
plain ridge regression is **not distinguishable from zero on shifted traffic at
60 s (−12.7% to +5.5%) or 120 s (−1.0% to +13.4%)**. Both models still beat dead
reckoning decisively. What is uncertain is whether the extra capacity earns its
place away from the training distribution — and on this evidence, shipping the
linear model would be defensible.

All caveats are in [`docs/limitations.md`](docs/limitations.md). The short
version: simulated aircraft hold heading and speed exactly, there is no wind, and
real prediction error is substantially larger than anything reported here.

### Throughput

At **500 aircraft** reporting at 1 Hz, one full cycle — every report through the
Kalman filter plus one conflict scan of the whole picture — takes a **median of
204 ms and a p95 of 251 ms** against a 500 ms budget and a 1000 ms report
interval. Roughly four times the headroom needed to keep up with real time.
[`docs/latency-budget.md`](docs/latency-budget.md) states what is measured
(the compute path) and what is not (Kafka, Postgres, Redis — their latency
belongs to the deployment).

## Testing

| Suite | What only it can catch | Where |
| --- | --- | --- |
| unit | Logic, invariants, architecture rules, and drift between the code and the deployment artefacts | fast gate |
| contract | Spec drift, breaking schema changes, the API disobeying its own spec | fast gate |
| degradation | Whether the documented fallbacks are real: installs **without** `ml` and **without** `observability`, then exercises both paths | CI |
| integration | Partition assignment, consumer resume, idempotency against the real constraint | CI, Docker |
| e2e | A broken compose file, a wrong env var, a service that cannot reach another | CI, Docker |
| k8s | Whether a pod actually starts under the security context it declares | CI, `kind` |
| perf | Whether it keeps up at realistic load | CI, informational |

The fast gate has **no coverage exclusions** and reports 82% over 720 tests. The
two pure I/O adapters are measured separately by the integration job against
their own floor, which is what turned an earlier promise ("these will be covered
later") into something enforced.

## Repository map

```
src/acp/common/      wire contracts, geodesy, config, logging, Kafka client
src/acp/storage/     Postgres schema and access, Redis live picture and alerts
src/acp/sim/         kinematic flight model, scenarios, scenario generator
src/acp/services/    feed, track, conformance, api - one process each
src/acp/ml/          baselines, features, dataset, models, training, serving
models/              trained residual models, committed (17 KB each)
scenarios/           committed, seeded airspace situations
contracts/           committed JSON Schemas, drift-gated in CI
migrations/          Alembic
deploy/compose.yml   development stack plus all four services
deploy/k8s/          Kubernetes manifests, applied to a kind cluster in CI
deploy/observability/ Prometheus scrape config, Grafana provisioning, dashboard JSON
tests/               unit · contract · integration · e2e · perf
eval/                evaluation runners and committed, seed-stamped results
docs/                ADRs, cards, operations, limitations, future work
```

## Documentation

| Document | What it is for |
| --- | --- |
| [`docs/operations.md`](docs/operations.md) | Running, configuring, scaling, inspecting, troubleshooting |
| [`docs/safety-notes.md`](docs/safety-notes.md) | What this system is not. Read first. |
| [`docs/limitations.md`](docs/limitations.md) | What every published number does and does not support |
| [`docs/adr/`](docs/adr/) | Why each significant choice was made, and what lost |
| [`docs/cards/`](docs/cards/) | Model and data cards |
| [`docs/how-it-was-built.md`](docs/how-it-was-built.md) | The build in one page: the decisions, and the defects each level of testing found |
| [`docs/walkthroughs/`](docs/walkthroughs/) | Per-milestone detail, including what each one got wrong |
| [`docs/agile.md`](docs/agile.md) | Objectives, acceptance criteria, definition of done, ROAM risk log |
| [`docs/interview-brief.md`](docs/interview-brief.md) | Talking points. Not part of the system |
| [`docs/future-work.md`](docs/future-work.md) | What would come next, what scale would force, and what is declined |
| [`docs/ai-assisted-development.md`](docs/ai-assisted-development.md) | How AI was used to build this, and what it got wrong |
| [`docs/latency-budget.md`](docs/latency-budget.md) | What "keeps up" means here, and what the measurement excludes |

**Where to start, by how long you have.** Five minutes: this page, then
[`safety-notes.md`](docs/safety-notes.md). An hour: add
[`limitations.md`](docs/limitations.md) and two ADRs —
[0006](docs/adr/0006-constant-velocity-filter.md), where a filter is chosen *for*
its weakness, and [0011](docs/adr/0011-partition-ownership-is-state.md), which is
the most interesting defect in the repository. Longer:
[`how-it-was-built.md`](docs/how-it-was-built.md) and
[`future-work.md`](docs/future-work.md).

## Quick start

The published image needs no clone and no build:

```bash
docker pull ghcr.io/nick-bellows/airspace-conformance-platform:latest
docker run --rm ghcr.io/nick-bellows/airspace-conformance-platform:latest acp-feed --help
```

It runs under the same restrictive context the Kubernetes manifests declare —
`--read-only --user 1001 --cap-drop ALL` — which is a claim worth checking
rather than believing.

Run the whole thing (Docker required). Linux, macOS, or WSL:

```bash
./scripts/demo.sh                                 # head-on conflict
./scripts/demo.sh --scenario unannounced-turn     # conformance monitoring
./scripts/demo.sh --scenario quiet-cruise         # nothing should ever alert
./scripts/demo.sh --tools                         # plus a Kafka console on :8080
./scripts/demo.sh --down                          # tear down, including volumes
```

Windows PowerShell:

```powershell
.\scripts\demo.ps1                                # head-on conflict
.\scripts\demo.ps1 -Scenario unannounced-turn     # conformance monitoring
.\scripts\demo.ps1 -Scenario quiet-cruise         # nothing should ever alert
.\scripts\demo.ps1 -Tools                         # plus a Kafka console on :8080
.\scripts\demo.ps1 -Down                          # tear down, including volumes
```

Then open <http://localhost:8000>.

| Scenario | What to watch for | When |
| --- | --- | --- |
| `head-on-conflict` | Two aircraft ring red; a conflict advisory names them and counts down to closest approach. A third crosses the same point at the same moment — 0.2 NM laterally — but 4,000 ft above, and is correctly ignored. | ~2½ min |
| `unannounced-turn` | ACP501 turns 80° with nothing to forecast it; a conformance advisory appears once the earlier prediction matures, then clears. | turn at 4 min, alert ~4½ min |
| `quiet-cruise` | Six well-separated aircraft and no alerts at all. The false-alarm control. | never |

Develop and run the checks — the same five gates CI runs first:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[all]"
./scripts/run_checks.sh
```

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
.\scripts\run_checks.ps1
```

See metrics, dashboards, and traces:

```bash
ACP_OTLP_ENDPOINT=http://jaeger:4318/v1/traces \
  docker compose -f deploy/compose.yml --profile observability up -d
```

Grafana on :3000 (dashboard provisioned, anonymous read-only access),
Prometheus on :9090, Jaeger on :16686 — pick service `acp-feed` there to watch
one surveillance report travel through three processes in a single trace.

Full operator and developer guide: [`docs/operations.md`](docs/operations.md).

## What runs where

| Service | Consumes | Produces | Notes |
| --- | --- | --- | --- |
| `feed` | a scenario file | `surveillance.reports.v1`, `sim.truth.v1` | Owns the simulation clock. The only component that touches truth. |
| `track` | `surveillance.reports.v1` | `tracks.updates.v1`, Postgres, Redis | Kalman filter, track lifecycle. Publishes the innovation as a manoeuvre signal. |
| `conformance` | `tracks.updates.v1` | `airspace.alerts.v1`, Redis | Predicted losses of separation, trajectory conformance, and single-aircraft rules. |
| `api` | Redis, Postgres | REST + the display | Never consumes Kafka and never writes. |

## Reproducing the evaluations

```powershell
python eval/run_conflict_eval.py --scenarios 120 --seed 20260815 --include-committed   # ~4 min
python -m acp.ml.train --scenarios 120 --seed 20260815                                  # ~7 min
```

Both are deterministic and both write a committed report stamped with every
component version plus a hash of the scenario set, so a stale result is
identifiable rather than merely suspicious. A test recomputes that hash on every
run: a change to scenario generation that would invalidate the committed
conflict report fails the build rather than going unnoticed. It has already
caught one — a reordered random draw during a refactor.

## Quality gate

`scripts/run_checks.ps1` is the fast local gate, and the `quality` workflow runs
the same five checks before adding everything that needs Docker or a network:

| Check | Purpose |
| --- | --- |
| `ruff check` | Lint, import order, and flake8-bandit security rules |
| `ruff format --check` | Formatting |
| `mypy --strict` | Full static typing, no implicit `Any` |
| `pytest` + coverage | Unit and contract tests, 80% floor |
| `scripts/contracts.py --check` | Fails if a wire model changed without its committed schema being regenerated |

Thirteen jobs in all. Beyond `lint`, `types`, `unit`, and `contracts` — the four
that mirror the local gate — CI adds `degradation`, `integration`, `e2e`, `perf`,
`image`, `compose`, `security`, `k8s`, and `publish`.

The security job asks four separate questions: bandit for the code, pip-audit
for the dependencies that actually ship, gitleaks over the full git history, and
Trivy plus a syft SBOM for the image. `publish` pushes to GHCR on `main`, gated
on every job except the informational `perf`, because pushing an image that
failed its own tests is the one ordering that matters. Four other jobs depend on
`image` so they all scan and run the *same* artefact rather than rebuilding it.
Full breakdown in [`docs/operations.md`](docs/operations.md) §11.

Three of the current tests are worth singling out:

- **`test_architecture.py`** reads the import graph out of the source and fails
  if one service imports another. It caught the API service importing the
  tracker's storage module during M1; the fix was to lift storage into its own
  layer ([ADR 0004](docs/adr/0004-shared-read-model-between-tracker-and-api.md)).
- **`test_geodesy.py`** asserts invariants with Hypothesis rather than examples,
  and found two real defects on its first run: `normalize_bearing` returning
  exactly `360.0` for tiny negative inputs, and the local projection reporting
  ~21,000 NM instead of ~12 NM across the antimeridian — which would have hidden
  a conflict entirely.
- **`test_deployment.py`** treats the compose file, Kubernetes manifests,
  Prometheus scrape config and Grafana dashboard as code that nothing
  type-checks and nothing imports, because that is what they are. These drift
  silently: a renamed metric leaves a panel reading "No data", which looks
  exactly like a healthy idle system.

## Milestones

- [x] **M0 — Scaffold.** Contracts, geodesy, config, logging, quality gate, dev stack, ADRs 0001–0003.
- [x] **M1 — Walking skeleton.** Traffic flowing feed → Kafka → tracker → Postgres/Redis → API, with a live display; Alembic migrations; ADR 0004.
- [x] **M2 — Detection.** Manoeuvring flight model, Kalman filter, separation monitor, alert lifecycle, scenario generator, and the conflict-detection evaluation report; ADRs 0005–0006.
- [x] **M3 — Trajectory prediction.** Physics baselines, ridge and PyTorch residual models, stratified distribution-shift evaluation, conformance monitoring, model and data cards, operations manual; ADRs 0007–0008.
- [x] **M4 — Real-time surface and full test pyramid.** WebSocket streaming, contract/compatibility/conformance tests, integration on real containers, end-to-end under compose, and a measured latency budget; ADR 0009.
- [x] **M5 — DevSecOps and operations.** Prometheus metrics on all four services, W3C trace context across Kafka into Jaeger, a provisioned Grafana dashboard, Kubernetes manifests, four-tool security scanning with an SBOM, and image publishing to GHCR; ADRs 0010–0011.
- [x] **M6 — Make it legible.** A demo generated from the real simulator, one page of planning and delivery material, an interview brief, and a 25% cut to the documentation.

No metric appears in this README until its evaluation runner is committed and
reproducible from a recorded seed, and since M5 the same standard applies to
claims about CI.

## What comes next, and what deliberately does not

Every milestone is done and the pipeline is green, so the project is finished as
a portfolio piece. [`docs/future-work.md`](docs/future-work.md) is the fastest
way to see the boundaries: the one technical improvement worth making, the
engineering that scale would force, and the things declined on purpose.

## Licence

MIT. See [`docs/safety-notes.md`](docs/safety-notes.md) for scope and disclaimers.
