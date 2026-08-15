# M4 — Real-time surface and the full test pyramid

What exists, why it was built that way, and where it is weak.

## The one-sentence version

The display now streams over a WebSocket, and the system is tested at every
level it claims to work at: contract, integration against real infrastructure,
end to end under docker compose, and a latency budget with a number attached.
Four suites found four defects that the 421 existing tests could not.

## The four defects, because they are the point

Every one of these was invisible to the unit suite, and each was found by the
level of testing designed to catch exactly that class of problem.

**1. The OpenAPI spec was lying, twice.** Fuzzing the implementation against its
own published document found that `/v1/tracks/{track_id}/history` returns a 404
the spec never mentioned, and that `/v1/alerts` declared its `kind` filter
*nullable* — so the spec claimed `?kind=null` was valid while the server
rejected it with a 422. A query parameter cannot meaningfully be null; the fix
was a repeated parameter (`?kind=a&kind=b`), which is both honest and more
useful.

**2. `ContextVar.reset()` across an async generator boundary.** A token may only
be reset in the context that created it, and an async generator can resume — or
be closed — in a different one. Breaking out of a consumer loop early raised
`ValueError: Token was created in a different Context` from inside a `finally`,
masking whatever the caller was actually doing. Saving and restoring **by value**
fixes it. No fake broker could have produced this; it needed a real one.

**3. The WebSocket could not accept a single connection.** `_Client` was a plain
`@dataclass`, which generates `__eq__` and therefore sets `__hash__ = None`, so
putting one in a set raised `TypeError` on the first real connect. The e2e suite
found it on its first run.

**4. My own at-least-once test asserted at-most-once.** It required a restarted
consumer to see exactly 0–9 with no repeats. It failed, correctly: the consumer
stopped after taking message 3 and *before* its offset was committed, so message
3 arrived again. That is the guarantee working. The system was right and the
test was wrong, and the corrected test now states the real contract — lose
nothing, replay at most the one in flight.

## What was built

### The live stream — `src/acp/services/api/stream.py`

**One reader, many viewers.** A single background task polls Redis once per
interval and pushes the same frame to every client. A per-connection poll loop
would multiply backend work by the size of the audience — and the number of
aircraft is a fact about the world while the number of people watching is not.
With nobody connected it does not read at all.

Tracks and alerts travel in **one frame**, so the display can no longer render
an alert against a picture that does not yet contain the aircraft it names — a
real inconsistency the two-request polling design allowed.

[ADR 0009](../adr/0009-one-reader-fans-out-to-many-viewers.md) records the
choice, including the honest limitation: this is server-side polling with push,
not event-driven streaming. Update latency is bounded by the interval, not by
how fast an alert is produced.

Polling stays as a fallback. A proxy that does not forward `Upgrade` is an
ordinary deployment problem and a blank display would be a worse answer than a
slightly more expensive one.

### Contract tests — `tests/contract/`

Three distinct things, easily confused:

- **Drift**: `contracts/openapi.json` and the four Kafka schemas are committed,
  and regenerated in CI to check they still match the code. A spec allowed to
  drift is one nobody can generate a client from.
- **Compatibility**: schemas are diffed against the version in git. Removing a
  field, making an optional field required, or changing a type fails the build,
  because those break consumers that have not been redeployed.
- **Conformance**: schemathesis generates requests from the spec — including
  inputs no hand-written test would think of — and asserts every response
  carries a documented status code and matches its declared schema.

### Integration tests — `tests/integration/`

Real Redpanda, Postgres, and Redis via testcontainers, pinned to **the same
image versions `deploy/compose.yml` runs**, so a pass says something about the
stack that actually ships.

These make claims the unit suite structurally cannot: that keying puts one
aircraft on one partition *and different aircraft on different ones*, that a
consumer group divides partitions the way `--scale track=3` assumes, that a
restarted consumer resumes correctly, and that the unique constraint really does
absorb a replayed batch.

