# M1 — Walking skeleton

What exists, why it was built that way, and where it is weak.

## The one-sentence version

All four services now run: a simulator generates aircraft, publishes noisy
position reports to Kafka, a tracker turns those into track estimates and stores
them, and an API serves a live display. It is deliberately thin — the tracker
does not filter and nothing raises alerts — but every part of the path exists
and runs, so M2 onwards deepens something already working rather than bolting on
disconnected pieces.

## Prove it works

```powershell
.\scripts\demo.ps1
```

Four aircraft appear within seconds. Two of them — ACP101 and ACP202 — are
converging head-on at FL350 and will pass inside the 5 NM separation standard
about eight minutes in. Nothing warns you about that yet; that is exactly what
M2 adds.

Verified on this machine at the end of M1: 4 live tracks, 231 rows persisted
after four minutes, zero `ERROR` lines across all six containers, and zero
duplicate `(track_id, observed_at)` groups in the database.

## What was built

### 1. The simulator — `src/acp/sim/`

A scenario is a committed YAML file: aircraft, initial states, a sensor model,
and a seed. `Simulation.advance()` moves everything forward; `truth()` returns
exact state; `observe()` returns what the sensor saw.

**The split between those last two is the most important line in the project.**
`observe()` adds Gaussian position, altitude, speed, and heading error, and
drops reports at a configurable rate. Only its output enters the pipeline.
`truth()` goes to a separate topic that no production code path consumes. That
is what will make the M2 conflict-detection metric meaningful despite the data
being synthetic: the detector is scored against a truth it genuinely never saw.

Noise is drawn from a **per-aircraft** generator seeded from the scenario seed
plus the aircraft's address. That means adding a fifth aircraft to a scenario
does not shift the noise sequence of the first four, so a result already
recorded for them stays valid. Tested directly
(`test_adding_an_aircraft_does_not_disturb_the_others`).

Two scenarios ship, and the pair matters:

- `head-on-conflict.yaml` — the conflict, plus two aircraft that fly nearby
  without conflicting. One of them is laterally close but 4,000 ft above; a
  detector that ignores altitude will fire on it.
- `quiet-cruise.yaml` — six well-separated aircraft, no conflict anywhere. This
  is the **false-alarm control.** Precision is meaningless if the detector has
  only ever been shown conflicts, because a detector that always fires would
  score perfectly on recall.

Both scenarios are asserted to actually do what their comments claim:
`test_the_head_on_scenario_actually_produces_a_conflict` and
`test_the_quiet_scenario_never_produces_a_conflict` walk the truth states and
check the geometry. A commented scenario that has drifted from its description
would quietly invalidate every metric derived from it.

### 2. The Kafka layer — `src/acp/common/messaging.py`

Three decisions are visible in the API itself.

**The publish key is mandatory, not optional.** Publishing without a key
round-robins the message across partitions, which destroys per-aircraft
ordering. The tracker would then read one aircraft's reports out of sequence and
compute nonsense turn rates. That failure is invisible until someone looks
closely at a turn, so the method signature refuses to allow it.

**Auto-commit is off.** The offset advances only after the handler body
finishes, which makes delivery at-least-once: a crash replays rather than drops.
The price is that handlers must be idempotent, which drove the database design
below.

**Malformed messages are skipped, not retried forever.** A record that fails
validation is logged and its offset committed. Halting instead would stop the
airspace picture updating for *every* aircraft because of one bad message. A
production system would route it to a dead-letter topic; that is recorded as a
gap rather than pretended away.

Topics are created explicitly with 6 partitions rather than left to
auto-creation, which would give them the broker default of 1 — correct, but with
no parallelism and no way to scale the tracker horizontally.

### 3. The tracker — `src/acp/services/track/`

`TrackEstimator` owns the track lifecycle: **initiating → confirmed → coasting →
terminated**. A single report is not a track; three consistent ones is. After
5 seconds of silence a track coasts, after 30 it terminates.

**M1 does not filter.** Reported values pass through, gaps are filled from the
last known value, and turn rate is the signed shortest heading difference
divided by elapsed time. The uncertainty it publishes is a **named placeholder
constant**, not a computed covariance, and it says so everywhere it appears. M2
replaces the arithmetic inside `on_report`; the lifecycle around it does not
change.

You can see the missing filter on the display: the flight level for a level
aircraft flickers between FL349 and FL350, because 25 ft of altitude noise goes
straight through. That flicker disappearing is how you will know M2 worked.

`TrackRunner` adds two things worth understanding:

- **Batched writes.** Updates buffer and flush on whichever comes first: 50
  updates or 1 second. One database round trip per aircraft per second would put
  I/O in the critical path of every report; the time bound stops a quiet
  airspace from holding the last few updates indefinitely.
- **A sweep on a timer.** A track that stops reporting produces no message to
  trigger its own expiry, so something has to look at the clock instead. Without
  it, an aircraft that left coverage would sit on the display forever at its
  last known position — the most dangerous failure this system could have, because
  a frozen display looks exactly like a working one.

### 4. Storage — `src/acp/storage/`

Postgres holds history, Redis holds the live picture. Different questions,
different stores:

- Postgres answers "where has this aircraft been?" — append-only, queried
  rarely. Written with SQLAlchemy **Core**, not the ORM: these are flat bulk
  inserts and there is no object graph for an ORM to buy anything with.
- Redis answers "what is in the air now?" — read on every display refresh, and
  worthless the moment it is stale. Keys carry a TTL, so if the tracker dies the
  picture empties itself rather than showing aircraft that stopped existing
  minutes ago.

