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
| `GET /metrics` | Prometheus exposition. Excluded from the OpenAPI spec — it is an operational interface, not part of the product API |

The three worker services have no HTTP API but do serve `/metrics`, on
`ACP_METRICS_PORT` (default 9464). See §6.

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
| `ACP_METRICS_PORT` | `9464` | Where the three workers serve `/metrics`. The API uses `ACP_API_PORT` instead |
| `ACP_OTLP_ENDPOINT` | *(empty)* | Empty disables tracing. Set to e.g. `http://jaeger:4318/v1/traces` to turn it on |
| `ACP_ALLOWED_WEBSOCKET_ORIGINS` | *(empty)* | Extra browser origins allowed to open `/v1/stream`, comma-separated. Same-origin and non-browser clients are always allowed |

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

### Metrics, traces, and dashboards

Off by default — the demo does not need them and the images are another 500 MB.
Turning them on requires no rebuild:

```powershell
$env:ACP_OTLP_ENDPOINT = "http://jaeger:4318/v1/traces"
docker compose -f deploy/compose.yml --profile observability up -d
```

| | |
| --- | --- |
| Grafana | <http://localhost:3000> — dashboard **Airspace Conformance Platform**, provisioned; anonymous access is read-only, so no login is needed to view and none is possible to edit with |
| Prometheus | <http://localhost:9090> — check `Status → Targets`; four should be `up` |
| Jaeger | <http://localhost:16686> — pick service `acp-feed` to see a trace start at the edge |

**`ACP_OTLP_ENDPOINT` must be set for traces.** It is deliberately not defaulted
to the Jaeger URL: with the profile down, every service would retry an export to
a collector that is not there and fill its logs with failures caused entirely by
the default. Metrics need no such switch — they are always collected and always
served.

The dashboard answers four questions, one row each:

| Row | Reads |
| --- | --- |
| Is data flowing? | Publish rate by topic; reports in vs track updates out, which should track one for one |
| Is it keeping up? | Report-processing p50/p95/p99 against the 1 s budget, consumer lag, conflict-scan duration, discarded messages |
| Is the picture believable? | Live tracks, active alerts, alert transitions by kind and state |
| Is the clever part working? | Whether the trajectory model loaded, and the split of predictions between the model and the physics fallback |

A trace covers one surveillance report from the feed's `publish report` span,
through the tracker's `consume` and `estimate`, into the conformance service —
four spans across three processes, joined by a `traceparent` header on the Kafka
message. That is what makes "where did the seconds go" answerable; the
`trace_id` in the logs only makes it *greppable*.

Scraping the workers directly, without Prometheus:

```powershell
docker compose -f deploy/compose.yml exec track `
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9464/metrics').read().decode())"
```

**Without the `observability` extra installed, every metric is a no-op and every
span is dropped.** The service starts, logs one warning, and runs blind. That is
deliberate ([ADR 0010](adr/0010-observability-degrades-rather-than-blocks.md))
and is exercised by the `degradation` job in CI. The shipped image *does* include
the extra, so this is the fallback path rather than the normal one.

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

**Prometheus shows a target as `down`.**
The workers only bind the metrics port if `prometheus_client` is importable and
the port is free. Check the startup line: `metrics endpoint listening` means it
bound, `could not bind the metrics port` means something else has it, and
`prometheus_client is not installed` means the image was built without the
`observability` extra.

**Grafana panels say "No data" but Prometheus has the series.**
Usually the datasource uid. The dashboard queries `acp-prometheus`, which
`deploy/observability/grafana-datasources.yml` provisions;
`tests/unit/test_deployment.py` fails if those two ever disagree.

**Jaeger lists no services.**
`ACP_OTLP_ENDPOINT` is unset. Traces are opt-in; metrics are not.

**`migrate` exits with `ConnectionRefusedError: [Errno 111]` on a fresh volume.**
Fixed, and recorded here because the symptom is misleading: Postgres reports
healthy while it is still initialising, because `pg_isready` without `-h` checks
the Unix socket and the init-time server listens on the socket only. Both the
compose healthcheck and the Kubernetes readiness probe now pass `-h 127.0.0.1`.
If this reappears, that flag has gone missing.

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
.\.venv\Scripts\python.exe -m pip install -e ".[all]"
.\scripts\run_checks.ps1
```

`[all]` is every extra: messaging, storage, api, ml, observability, dev, and
integration. Install a subset if you want to work on one service, but be aware
that the fast gate then skips the tests for whatever is missing — which is why
CI installs everything for the normal jobs and deliberately installs *less* for
the `degradation` job.

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

