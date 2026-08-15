# Airspace Conformance Platform

[![quality](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml/badge.svg)](https://github.com/nick-bellows/airspace-conformance-platform/actions/workflows/quality.yml)

Event-driven monitoring of simulated air traffic. Four services connected by
Kafka turn noisy aircraft position reports into smoothed tracks and advisory
safety alerts: predicted losses of separation, unmodelled maneuvers, and
emergency transponder codes.

> **Status: M0 — scaffold.** Contracts, geometry, configuration, logging, the
> quality gate, and the development stack are in place and tested. No service
> runs yet; the first end-to-end slice is M1. Milestones below are checked only
> when their evidence is committed.

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

## Repository map

```
src/acp/common/      wire contracts, geodesy, config, structured logging
src/acp/sim/         kinematic flight model and scenario generation
src/acp/services/    feed, track, conformance, api - one container each
src/acp/ml/          trajectory prediction: baselines, features, model
contracts/           committed JSON Schemas, drift-gated in CI
deploy/compose.yml   development stack: Redpanda, Postgres, Redis
deploy/k8s/          Kubernetes manifests
tests/               unit · contract · integration · e2e · perf
eval/                evaluation runners and committed, seed-stamped results
docs/                ADRs, agile artefacts, walkthroughs, limitations
```

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\scripts\run_checks.ps1
```

Bring up the development infrastructure (Docker required):

```powershell
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml ps
```

Add `--profile tools` for a Redpanda console at <http://localhost:8080> to watch
topics during a demo.

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
  and fails the build if one service imports another. The isolation claim is
  executable, not aspirational.
- **`tests/unit/test_geodesy.py`** asserts invariants with Hypothesis rather than
  examples. It found two real defects on its first run: `normalize_bearing`
  returning exactly `360.0` for tiny negative inputs (which would have failed
  contract validation at runtime), and the local projection reporting ~21,000 NM
  instead of ~12 NM for a pair straddling the antimeridian (which would have
  hidden a conflict entirely).

## Milestones

- [x] **M0 — Scaffold.** Contracts, geodesy, config, logging, quality gate, dev stack, ADRs 0001–0003.
- [ ] **M1 — Walking skeleton.** Straight-line traffic flowing feed → Kafka → tracker → Postgres → API, with a live display.
- [ ] **M2 — Domain depth.** Full flight model, Kalman filter, separation monitor, alert state machine, and the conflict-detection evaluation report.
- [ ] **M3 — Trajectory prediction.** Physics baselines, ridge and PyTorch residual models, distribution-shift evaluation, model card.
- [ ] **M4 — Real-time surface and full test pyramid.** WebSocket streaming, integration and e2e tests, latency budget.
- [ ] **M5 — DevSecOps and operations.** Security scanning, SBOM, image publishing, tracing and metrics, Kubernetes with a `kind` smoke test in CI.
- [ ] **M6 — Narrative.** Limitations, agile artefacts, interview brief, demo recording.

No metric appears in this README until its evaluation runner is committed and
reproducible from a recorded seed.

## Licence

MIT. See [`docs/safety-notes.md`](docs/safety-notes.md) for scope and disclaimers.
