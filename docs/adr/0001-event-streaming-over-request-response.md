# ADR 0001 — Event streaming between services, not request/response

Status: accepted · Date: 2026-08-15

## Context

Four stages have to run over a continuous stream of aircraft position reports:
generate, filter, analyse, serve. Something has to connect them.

The obvious alternative is HTTP: the feed POSTs to the tracker, the tracker
POSTs to the conformance service, the API polls the tracker.

The workload has three properties that decide the question:

- **Unbounded and continuous.** Reports arrive at roughly 1 Hz per aircraft for
  as long as the aircraft is airborne. There is no request that ends.
- **Fan-out.** A track update is needed by the conformance detectors, by the
  API's live display, and by the evaluation harness. Three consumers, one
  producer, all wanting the same bytes.
- **Ordering matters per aircraft, not globally.** A turn is only detectable if
  that aircraft's reports arrive in sequence. Two different aircraft have no
  ordering relationship at all.

## Decision

Kafka-protocol topics between every service, with the aircraft address
(`icao24`) as the partition key. Redpanda in development, any Kafka in
production.

Topics: `surveillance.reports.v1` → `tracks.updates.v1` → `airspace.alerts.v1`,
plus `sim.truth.v1` for evaluation only.

## Consequences

**What this buys**

- *Per-aircraft ordering for free.* Keying by `icao24` puts every report for one
  aircraft on one partition, and Kafka orders within a partition. Different
  aircraft land on different partitions and are processed concurrently. The
  ordering guarantee we need is exactly the one the partition key gives us, and
  we get parallelism everywhere else.
- *Consumers are added without touching the producer.* The evaluation harness
  reads `tracks.updates.v1` alongside the conformance service. Neither knows the
  other exists.
- *Back-pressure becomes visible instead of destructive.* A slow consumer builds
  measurable lag. Over HTTP the same slowness would push back on the producer,
  which cannot slow down — aircraft keep flying — so reports would be dropped.
- *Restarts replay.* A consumer that crashes resumes from its committed offset.
  With HTTP, in-flight work is simply lost.

**What it costs**

- A broker to run, and a broker to understand. This is real operational weight
  for four services.
- At-least-once delivery, which means consumers must be idempotent. Handled
  deliberately in ADR 0005.
- Debugging is harder: no stack trace crosses a topic. Mitigated by propagating
  a `trace_id` in Kafka headers (M5).

**Rejected: HTTP request/response.** Simpler for a reader, but it makes the
producer responsible for knowing every consumer, loses ordering guarantees the
moment a retry happens, and has no answer for a consumer that is temporarily
down other than dropping data.

**Rejected: a database as the queue.** Polling a table would work at this scale
and needs no new infrastructure. Rejected because the point of the exercise is
to build the streaming architecture, and because consumer groups, offsets, and
partition-based ordering are the mechanisms actually worth demonstrating.
