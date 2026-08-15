# ADR 0004 — A shared storage layer, with the tracker writing and the API reading

Status: accepted · Date: 2026-08-15

## Context

This decision was forced by a failing test rather than chosen in advance, which
is worth recording honestly.

The API service needs the current airspace picture and track history. Both live
in Redis and Postgres, written by the track service. The first implementation
simply imported the tracker's storage module from the API — and
`tests/unit/test_architecture.py`, written a day earlier specifically to forbid
one service importing another, failed the build:

```
acp.services.api.app imports another service: ['acp.services.track.store'].
Cross-service communication goes through Kafka or HTTP.
```

The rule was right and the code was wrong. Four ways out were available.

1. **Lift storage into a layer both services may depend on.**
2. **Have the API ask the tracker over HTTP.** The tracker would gain a query
   API and serve reads while it is trying to keep up with a real-time stream.
3. **Give the API its own Kafka consumer** and let it build a private read model
   from `tracks.updates.v1`.
4. **Move the boundary** — merge the API into the tracker.

## Decision

Option 1. `acp.storage` sits at the bottom of the stack next to `acp.common`.
The track service writes through it; the API service reads through it; neither
imports the other. The architecture test now enforces that `acp.storage` itself
imports nothing above it.

## Consequences

**Why not the alternatives**

- *HTTP to the tracker* puts read traffic on the process doing real-time state
  estimation. A display refresh would compete with report processing, and the
  tracker would have to be scaled for query load it should not care about.
  It also makes the API's availability depend on the tracker's.
- *A private read model in the API* is the textbook answer and would give full
  independence. Rejected as disproportionate: it means a second consumer group,
  a second copy of the state, and a second set of staleness bugs, to avoid
  sharing a schema that is already versioned and migrated in one place.
- *Merging the services* would remove the problem by removing the boundary. The
  boundary is worth having: the API is stateless, scales with viewers, and must
  stay responsive whether or not the pipeline is healthy.

**The honest cost.** Two services now share a database schema. That is a real
coupling and a recognised microservices anti-pattern — a migration has to be
compatible with both, and neither can change storage independently. Three things
make it tolerable here, and they are the conditions under which it stops being
tolerable:

1. **The access is asymmetric.** Exactly one service writes. There is no
   write-write contention and no ambiguity about ownership.
2. **The schema is versioned and migrated in one place**, so it behaves like the
   Kafka contracts: a change is a deliberate, reviewable act.
3. **The read path is Redis**, not Postgres. The API's hot path touches a
   purpose-built read model that the tracker maintains for it; Postgres is only
   for the occasional history query.

If a third service ever needed to write, or the read pattern diverged from what
the tracker naturally produces, option 3 becomes the right answer.

**What the incident says about the practice.** The rule was written first, it
was violated within a day by the person who wrote it, and an automated check
caught it in seconds rather than in review months later. That is the argument
for architecture fitness functions in one paragraph.
