# Operations manual

How to run, drive, inspect, and troubleshoot the platform. For *why* it is built
this way, read the [ADRs](adr/). For what it is not, read
[`safety-notes.md`](safety-notes.md) first.

---

## 1. Prerequisites

| | |
| --- | --- |
| Docker | Desktop or Engine, with Compose v2. About 4 GB free. |
| Python | 3.12 or 3.13, for development and for running evaluations. |
| Disk | ~3 GB for images (PyTorch CPU is most of it). |

Nothing else. No cloud account, no API keys, no data download — all traffic is
generated locally.

---

## 2. Running the stack

```powershell
.\scripts\demo.ps1                              # head-on conflict (default)
.\scripts\demo.ps1 -Scenario unannounced-turn   # exercises conformance monitoring
.\scripts\demo.ps1 -Scenario quiet-cruise       # nothing should ever alert
.\scripts\demo.ps1 -Tools                       # plus a Kafka console on :8080
.\scripts\demo.ps1 -Down                        # tear down, including volumes
```

Or directly:

```powershell
$env:ACP_SCENARIO = "scenarios/unannounced-turn.yaml"
docker compose -f deploy/compose.yml up -d --build
```

The display is at <http://localhost:8000>. First build takes several minutes;
subsequent starts take seconds.

### What you should see, and when

| Scenario | What happens | When |
| --- | --- | --- |
| `head-on-conflict` | Two aircraft ring red, `PREDICTED_CONFLICT` advisory | ~2½ min in |
| `unannounced-turn` | ACP501 turns; `NON_CONFORMANCE` advisory appears and later clears | turn at 4 min, alert ~4½ min |
| `quiet-cruise` | Six aircraft, no alerts at all | never — that is the point |

The feed loops, so a scenario left running restarts rather than going empty.

---

## 3. Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /` | The plan-view display |
| `GET /docs` | Generated OpenAPI documentation |
| `GET /health` | Liveness. Checks nothing external — see below |
| `GET /ready` | Readiness. Checks Redis and Postgres, names which failed. **503 is normal** when a dependency is down |
| `GET /v1/tracks?window_s=20` | Current airspace picture |
| `GET /v1/tracks/{track_id}/history?limit=600` | Where a track has been. 404 if unknown |
| `GET /v1/alerts?kind=predicted_conflict&kind=emergency_squawk` | Active advisories. Repeat `kind` to filter; omit for all |
| `WS /v1/stream` | Live picture pushed once a second |

The committed specification is [`contracts/openapi.json`](../contracts/openapi.json),
and CI fails if it drifts from the code. **The WebSocket is deliberately absent
from it** — OpenAPI 3.1 cannot describe one. Its frame format is `StreamFrame` in
`acp/services/api/stream.py`: the same tracks and alerts the REST endpoints
return, in a single message.

### The stream

One background task reads Redis per interval and pushes the same frame to every
connected client, so backend load is a function of the airspace rather than of
how many people are watching ([ADR 0009](adr/0009-one-reader-fans-out-to-many-viewers.md)).
With nobody connected it does no work at all.

A client that stops reading is disconnected after a send timeout rather than
being allowed to stall the broadcast for everyone else. The display falls back
to polling the REST endpoints if the socket cannot connect — which is the
ordinary outcome behind a proxy that does not forward `Upgrade`.

**Health and readiness are different on purpose.** `/health` must never check a
dependency: a liveness probe that fails when the database hiccups gets the pod
killed and restarted in a loop, which fixes nothing. `/ready` does check, returns
503, and names the failing half so the pod leaves the load balancer but keeps
running.

---

## 4. Configuration

Every setting is an environment variable prefixed `ACP_`. Unprefixed variables
are ignored, so a stray `LOG_LEVEL` in a shared environment cannot reconfigure
anything.

| Variable | Default | Notes |
| --- | --- | --- |
| `ACP_KAFKA_BOOTSTRAP_SERVERS` | `localhost:19092` | `redpanda:9092` inside compose |
| `ACP_POSTGRES_DSN` | `postgresql+asyncpg://acp:acp@localhost:5432/acp` | |
| `ACP_REDIS_URL` | `redis://localhost:6379/0` | |
| `ACP_LOG_LEVEL` | `INFO` | |
| `ACP_API_PORT` | `8000` | |
| `ACP_HORIZONTAL_SEPARATION_NM` | `5.0` | En-route standard |
| `ACP_VERTICAL_SEPARATION_FT` | `1000.0` | En-route standard |
| `ACP_CONFLICT_LOOKAHEAD_S` | `300.0` | How far ahead conflicts are probed |

A nonsensical separation standard (zero or negative) **fails at startup** rather
than producing a detector that silently never alerts.

### Service flags

```
acp-feed --scenario=PATH [--replay] [--loop] [--no-truth]
acp-track [--group-id=track-service]
acp-conformance [--group-id=conformance-service] [--scan-interval-s=1.0]
                [--conformance-horizon-s=60] [--no-conformance] [--models-dir=models]
```

`--replay` runs the simulation flat out instead of pacing it against the wall
clock. Useful for generating data quickly; the conformance service handles the
resulting time skew by taking its clock from the data (see §7).

---

## 5. Scaling out

