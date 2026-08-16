# M5 — DevSecOps and operations

What exists, why it was built that way, and where it is weak.

## The one-sentence version

The system can now be watched, scanned, and deployed: Prometheus metrics on all
four services, one trace following a surveillance report across three processes
into Jaeger, a Grafana dashboard that ships with the repo, Kubernetes manifests
applied to a real cluster in CI, four security tools with an SBOM, and images
published to GHCR — and building it found three defects, all three in work the
previous milestones had already shipped and passed CI on.

## The three defects, because they are the point

**1. Postgres reported healthy before it was listening.** Bringing the stack up
on a cold volume, `migrate` died with `ConnectionRefusedError: [Errno 111]`
against a container compose had already declared healthy. The healthcheck was
`pg_isready -U acp -d acp`, which with no `-h` checks the **Unix socket** — and
during first-boot initialisation Postgres runs a temporary server that listens on
the socket only. So the check passed while every TCP client was refused. The same
bug was in the Kubernetes readiness probe, copied from the same line. Both now
pass `-h 127.0.0.1` and check the interface the clients actually use.

This had been latent since M1. It only fires on a cold volume, which is exactly
the case a CI runner always hits and a developer with a warm volume never does.

**2. The trace did not start where the data does.** With tracing switched on for
the first time, Jaeger listed `acp-track` and `acp-conformance` and no feed. The
reason is structural, not a typo: a `traceparent` header can only carry a context
that already exists, and the feed published without opening a span. So the trace
began at the first consumer, and the one interval anybody actually wants to
measure — report emitted to alert raised — was the one interval missing. A
`publish report` span in the feed runner fixed it. Traces now show four spans
across three services.

**3. An e2e test's wait condition did not match its assertion.** It waited for
the history endpoint to *respond* — which happens as soon as one row exists — and
then asserted on ten rows. That passed for as long as the stack happened to take
longer to start than the tracker took to write ten rows, and failed the first
time the ordering went the other way. Found by running it, not by reading it. The
predicate is now the same condition as the assertion.

### And four things that were not defects, only rot

Found by a deliberate sweep for names defined and never referenced, run at the
end of the milestone rather than trusted to review:

- `FEET_PER_NM` in `geodesy.py` — a constant nothing converted with.
- `MONITOR_VERSION` and `RULES_VERSION` — version stamps in the shape of the
  ones the evaluation report consumes, except nothing consumed these. An unused
  provenance stamp is the same failure as an unreachable counter: it looks like
  traceability and tracks nothing. Removed, with a comment saying to add one
  back when there is a report to stamp.
- `Settings.service_name` and `Settings.kafka_consumer_group` — configuration
  nothing read. Someone sets `ACP_SERVICE_NAME=track`, nothing happens, and
  there is no error to explain why. That is worse than the setting not existing.

The `deploy/k8s/README.md` file table also listed six filenames that had never
existed, and documented `kubectl apply -k` against a directory with no
kustomization in it. Both corrected.

## What was built

### Metrics — `src/acp/common/metrics.py`

Twelve series, chosen to answer four questions an operator actually asks rather
than to be comprehensive:

| Question | Series |
| --- | --- |
| Is data flowing? | messages published, reports consumed, track updates published, alerts published |
| Is it keeping up? | report-processing histogram, conflict-scan histogram, consumer lag, messages discarded |
| Is the picture believable? | live tracks, active alerts |
| Is the clever part working? | trajectory model loaded, predictions by source |

**Consumer lag is the single most useful number in a Kafka system**, because it
distinguishes *slow* from *failing*: a flat non-zero lag is fine, a rising one is
a backlog. It is read from the consumer's cached high-water mark, so it costs
nothing per message.

**Instrumentation sits at the boundary, not at call sites.** Publishing is
counted inside `MessagePublisher`; discards and lag inside `MessageSubscriber`.
Counting at call sites would have left the feed — a pure producer, and the
service most likely to be silently dead — with no metric at all.

**`acp_trajectory_model_loaded` deserves its own mention.** It is a gauge that
answers "have we been running on physics for three weeks", which is the kind of
degradation that is invisible in logs nobody scrolls back through and obvious on
a dashboard.

### Getting the workers scraped

The API serves HTTP already, so `/metrics` is one route. The other three consume
Kafka and have no listening socket at all.

