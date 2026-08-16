# Airspace Conformance Platform

[![quality](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml/badge.svg)](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml)

Event-driven monitoring of simulated air traffic. Four services connected by
Kafka turn noisy aircraft position reports into smoothed tracks and advisory
safety alerts: predicted losses of separation, unmodelled maneuvers, and
emergency transponder codes.

> **Status: M5 — DevSecOps and operations.** All four services run. `docker
> compose up` from a clean clone produces manoeuvring traffic on a live display
> streamed over a WebSocket, conflict alerts, and conformance advisories. Tested
> at every level it claims to work at: contract, integration against real
> infrastructure, end to end, and a latency budget. Now also instrumented
> (Prometheus metrics, a provisioned Grafana dashboard, and traces that follow
> one report across three services), scanned (SAST, dependency CVEs, secrets,
> image, SBOM), and deployed to a real Kubernetes cluster in CI. Milestones below
> are checked only when their evidence is committed.

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
| Precision | 0.57 |
| False alerts per airspace hour | 1.40 |
| Median warning lead time | 249 s |

**Precision of 0.57 is the honest result and it is not good enough.** Two in five
alerts concern a pair that never actually loses separation, because the detector
thresholds a point estimate: a predicted 4.9 NM miss alerts, 5.1 NM does not, and
the velocity estimate behind that prediction carries enough noise to move the
answer across the line. The principled fix is probabilistic detection using the
covariance the filter already maintains. **Recall of 1.00 is also weaker than it
looks** — the generated encounters are mostly constant-velocity approaches, which
is exactly the assumption the detector makes.

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
204 ms and a p95 of 251 ms** against a 500 ms budget and a 1000 s report
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

The fast gate has **no coverage exclusions** and reports 81% over 512 tests. The
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
docs/                ADRs, cards, walkthroughs, operations, limitations
```

## Documentation

| Document | What it is for |
| --- | --- |
| [`docs/operations.md`](docs/operations.md) | Running, configuring, scaling, inspecting, troubleshooting |
| [`docs/safety-notes.md`](docs/safety-notes.md) | What this system is not. Read first. |
| [`docs/limitations.md`](docs/limitations.md) | What every published number does and does not support |
| [`docs/adr/`](docs/adr/) | Why each significant choice was made, and what lost |
| [`docs/cards/`](docs/cards/) | Model and data cards |
| [`docs/walkthroughs/`](docs/walkthroughs/) | Per-milestone explanation in plain English |
| [`docs/ai-assisted-development.md`](docs/ai-assisted-development.md) | How AI was used to build this, and what it got wrong |

## Quick start

Run the whole thing (Docker required):

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
| `head-on-conflict` | Two aircraft ring red; a conflict advisory names them and counts down to closest approach. A third aircraft is laterally close but 4,000 ft above and is correctly ignored. | ~2½ min |
| `unannounced-turn` | ACP501 turns 80° with nothing to forecast it; a conformance advisory appears once the earlier prediction matures, then clears. | turn at 4 min, alert ~4½ min |
| `quiet-cruise` | Six well-separated aircraft and no alerts at all. The false-alarm control. | never |

Develop and run the checks:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
.\scripts\run_checks.ps1
```

See metrics, dashboards, and traces:

