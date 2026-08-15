# ADR 0009 — One reader fans out to many viewers

Status: accepted · Date: 2026-08-15

## Context

The display needs live updates. Three ways to get them:

1. **Each browser polls the REST endpoints.** What M1–M3 did: every viewer hits
   `/v1/tracks` and `/v1/alerts` once a second.
2. **Each WebSocket connection runs its own poll loop** against Redis, pushing
   what it reads to its own client.
3. **One shared reader** polls Redis once per interval and pushes the same
   frame to every connected client.

## Decision

Option 3. A single background task in the API reads the picture once per
interval and broadcasts it. The REST endpoints stay, and the display falls back
to polling them if the WebSocket cannot connect.

## Consequences

**Load becomes a function of the airspace, not the audience.** Options 1 and 2
both multiply backend work by the number of people watching. The number of
aircraft is a fact about the world; the number of viewers is not, and a system
whose cost scales with attention is one that gets slower exactly when it is
being watched most. `test_one_broadcast_reads_the_store_once_regardless_of_viewers`
pins it.

**Tracks and alerts arrive together.** Polling fetched them with two independent
requests, which could land either side of an update and render an alert against
a picture that did not yet contain the aircraft it named. One frame removes that
class of inconsistency entirely.

**Nobody watching costs nothing.** The loop skips the read when there are no
clients, so an idle deployment does no work at all.

**The honest limitation: this is server-side polling with push, not
event-driven streaming.** Update latency is bounded by the poll interval, not by
how quickly an alert is produced. At a one-second interval against a one-second
report rate it is not the bottleneck, and if it became one the fix is a Kafka
consumer feeding the same broadcaster rather than a change to the client
protocol. Stated in the module docstring so nobody mistakes the endpoint for
something it is not.

**The API still never consumes Kafka.** Reading the read model the tracker
already maintains keeps ADR 0004 intact and keeps a display refresh entirely off
the pipeline that is estimating the picture. That was the deciding factor
against having the API subscribe directly.

**Backpressure is a policy, and the policy is to drop the slow client.** Each
send is bounded by a timeout; a client that misses it is disconnected and its
dropped-frame count logged. Freezing the broadcast for every viewer because one
of them stopped reading would be a worse failure than losing that one viewer.

**Polling remains as a fallback, deliberately.** A proxy that does not forward
`Upgrade` is an entirely ordinary deployment problem, and a display that went
blank behind one would be worse than a display that costs slightly more. The
client reconnects with exponential backoff and polls while disconnected.

## What this cost

A dataclass defect that no unit test caught: `_Client` was a plain
`@dataclass`, which generates `__eq__` and therefore sets `__hash__ = None`, so
adding one to a set raised `TypeError: unhashable type` on the first real
connection. The end-to-end suite found it immediately. `eq=False` restores
identity semantics, which is also the correct meaning — two viewers are the same
only if they are the same connection.