The first draft of `deploy/observability/prometheus.yml` documented that as a
gap: worker metrics reach nobody, written down honestly rather than papered
over. Writing that sentence made it obviously wrong. **Counters that are
maintained but unreachable look like observability while being useless** — worse
than no instrumentation, because the gap is then invisible in the code and shows
up only as an empty dashboard.

So `serve_metrics()` starts a `prometheus_client` exposition server on a
background thread, on `ACP_METRICS_PORT` (9464 by default, deliberately not
node_exporter's 9100). Failure to bind is logged and tolerated: a metrics
endpoint is not worth refusing to process air traffic over.

### Tracing — `src/acp/common/tracing.py`

W3C trace context, injected into Kafka headers on publish and extracted on
consume. **Manually, not by auto-instrumentation**, because auto-instrumentation
handles HTTP — one request, one response, one exchange — and a Kafka message is
not that. The producer finishes long before the consumer starts and nothing links
them but bytes on the message.

The plain `x-acp-trace-id` header from M1 still travels alongside, so `grep`
keeps working with no collector deployed, which is the common case for anyone
running this locally.

### Everything degrades — [ADR 0010](../adr/0010-observability-degrades-rather-than-blocks.md)

`prometheus_client` and the OpenTelemetry SDK live in an optional extra. Without
them every metric is a no-op, every span is dropped, one warning is logged, and
the service runs.

That is the same principle as the trajectory model falling back to dead reckoning
(ADR 0007), and it is **executable rather than aspirational**: the `degradation`
CI job installs without `ml` *and* without `observability`, then exercises every
call site the codebase contains against the no-ops. A service whose availability
depends on its monitoring is precisely backwards.

The shipped image does install the extra. Discovering otherwise would have been a
quiet disaster — Prometheus scraping a system with nothing to say, and no error
anywhere.

### Grafana — provisioned, not clicked in

`deploy/observability/` carries the datasource, the dashboard provider, and the
dashboard JSON. A fresh `docker compose --profile observability up` lands on a
working dashboard with no manual setup, because a dashboard that needs ten
minutes of configuration first is a dashboard nobody opens.

`allowUiUpdates: false` is deliberate: the dashboard is a committed artefact, and
letting the UI edit it would produce a live dashboard that no longer matches the
JSON anyone reviews, with the drift invisible.

### Kubernetes — `deploy/k8s/`

Namespace, ConfigMap and Secret, three infrastructure Deployments, a migration
Job, four application Deployments, one Service. Non-root UID 1001, read-only root
filesystem, all capabilities dropped, `seccompProfile: RuntimeDefault`, resource
requests and limits on every container.

Two replica counts are decisions rather than defaults:

- **`track: 2`** — the one service that genuinely scales out. Reports are keyed
  by aircraft address, so Kafka assigns partitions across the consumer group and
  each aircraft stays with one replica, keeping its ordering. Useful up to the
  six topic partitions.
- **`conformance: 1`** — pinned. Every replica would hold the whole airspace
  picture and publish duplicate alerts. Raising the number would produce a
  visibly wrong system, so the manifest says why in a comment rather than
  leaving a reader to find out.

The `k8s` CI job applies all of it to a `kind` cluster, waits for rollouts, and
then checks that traffic actually reaches the API and that a worker's metrics
endpoint answers. `kubectl apply --dry-run` proves the YAML parses; it proves
nothing about whether a pod starts under the security context it declares, which
is where `readOnlyRootFilesystem` and a non-root UID usually turn out to be
wrong.

### Security — four questions, one job

| Tool | Question |
| --- | --- |
| bandit | Is my code doing something dangerous? |
| pip-audit | Do my dependencies have known CVEs? |
| gitleaks | Is there a secret anywhere in my history? |
| Trivy + syft | What is in my image, and what is wrong with it? |

**bandit overlaps ruff's `S` ruleset on purpose.** Ruff implements a subset of
bandit's checks, the two are pinned to different release cadences, and the run
costs seconds. Cheap overlap beats a gap between two tools that both claim to do
SAST.

**gitleaks scans the full history** (`fetch-depth: 0`), because a secret that was
committed and then removed is still in the objects and still compromised.
Scanning only the tip would report clean on exactly the case that matters. The
one committed development credential is allowlisted by its literal value in
`.gitleaks.toml`, so a *different* password committed to the same file still
fails the build.

**pip-audit is scoped to what the image installs**, not the dev toolchain: a CVE
in a linter that never runs in production is noise. Torch is excluded because the
CPU-only wheel carries a `+cpu` local version that does not exist on PyPI for
pip-audit to resolve — it is covered by the Trivy image scan instead, which is a
different tool with a different database rather than an equivalent one.

**Only fixed CRITICAL/HIGH image findings fail the build.** An unfixed CVE in a
base image is real, but failing every run over something with no available patch
trains people to ignore the job — which is how a fixable one gets ignored too.

### CD — publishing to GHCR

`publish` builds and pushes on `main`, tagged by both commit SHA and `latest`, so
a deployment can name an exact build and a rollback has something to roll back
to. It is the only job with `needs:`, listing every gate that could say the image
is broken. Everything else runs in parallel, because a lint failure and a test
failure are independent facts and a developer wants both in one run.

Permissions are read-only at the workflow level and widened to `packages: write`
inside that one job, rather than every job inheriting the ability to write
packages.

### Deployment artefacts as code — `tests/unit/test_deployment.py`

Sixteen tests treating the compose file, the manifests, the scrape config, and
the dashboard as what they are: code that nothing type-checks and nothing
imports. They fail if the dashboard queries a metric the code no longer creates,
if a scrape annotation names a port nothing listens on, if the compose metrics
port and the settings default disagree, or if a published port stops binding to
loopback.

These things drift silently. A renamed metric leaves a panel reading "No data",
which looks exactly like a healthy idle system.

## What this milestone proves, and what it does not

**Proves.** The manifests start real pods and pass real traffic. The trace really
does cross two broker hops. The metrics really are scrapeable from all four
services — verified by running the stack, checking Prometheus reported four
targets `up`, and running every one of the dashboard's twelve queries against it.
The degradation claims are executed rather than asserted.

**Does not prove.** Nothing alerts — Prometheus scrapes and Grafana draws, but
there are no alerting rules and no Alertmanager, so "consumer lag is climbing" is
visible only to someone looking at the page. The `kind` job runs one node with no
resource pressure, no node failure, and no rolling update under load. Traces are
sampled at 100%, which is fine at four aircraft and wrong at real volume.
Infrastructure runs as Deployments with ephemeral storage, so a Postgres restart
loses track history. There is no Ingress, no NetworkPolicy, no TLS, and no
authentication anywhere.

All of it is in [`limitations.md`](../limitations.md) §5a.

## Numbers

| | |
| --- | --- |
| Fast gate | 512 tests, 81% coverage, no exclusions |
| Integration | 28 tests against real Redpanda, Postgres, Redis |
| End to end | 8 tests under docker compose |
| CI jobs | 12, of which one is CD |
| Prometheus targets in the dev stack | 4, all `up` |
| Spans in one report's trace | 4, across 3 services |

## Questions a reviewer might ask

**"Why a background thread instead of a sidecar?"** A sidecar is correct in a
large Kubernetes estate and disproportionate here: a container, a shared volume
or socket, and a second thing to keep in step with the application, to avoid one
thread. A Pushgateway would be worse — it is designed for batch jobs that exit
before they can be scraped, and on long-running processes it breaks the up/down
signal and makes stale series indistinguishable from live ones.

**"Isn't running bandit and ruff's S rules just duplication?"** Yes, deliberately.
Ruff's implementation is a subset and the two release on different schedules. The
duplication costs seconds; a gap between two tools that both claim to do SAST
costs a finding.

**"You committed a password."** Yes, so a clean checkout runs, and it is called
out in the manifest itself, in `deploy/k8s/README.md`, and in `limitations.md`.
gitleaks allowlists that one literal string and nothing else, so the scan still
catches a real credential added to the same file.

**"Why is `conformance` pinned to one replica when the whole point is
microservices?"** Because it holds the whole airspace picture and does not shard.
Setting it to 2 would produce a system that publishes every alert twice — visibly
wrong, and a much worse answer than admitting one service does not scale out yet.
Sector-partitioning it is the fix and it is on the roadmap, unbuilt.

**"How do I know the metrics are real and not just declared?"** Bring the stack
up with `--profile observability`, open Prometheus at :9090, and check
`Status → Targets`. Then open Grafana at :3000. That was the acceptance test for
this milestone, run by hand, and every dashboard query returned data except the
two that correctly return nothing when nothing has gone wrong.
