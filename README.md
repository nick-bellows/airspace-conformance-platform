# Airspace Conformance Platform

[![quality](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml/badge.svg)](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml)

Event-driven monitoring of simulated air traffic. Four services connected by
Kafka turn noisy aircraft position reports into smoothed tracks and advisory
safety alerts: predicted losses of separation, unmodelled maneuvers, and
emergency transponder codes.

> **Status: M2 — detection.** All four services run. `docker compose up` from a
> clean clone produces manoeuvring traffic on a live display, a Kalman filter
> smoothing it, and advisory alerts when two aircraft are predicted to lose
> separation. Trajectory prediction (M3) is not built yet. Milestones below are
> checked only when their evidence is committed.

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

## Results

The conflict detector is scored against simulator ground truth it never sees,
while consuming only the noisy observation stream. Full report, including what
these numbers do not support:
[`eval/results/conflict_detection.md`](eval/results/conflict_detection.md).

| Metric | Value |
| --- | --- |
| Scenarios / simulated airspace time | 122 / 20.5 hours |
| Real losses of separation | 39 |
| Recall (alerted before violation) | 1.00 |
| Precision | 0.57 |
| False alerts per airspace hour | 1.41 |
| Median warning lead time | 249 s |

**Precision of 0.57 is the honest result and it is not good enough.** Two in five
alerts concern a pair that never actually loses separation, because the detector
thresholds a point estimate: a predicted 4.9 NM miss alerts, 5.1 NM does not, and
the velocity estimate behind that prediction carries enough noise to move the
answer across the line. The principled fix is probabilistic detection using the
covariance the filter already maintains. **Recall of 1.00 is also weaker than it
looks** — the generated encounters are mostly constant-velocity approaches, which
is exactly the assumption the detector makes. Both caveats are in the report and
in [`docs/limitations.md`](docs/limitations.md).

## Repository map

```
src/acp/common/      wire contracts, geodesy, config, logging, Kafka client
src/acp/storage/     Postgres schema and access, Redis live picture
src/acp/sim/         kinematic flight model and scenario generation
src/acp/services/    feed, track, conformance, api - one process each
src/acp/ml/          trajectory prediction: baselines, features, model
scenarios/           committed, seeded airspace situations
contracts/           committed JSON Schemas, drift-gated in CI
migrations/          Alembic
deploy/compose.yml   development stack plus all four services
deploy/k8s/          Kubernetes manifests
tests/               unit · contract · integration · e2e · perf
eval/                evaluation runners and committed, seed-stamped results
docs/                ADRs, agile artefacts, walkthroughs, limitations
```

## Quick start

Run the whole thing (Docker required):

```powershell
.\scripts\demo.ps1                          # head-on conflict scenario
.\scripts\demo.ps1 -Scenario quiet-cruise   # the false-alarm control
.\scripts\demo.ps1 -Tools                   # plus a Kafka console on :8080
.\scripts\demo.ps1 -Down                    # tear down, including volumes
```

Then open <http://localhost:8000>. Four aircraft appear within a few seconds;
two of them converge head-on at FL350 and pass inside the 5 NM standard about
eight minutes in. Detecting that is M2 — right now the display just shows it
happening.

Develop and run the checks:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,messaging,storage,api]"
.\scripts\run_checks.ps1
```

## What runs where

| Service | Consumes | Produces | Notes |
| --- | --- | --- | --- |
| `feed` | a scenario file | `surveillance.reports.v1`, `sim.truth.v1` | Owns the simulation clock. The only component that touches truth. |
| `track` | `surveillance.reports.v1` | `tracks.updates.v1`, Postgres, Redis | Kalman filter, track lifecycle. Publishes the innovation as a manoeuvre signal. |
| `conformance` | `tracks.updates.v1` | `airspace.alerts.v1`, Redis | Scans the airspace picture on a timer for predicted losses of separation, plus single-aircraft rules. |
| `api` | Redis, Postgres | REST + the display | Never consumes Kafka and never writes. |

## Reproducing the evaluation

```powershell
python eval/run_conflict_eval.py --scenarios 120 --seed 20260815 --include-committed
```

Roughly four minutes. Writes `eval/results/conflict_detection.{md,json}`,
stamped with the simulator, generator, filter, and detector versions plus a hash
of the scenario set, so a stale result is identifiable rather than merely
suspicious.

## Quality gate

`scripts/run_checks.ps1` and the `quality` workflow run the same five checks:

| Check | Purpose |
| --- | --- |
| `ruff check` | Lint, import order, and flake8-bandit security rules |
| `ruff format --check` | Formatting |
| `mypy --strict` | Full static typing, no implicit `Any` |
| `pytest` + coverage | Unit and contract tests, 80% floor |
| `scripts/contracts.py --check` | Fails if a wire model changed without its committed schema being regenerated |

Two of the current tests are worth singling out:

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

**Coverage is measured with two files excluded**, both pure I/O adapters
(`common/messaging.py`, `storage/stores.py`). Every branch in them is a call
into Kafka, Postgres, or Redis, where a unit test could only assert that a mock
behaved as configured. They are covered by the integration suite at M5, and the
exclusion is written down in `pyproject.toml` with instructions to delete it.

## Milestones

- [x] **M0 — Scaffold.** Contracts, geodesy, config, logging, quality gate, dev stack, ADRs 0001–0003.
- [x] **M1 — Walking skeleton.** Traffic flowing feed → Kafka → tracker → Postgres/Redis → API, with a live display; Alembic migrations; ADR 0004.
- [x] **M2 — Detection.** Manoeuvring flight model, Kalman filter, separation monitor, alert lifecycle, scenario generator, and the conflict-detection evaluation report; ADRs 0005–0006.
- [ ] **M3 — Trajectory prediction.** Physics baselines, ridge and PyTorch residual models, distribution-shift evaluation, model card.
- [ ] **M4 — Real-time surface and full test pyramid.** WebSocket streaming, integration and e2e tests, latency budget.
- [ ] **M5 — DevSecOps and operations.** Security scanning, SBOM, image publishing, tracing and metrics, Kubernetes with a `kind` smoke test in CI.
- [ ] **M6 — Narrative.** Limitations, agile artefacts, interview brief, demo recording.

No metric appears in this README until its evaluation runner is committed and
reproducible from a recorded seed.

## Licence

MIT. See [`docs/safety-notes.md`](docs/safety-notes.md) for scope and disclaimers.
