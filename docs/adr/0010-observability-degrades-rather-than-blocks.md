# ADR 0010 — Observability degrades rather than blocks

Status: accepted · Date: 2026-08-16

## Context

M5 added metrics and distributed tracing. Three questions had to be answered
before a line of it was written, and each had a tempting wrong answer.

**1. What happens when the observability stack is not there?**

`prometheus_client` and the OpenTelemetry SDK live in an optional extra. A
service can be started without them — by a developer running one process
locally, by a slimmed image, by a deployment that has not got round to
installing a collector yet.

**2. How do three services with no HTTP surface get scraped?**

The API serves HTTP already, so `/metrics` is one route. The feed, tracker, and
conformance services consume Kafka and publish to it. They have no listening
socket at all, which is the point of their design.

**3. How does a trace cross a Kafka broker?**

Auto-instrumentation handles HTTP because a request and its response are one
exchange. A Kafka message is not: the producer finishes long before the consumer
starts, and nothing links them but the bytes on the message.

## Decision

**Every observability call site degrades to a no-op.** `Metrics` builds
`_NoOpMetric` stand-ins when the library is absent; every function in
`acp.common.tracing` returns or yields without doing anything when the SDK is
absent. A missing extra produces one warning line at startup and no other
behavioural difference.

**The three workers each start a Prometheus exposition server on a background
thread** (`ACP_METRICS_PORT`, default 9464). Failure to bind is logged and
tolerated.

**Trace context is injected and extracted manually**, as W3C `traceparent`
headers on the Kafka message, alongside the plain `x-acp-trace-id` correlation
id that M1 introduced.

## Consequences

**A degraded deployment is a running deployment.** This is the same principle
as the trajectory model falling back to dead reckoning (ADR 0007): the system
does less, says so, and keeps processing traffic. A service that refused to
start because it could not find a metrics library would be a system whose
availability depended on its monitoring — precisely backwards.

The claim is executable, not aspirational. The `degradation` CI job installs
without `ml` **and** without `observability`, then exercises every call site the
codebase contains against the no-ops.

**A background thread is the price of scrapeable workers.** The alternative was
to leave the workers unscrapeable and write the gap down in
`docs/limitations.md`. That was drafted and then rejected: counters that are
maintained but unreachable look like observability while being useless, which is
strictly worse than not instrumenting at all, because the gap is then invisible
in the code and visible only in an empty dashboard. One thread and one port is a
much smaller cost than a monitoring story that does not survive contact with a
scrape config.

The trade-off, stated plainly: two workers on one host both default to 9464 and
the second fails to bind. Under compose and Kubernetes each has its own network
namespace, so this only affects running two directly on a developer machine, and
`ACP_METRICS_PORT` overrides it.

**Instrumentation lives at the boundary, not at the call sites.** Messages
published are counted inside `MessagePublisher`, discarded messages and consumer
lag inside `MessageSubscriber`. Counting at call sites would have meant the feed
— a pure producer with no other instrumentation — having no metric at all, and
would have left every future producer to remember.

**The span has to be opened at the edge.** A `traceparent` header can only carry
a context that already exists. The feed originally published without opening a
span, and the result was visible on the first run of the stack: Jaeger showed
`acp-track` and `acp-conformance` and no feed, so the one interval anybody
actually wants — report emitted to alert raised — was the one interval missing.
A `publish report` span in the feed runner makes the trace start where the data
does.

**What this does not give you.** Update latency of the metrics is the scrape
interval, and the traces are sampled at 100% because the traffic is a
demonstration. Both would be wrong at real volume; both are one configuration
change, and neither is worth pretending about at four aircraft.

## Alternatives rejected

**A sidecar exporter per worker.** Correct in a large Kubernetes estate, and
entirely disproportionate here: it adds a container, a shared volume or a local
socket, and a second thing to keep in step with the application, in exchange for
avoiding one thread.

**A Pushgateway.** Designed for batch jobs that exit before they can be scraped.
These workers are long-running, and Pushgateway on long-running processes is a
well-documented anti-pattern: it breaks the up/down signal and makes stale
series indistinguishable from live ones.

**OpenTelemetry auto-instrumentation for Kafka.** Instruments the client
library, not the message semantics; it would have produced producer and consumer
spans without linking them into one trace across the broker, which is the entire
question.

**Making the observability extra mandatory.** Would have removed roughly forty
lines of fallback code and made every claim in this ADR unnecessary. Rejected
because "the service will not start without a metrics library" is a failure mode
this domain should not accept, and because the fallback code is what the
`degradation` job proves rather than asserts.