Run more instances of a service with the **same** `--group-id`. Kafka assigns
partitions across the group, and because every message is keyed by aircraft
address, each aircraft stays with one consumer and keeps its ordering.

```powershell
docker compose -f deploy/compose.yml up -d --scale track=3
```

Topics are created with 6 partitions, so up to 6 instances of a service can
share work. Beyond that, extra instances sit idle.

**The API scales independently** and statelessly — it holds no state and consumes
no Kafka.

---

## 6. Inspecting a running system

```powershell
# Logs are one JSON object per line
docker compose -f deploy/compose.yml logs -f conformance

# Anything gone wrong anywhere?
docker compose -f deploy/compose.yml logs | Select-String '"level":"ERROR"'

# Did the trajectory model actually load?
docker compose -f deploy/compose.yml logs conformance | Select-String "model_loaded"

# Kafka topics, offsets, consumer lag
docker compose -f deploy/compose.yml exec redpanda rpk topic list
docker compose -f deploy/compose.yml exec redpanda rpk group describe track-service

# Track history straight from the database
docker compose -f deploy/compose.yml exec postgres psql -U acp -d acp `
  -c "select track_id, count(*) from track_points group by 1;"
```

Every log line carries a `trace_id` propagated through Kafka headers, so one
surveillance report can be followed across all four services:

```powershell
docker compose -f deploy/compose.yml logs | Select-String '"trace_id":"abc123'
```

With `--profile tools`, the Redpanda console at <http://localhost:8080> shows
topic contents live.

---

## 7. Troubleshooting

**Nothing appears on the display.**
Check `/ready` first. If Redis or Postgres is down it says which. If both are up,
check the feed is running (`logs feed`) and that the tracker is consuming
(`rpk group describe track-service` — lag should be near zero).

**Aircraft appear but no alerts ever fire.**
Expected on `quiet-cruise`. On other scenarios, confirm the conformance service
is up and check its startup line for `model_loaded`. Conflict detection does not
need the model; conformance monitoring degrades to physics without it.

**The conformance service logs `model_loaded: false`.**
The artifact is missing or unreadable. The service still runs on dead reckoning
and says so in every alert's reason codes (`physics_prediction_only`). Retrain
with `python -m acp.ml.train --scenarios 120` and rebuild the image.

**`airspace picture exceeds the accurate projection envelope`.**
Traffic is spread over more than 300 NM, where the shared flat-earth projection
distorts separation by a meaningful fraction of the 5 NM standard. Detection
still runs but its distances are degraded. The real fix is one monitor per
airspace sector.

**The tracker crash-loops on startup.**
Almost always the database. Check `migrate` exited successfully — everything
touching Postgres waits on it, so a failed migration stops the tracker rather
than letting it run against a stale schema.

**A test fails saying NOMINAL scenario generation has changed.**
Something altered what the scenario generator produces, which means
`eval/results/conflict_detection.md` no longer describes the traffic it claims
to. Regenerate the report and bump `GENERATOR_VERSION`. **Do not** update the
fingerprint constant to make the test pass.

---

## 8. Regenerating the evaluations

Both are deterministic from a seed. Both write a committed report.

```powershell
# Conflict detection: ~4 minutes
python eval/run_conflict_eval.py --scenarios 120 --seed 20260815 --include-committed

# Trajectory prediction: ~7 minutes, also retrains and rewrites models/
python -m acp.ml.train --scenarios 120 --seed 20260815
```

Reports are stamped with every component version and a hash of the scenario set,
so a stale result is identifiable rather than merely suspicious.

**If you retrain, update `TYPICAL_MODEL_ERROR_NM` in
`acp/services/conformance/monitor.py`.** The conformance threshold is calibrated
from those figures; leaving them behind a more accurate model makes the monitor
progressively less sensitive with nothing to indicate it.

---

## 9. Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,messaging,storage,api,ml]"
.\scripts\run_checks.ps1
```

`run_checks.ps1` runs the **fast** gate: ruff, ruff format, mypy strict, pytest
over unit and contract with an 80% coverage floor, and the contract drift gate.
Everything needing Docker runs separately, as it does in CI:

```powershell
pytest tests/integration      # real Kafka, Postgres, Redis   ~1 min
pytest tests/e2e              # the whole stack under compose ~9 min
pytest tests/perf -s          # latency budget at 500 aircraft ~10 s
```

After changing a wire contract **or an API route**, regenerate the committed
artefacts or the build fails:

```powershell
python scripts/contracts.py
```

That writes the four Kafka JSON Schemas *and* `contracts/openapi.json`. The
schemas are additionally checked for backward compatibility against the version
in git: removing a field, making an optional field required, or changing a type
fails the build, because each breaks a consumer that has not been redeployed.
If the change is intended, it belongs on a new topic version with a dual-write
window.

Database schema changes need a migration:

```powershell
alembic revision -m "what changed"      # then write the upgrade and downgrade by hand
alembic upgrade head
```

Migrations are written by hand rather than autogenerated, so constraint names
and the downgrade path are deliberate. The writer references
`uq_track_points_track_observed` by name in its `ON CONFLICT` clause — an
autogenerated rename would silently break idempotency.

---

## 10. Kubernetes

Manifests arrive at M5, along with a `kind` smoke test in CI. The services are
already shaped for it: stateless where possible, configured entirely by
environment, and with liveness and readiness split correctly.