```powershell
$env:ACP_OTLP_ENDPOINT = "http://jaeger:4318/v1/traces"
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

Twelve jobs in all. Beyond `lint`, `types`, `unit`, and `contracts` — the four
that mirror the local gate — CI adds `degradation`, `integration`, `e2e`,
`perf`, `compose`, `security`, `k8s`, and `publish`.

The security job asks four separate questions: bandit for the code, pip-audit
for the dependencies that actually ship, gitleaks over the full git history, and
Trivy plus a syft SBOM for the image. `publish` pushes to GHCR on `main`, and it
is the only job with `needs:`, because pushing an image that failed its own
tests is the one ordering that matters. Full breakdown in
[`docs/operations.md`](docs/operations.md) §11.

Three of the current tests are worth singling out:

- **`tests/unit/test_architecture.py`** reads the import graph out of the source
  and fails the build if one service imports another. It has already caught a
  real violation: during M1 the API service was written to import the tracker's
  storage module, and the build refused it. The fix was to lift storage into its
  own layer ([ADR 0004](docs/adr/0004-shared-read-model-between-tracker-and-api.md)).
- **`tests/unit/test_geodesy.py`** asserts invariants with Hypothesis rather than
  examples. It found two real defects on its first run: `normalize_bearing`
  returning exactly `360.0` for tiny negative inputs (which would have failed
  contract validation at runtime), and the local projection reporting ~21,000 NM
  instead of ~12 NM for a pair straddling the antimeridian (which would have
  hidden a conflict entirely).
- **`tests/unit/test_deployment.py`** treats the compose file, the Kubernetes
  manifests, the Prometheus scrape config, and the Grafana dashboard as code
  that nothing type-checks and nothing imports — because that is what they are.
  It fails if the dashboard queries a metric the code no longer creates, if a
  scrape annotation names a port nothing listens on, or if a published port
  stops binding to loopback. Those drift silently: a renamed metric leaves a
  panel reading "No data", which looks exactly like a healthy idle system.

## Milestones

- [x] **M0 — Scaffold.** Contracts, geodesy, config, logging, quality gate, dev stack, ADRs 0001–0003.
- [x] **M1 — Walking skeleton.** Traffic flowing feed → Kafka → tracker → Postgres/Redis → API, with a live display; Alembic migrations; ADR 0004.
- [x] **M2 — Detection.** Manoeuvring flight model, Kalman filter, separation monitor, alert lifecycle, scenario generator, and the conflict-detection evaluation report; ADRs 0005–0006.
- [x] **M3 — Trajectory prediction.** Physics baselines, ridge and PyTorch residual models, stratified distribution-shift evaluation, conformance monitoring, model and data cards, operations manual; ADRs 0007–0008.
- [x] **M4 — Real-time surface and full test pyramid.** WebSocket streaming, contract/compatibility/conformance tests, integration on real containers, end-to-end under compose, and a measured latency budget; ADR 0009.
- [x] **M5 — DevSecOps and operations.** Prometheus metrics on all four services, W3C trace context across Kafka into Jaeger, a provisioned Grafana dashboard, Kubernetes manifests applied to a `kind` cluster in CI, four-tool security scanning with an SBOM, and image publishing to GHCR; ADR 0010.
- [ ] **M6 — Narrative.** Agile artefacts (PI plan, stories with acceptance criteria, ROAM risk log), interview brief, demo recording.

No metric appears in this README until its evaluation runner is committed and
reproducible from a recorded seed.

## Roadmap

Everything below is unbuilt. It is listed because the gaps are known, not
because they are scheduled — and because a roadmap that quietly omits the known
weaknesses is worse than no roadmap.

### Making it viewable without Docker

The single biggest barrier to anyone looking at this: today it requires cloning
the repository, having Docker, and waiting several minutes for a first build.
Most people who might want to look at it will not do that.

- [ ] **A recorded demo in the README.** An animated capture of the head-on
      conflict developing and the advisory firing, so the system is legible in
      five seconds from the repository page alone. Cheapest possible fix and the
      first one to do.
- [ ] **A static replay page on GitHub Pages.** A pre-recorded scenario shipped
      as a JSON frame log and replayed by the existing display code with no
      backend at all. The display already renders from frames, so this is
      largely a matter of serialising a run and pointing the page at the file.
      Honest about what it is: a replay, not a live system, and labelled as such
      on the page rather than in a footnote.
- [ ] **A hosted live instance.** Genuinely useful and genuinely the most
      expensive: a public deployment needs authentication, rate limiting, TLS,
      and a cost ceiling, none of which exist. Listed so the omission is a
      decision rather than an oversight.

### Advanced visualisation

The current display is a deliberately plain dark-canvas plan view — vanilla JS,
no build step, no CDN, no map tiles. That was the right call for a demo that has
to run offline from a clean clone, and it leaves several things unshown that the
system already computes:

- [ ] **Uncertainty on the display.** The Kalman filter maintains a full
      covariance and nothing draws it. A 1-sigma ellipse around each track would
      make the difference between a confident track and a coasting one visible,
      and would make the case for probabilistic conflict detection obvious
      rather than theoretical.
- [ ] **Predicted trajectory ribbons.** Draw the +30/+60/+120 s forecasts, with
      the model's and dead reckoning's side by side. This is the clearest
      possible answer to "what does the ML actually do", and it needs no new
      computation — the conformance service already produces both.
- [ ] **Conflict geometry, not just a red ring.** Show the closest-point-of-
      approach construction: where each aircraft will be, the predicted
      separation, and the countdown. The alert already carries every one of
      those numbers.
- [ ] **A vertical profile view.** Conflicts are three-dimensional and a plan
      view hides the dimension that resolves most of them. The `quiet-cruise`
      scenario's laterally-close-but-4,000-ft-apart pair is invisible as a
      *decision* today; a side view would show why it is correctly ignored.
- [ ] **Time scrubbing over history.** Track history is already in Postgres and
      already exposed by `/v1/tracks/{id}/history`. Replaying the last ten
      minutes would turn the display from a monitor into an investigation tool.
- [ ] **An embedded Grafana panel.** The dashboard exists and is provisioned;
      surfacing pipeline health next to the airspace picture would connect the
      two halves of the system for a viewer who only opens one page.

### Known technical gaps

- [ ] **Probabilistic conflict detection.** Precision is 0.57 because the
      detector thresholds a point estimate. Using the covariance the filter
      already maintains would replace "will they be within 5 NM" with "what is
      the probability", which is the principled fix and the highest-value
      unbuilt item in the repository.
- [ ] **A dead-letter topic.** A message that fails validation is logged,
      counted, and skipped. A real deployment would route it somewhere it can be
      inspected rather than discarding it.
- [ ] **Sharding the conformance service.** It holds the whole airspace picture,
      so it cannot run more than one replica without publishing duplicate
      alerts. Sector-based partitioning is the answer and is not written.
- [ ] **Radar-style data association.** Reports carry aircraft identifiers.
      Real surveillance does not, and building tracks without them is a
      substantial problem this project sidesteps entirely.
- [ ] **Secrets from a secret store.** The development Postgres password is
      committed so a clean checkout works. That is wrong for anything real and
      is flagged in the manifests themselves.

## Licence

MIT. See [`docs/safety-notes.md`](docs/safety-notes.md) for scope and disclaimers.