**Idempotency lives in a unique constraint.** `(track_id, observed_at)` is
unique, and inserts use `ON CONFLICT DO NOTHING`. At-least-once delivery means a
redelivered report *will* arrive; without the constraint every consumer restart
would silently inflate the track history in a way nobody would notice later.
Verified against the running database: zero duplicate groups.

Migrations are Alembic, hand-written rather than autogenerated so the constraint
names and the downgrade path are deliberate. The constraint name is referenced
by the writer's `ON CONFLICT` clause — an autogenerated name would work until
someone regenerated it differently and idempotency broke silently.

### 5. The API and display — `src/acp/services/api/`

FastAPI, read-only. It never consumes Kafka and never writes, so a display
refresh cannot touch the pipeline estimating the picture.

**Health and readiness are split deliberately.** `/health` is liveness and
checks nothing external; `/ready` is readiness and checks Redis and Postgres
separately, returning 503 and naming which half failed. Getting these the wrong
way round is the classic Kubernetes outage: a liveness probe that checks the
database turns a recoverable dependency blip into a restart loop that fixes
nothing. There is a test for it.

The display is a canvas plan-view in vanilla JavaScript — no build step, no CDN,
no external assets, because the page has to render with no network beyond its
own origin. Aircraft are chevrons rotated to track, with a real data block
(callsign / flight level / ground speed) and a speed vector showing where dead
reckoning puts them in one minute. That vector is worth pointing out in an
interview: it is literally the physics baseline the M3 model learns a correction
to, drawn on screen.

## Two things that went wrong, and what they taught

### The architecture test caught its own author

The API needed to read the same Postgres and Redis data the tracker writes, so
the first version simply imported `acp.services.track.store`. The build failed:

```
acp.services.api.app imports another service: ['acp.services.track.store'].
Cross-service communication goes through Kafka or HTTP.
```

That is the exact shortcut the test was written to prevent — taken within a day,
by the person who wrote the rule, for an entirely reasonable-seeming reason. The
fix was to lift storage into `acp.storage`, a layer both services may depend on.
[ADR 0004](../adr/0004-shared-read-model-between-tracker-and-api.md) records why
that beats the alternatives and names the cost honestly: two services now share
a database schema, which is a recognised anti-pattern, tolerable here only
because exactly one service writes and the schema is versioned in one place.

**If you take one thing to an interview from M1, take this one.** It is a
concrete answer to "how do you stop an architecture eroding?" that does not rely
on discipline or code review.

### Postgres refused the batch upsert

The tracker crash-looped on startup with:

> `ON CONFLICT DO UPDATE command cannot affect row a second time`

A batch normally holds many updates for the same aircraft, and Postgres will not
let one statement update the same row twice. The fix is to collapse to the
newest update per track before the upsert — which is also what the `tracks`
table *means*, so the bug was really a modelling slip showing up as a database
error. Worth knowing because the same trap catches people writing any batched
upsert.

## Where this is weak

- **No filtering.** Sensor noise goes straight through to the display and to the
  database. This is M1's defining limitation and it is labelled as such in the
  code, the README, and here.
- **No alerts.** Nothing detects the conflict the demo scenario is built around.
- **Track identity is simplified.** One track per aircraft address, forever. A
  real system issues a fresh track number when an aircraft reappears after a
  long absence, so two separate flights by the same airframe do not merge.
  Acceptable for a single continuous scenario; not acceptable against a live
  feed. Documented at `track_id_for`.
- **No data association.** Reports carry an aircraft identifier, so the tracker
  never has to work out which return belongs to which aircraft — the genuinely
  hard part of real radar tracking is absent by construction. This is in
  `safety-notes.md` and will be in `limitations.md`.
- **Two files are excluded from coverage.** `messaging.py` and `stores.py` are
  pure I/O adapters; the exclusion is narrow, named, and annotated with
  instructions to delete it when the integration suite lands at M5.
- **The e2e verification was done by hand.** The numbers above were checked
  against a running stack manually. Automating that is M4.

## Questions a reviewer might ask

**"Why Redis *and* Postgres? Isn't one enough?"**
Postgres alone would work, and the API would query it on every display refresh —
putting read load on the same database absorbing a write per aircraft per
second. Redis holds a purpose-built read model that is cheap to overwrite and
expires on its own. The TTL is the real argument: a stale entry disappearing by
itself is a safety property, not a caching detail.

**"Why batch the writes? Doesn't that risk losing data?"**
It does, and the code says so: the shutdown path flushes before returning
because the consumer offset is already committed for buffered work, so anything
still in memory has no redelivery to recover it. The trade is one database round
trip per second per instance instead of one per aircraft per second. At 500
aircraft that is the difference between 500 writes a second and 1.

**"At-least-once means duplicates. How do you know they are actually handled?"**
A unique constraint on `(track_id, observed_at)` plus `ON CONFLICT DO NOTHING`,
and a test that feeds the same reports through the tracker twice and asserts one
track results. Confirmed against the live database: zero duplicate groups after
a full scenario.

**"The tracker is a single consumer. What happens when one instance is not
enough?"**
Run more with the same `--group-id`. Kafka assigns partitions across the group,
and because reports are keyed by aircraft address, each aircraft stays with one
consumer and keeps its ordering. That is the whole reason for the keying
decision in ADR 0001, and it is why topics are created with 6 partitions instead
of the default 1. Untested at M1 — consumer-group rebalance is an integration
test at M5.

## Next

M2 — domain depth: a real flight model with climbs, turns and manoeuvres, a
Kalman filter replacing the passthrough, the separation monitor that finally
detects the conflict the demo has been quietly staging all along, and the first
committed evaluation report with precision, recall, and warning lead time.