```powershell
kind create cluster --name acp
docker build --tag acp:dev .
kind load docker-image acp:dev --name acp     # no registry involved
kubectl apply -f deploy/k8s/

kubectl -n acp rollout status deployment/redpanda --timeout=300s
kubectl -n acp wait --for=condition=complete job/migrate --timeout=180s
kubectl -n acp port-forward service/api 8000:8000
```

The display is then at <http://localhost:8000>, exactly as under compose.

`kind load` is not optional. The manifests name `acp:dev` with
`imagePullPolicy: IfNotPresent`, and that tag exists in no registry — skipping
the load leaves every application pod in `ImagePullBackOff` with an error
blaming Docker Hub rather than the missing side-load.

| | |
| --- | --- |
| `feed`, `conformance`, `api` | 1, 1, 2 replicas |
| `track` | 2 replicas — the one service that genuinely scales out, up to the 6 topic partitions |
| `conformance` | pinned to **1**. Every replica would hold the whole picture and publish duplicate alerts; it does not shard |
| Security context | non-root UID 1001, read-only root filesystem, all capabilities dropped, `seccompProfile: RuntimeDefault` |
| Probes | `/health` for liveness (checks nothing external), `/ready` for readiness (checks Redis and Postgres) |
| Metrics | workers annotated `prometheus.io/scrape`, since nothing fronts them with a Service to discover |

`deploy/k8s/README.md` argues the decisions; `tests/unit/test_deployment.py`
enforces the ones that can drift, and the `k8s` CI job applies the manifests to
a real `kind` cluster and drives traffic through the result. YAML that parses
proves nothing about whether a pod starts under the security context it claims.

**What these manifests are not.** Infrastructure runs as Deployments with
ephemeral storage, so a Postgres restart loses track history; the Postgres
password is committed so a clean checkout works; there is no Ingress, no
NetworkPolicy, no HPA, and no TLS anywhere. Each is stated in
`deploy/k8s/README.md` and `docs/limitations.md` rather than left for a reader
to discover.

---

## 11. CI and the release path

Twelve jobs, run in parallel except the last:

| Job | What would break without it |
| --- | --- |
| `lint`, `types` | Style and strict typing |
| `unit` | 512 tests, 80% coverage floor, no exclusions |
| `contracts` | A producer silently breaking a consumer; also backward-compatibility against the schema in git |
| `degradation` | Installs **without** `ml` and **without** `observability`, then proves both fallback paths actually work rather than merely being documented |
| `integration` | 28 tests against real Redpanda, Postgres, and Redis via testcontainers |
| `e2e` | The whole stack under compose, driving a scripted head-on conflict |
| `perf` | The latency budget at 500 aircraft. Informational — a budget that fails on a noisy shared runner teaches people to ignore the job |
| `compose` | Compose file syntax, an image build, and the image running under the Kubernetes security context — read-only root, non-root UID, no capabilities — in ten seconds rather than the five minutes `k8s` takes |
| `security` | bandit (SAST), pip-audit (dependency CVEs), gitleaks (secrets, full history), Trivy (image + deployment config), syft (SBOM artifact) |
| `k8s` | Applies the manifests to a `kind` cluster and asserts traffic reaches the API and the worker metrics endpoint answers |
| `publish` | Pushes to GHCR on `main`, tagged by commit SHA and `latest`, and only after every job above has passed |

`publish` is the one job with `needs:`. Everything else runs in parallel because
a lint failure and a test failure are independent facts and are more useful
together; pushing an image that failed its own tests is the one ordering that
actually matters.

Running the security checks locally:

```powershell
.\.venv\Scripts\python.exe -m pip install bandit pip-audit
.\.venv\Scripts\python.exe -m bandit --recursive src --severity-level medium --confidence-level medium
.\.venv\Scripts\python.exe -m pip_audit --skip-editable
docker run --rm -v "${PWD}:/repo" zricethezav/gitleaks:v8.21.2 detect --source /repo --config /repo/.gitleaks.toml --redact
```

bandit overlaps ruff's `S` ruleset on purpose: ruff's implementation is a
subset, the two are pinned to different release cadences, and the run costs
seconds. `pip-audit` is scoped to the extras the image installs rather than the
dev toolchain — a CVE in a linter that never runs in production is noise. Torch
is audited by Trivy inside the built image instead, because the CPU-only wheel
carries a `+cpu` local version that does not exist on PyPI for pip-audit to
resolve.