**The coverage exclusion is gone.** `common/messaging.py` and `storage/stores.py`
were omitted from coverage back in M1 on the promise that an integration suite
would cover them. It does — 93% — and CI now measures those two modules with
their own floor, so the promise is enforced rather than remembered. The fast
gate has **no exclusions at all** and still reports 81%.

### End-to-end — `tests/e2e/`

`docker compose up`, a scripted scenario, wait, assert. The only test that would
catch a broken compose file, a wrong environment variable, a service that cannot
reach another, or an image that builds but does not run.

It also asserts **zero `ERROR` log lines across every service** during a clean
run. An error nobody looked at is a defect even when every assertion passes.

### The latency budget — `tests/perf/` and `docs/latency-budget.md`

"Every second matters" with a number attached, at 500 aircraft:

| | Median | p95 | Budget |
| --- | --- | --- | --- |
| Full cycle: 500 reports filtered + one conflict scan | 204 ms | **251 ms** | 500 ms |

Roughly a factor of four of headroom before the system stops keeping up with
real time. The document and the test read the same constants — a test asserts
they match, so the budget cannot drift into decoration.

**Measured: the compute path. Not measured: Kafka, Postgres, Redis.** Their
latency belongs to the deployment, and folding it in would produce a figure that
moves for reasons unrelated to any change under review. That split is stated
rather than glossed.

## The measurement that changed the code

The scan initially missed its budget at 267 ms. Before loosening the budget, I
measured what the spatial grid was actually doing:

```
aircraft:            490
picture radius:      124 NM
interaction radius:  105 NM   (grid cell size)
exhaustive pairs:    119,805
grid pairs tested:   110,737
reduction:           7.6%
```

**The grid barely helps.** The interaction radius — how far apart two aircraft
can be and still converge inside the lookahead — is comparable to the entire
sector, so it barely partitions anything.

What does help is a fact about air traffic rather than about code: **aircraft
are separated by flight level far more often than by distance.** An exact
vertical rejection before the trigonometry — if the closest they could possibly
come vertically still exceeds 1000 ft, no horizontal geometry can matter — took
the cycle p95 from 283 ms to 251 ms for four floating-point operations.

The grid stayed, because it earns its place toward the 300 NM upper limit the
projection allows. But the measurement is in the document, so nobody has to take
its value on faith.

## Where this is weak

- **The stream is polling with push.** Bounded by the interval, not event-driven.
- **Perf excludes transport and storage**, and there is still no measurement of a
  deployed system.
- **The conformance service does not shard.** Every instance would hold the whole
  picture and duplicate every alert, so it cannot be scaled out the way the
  tracker can.
- **Consumer-group rebalance is tested for assignment, not for behaviour under
  a mid-stream rebalance.** A consumer joining while messages are in flight is
  the harder case and is untested.
- **The perf job is `continue-on-error`.** A budget that fails the build on a
  noisy shared runner teaches people to ignore the job; the numbers are still
  printed. That is a deliberate trade and it does mean a regression could land
  without blocking.

## Questions a reviewer might ask

**"Why does the API poll Redis instead of consuming Kafka for the stream?"**
Because the API deliberately never consumes Kafka — that is ADR 0004 — and
reading the read model the tracker already maintains keeps a display refresh off
the pipeline that is estimating the picture. The cost is stated: latency bounded
by the interval. The upgrade path, if it were ever needed, is a Kafka consumer
feeding the same broadcaster.

**"Isn't fuzzing the live spec instead of the committed one a loophole?"**
It would be, if the two could differ. `scripts/contracts.py --check` fails the
build when they do, so fuzzing the live spec *is* fuzzing the committed one.

**"Why is the perf job allowed to fail?"**
Because a shared CI runner's timing is noisy, and a red build people learn to
ignore is worse than an informational one they read. The budget still has a
number, the number is enforced locally, and the document explains what it means.
If this ran on dedicated hardware it should block.

## Next

M5 — DevSecOps and operations: security scanning, SBOM, image publishing to
GHCR, OpenTelemetry tracing across Kafka, Prometheus metrics, and Kubernetes
manifests with a `kind` smoke test in CI.
