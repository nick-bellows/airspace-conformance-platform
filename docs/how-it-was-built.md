# How it was built

The overview. Each milestone also has a full walkthrough in
[`walkthroughs/`](walkthroughs/) — this is the page to read first, and those are
where to go for detail on any one step.

The through-line is not the architecture — that is in the [ADRs](adr/). It is
that **every level of testing found the class of defect it was designed to find,
and prose was the least reliable artefact in the repository.**

---

## The shape of the work

| | What landed | The decision worth arguing about |
| --- | --- | --- |
| **[M0](walkthroughs/m0-scaffold.md)** | Contracts, geodesy, config, quality gate, dev stack | Kafka topics between services rather than HTTP calls ([0001](adr/0001-event-streaming-over-request-response.md)); synthetic traffic only ([0002](adr/0002-synthetic-traffic-only.md)) |
| **[M1](walkthroughs/m1-walking-skeleton.md)** | Traffic flowing feed → Kafka → tracker → Postgres/Redis → API, with a live display | The tracker writes and the API reads one storage layer ([0004](adr/0004-shared-read-model-between-tracker-and-api.md)) |
| **[M2](walkthroughs/m2-detection.md)** | Manoeuvring flight model, Kalman filter, separation monitor, alert lifecycle, and the conflict-detection evaluation | A constant-velocity filter chosen *for* its weakness ([0006](adr/0006-constant-velocity-filter.md)); at-least-once with idempotent consumers ([0005](adr/0005-at-least-once-delivery-and-idempotent-consumers.md)) |
| **[M3](walkthroughs/m3-trajectory-prediction.md)** | Baselines, ridge and PyTorch residual models, distribution-shift evaluation, conformance monitoring | The model learns the *correction to physics*, not the trajectory ([0007](adr/0007-residual-learning-over-direct-prediction.md)); no generative model in the safety path ([0008](adr/0008-no-generative-model-in-the-safety-path.md)) |
| **[M4](walkthroughs/m4-test-pyramid.md)** | WebSocket streaming, contract/integration/e2e suites, a measured latency budget | One reader fans out to many viewers ([0009](adr/0009-one-reader-fans-out-to-many-viewers.md)) |
| **[M5](walkthroughs/m5-devsecops.md)** | Metrics, tracing across Kafka, Grafana, Kubernetes, security scanning, GHCR publish | Observability degrades to no-ops rather than blocking startup ([0010](adr/0010-observability-degrades-rather-than-blocks.md)); partition ownership is state ([0011](adr/0011-partition-ownership-is-state.md)) |

---

## The two ideas the project is actually about

**A filter chosen for its weakness.** A constant-velocity Kalman filter lags
during turns. That lag — the *innovation*, how far the aircraft was from where
physics said it would be — is published downstream and becomes the manoeuvre
signal the conformance monitor thresholds on. The obvious upgrade to a
constant-turn model would smooth away the exact quantity the system is built to
notice.

**A model that learns the correction, not the answer.** The trajectory predictor
outputs a residual on top of dead reckoning. The failure mode is therefore
bounded: a broken, missing, or NaN-producing model degrades to physics rather
than to nonsense, and that promise is executed in CI rather than asserted. The
linear baseline ships alongside the neural one, and the model card reports that
on shifted traffic **their difference is not statistically distinguishable** —
which is the honest reading of a scenario-clustered bootstrap, and the reason
shipping the linear model would be defensible.

---

## Defects worth reading about

Each was found by the level of testing built for it. That is the argument for
having levels at all.

**Property tests found two geodesy bugs on their first run.** `normalize_bearing`
returned exactly `360.0` for tiny negative inputs, which would have failed
contract validation at runtime; and the local projection reported ~21,000 NM
instead of ~12 NM for a pair straddling the antimeridian, which would have hidden
a conflict entirely. Hypothesis found both; no example-based test would have.

**An architecture fitness test caught its own author.** It reads the import graph
out of the source and fails if one service imports another. Within a day of being
written it refused a build where the API imported the tracker's storage module.
The fix was to lift storage into its own layer.

**A scenario fingerprint test caught a reordered random draw.** A refactor moved
one `rng` call, which silently changed every generated scenario — and would have
invalidated the committed conflict-detection report without changing a single
number in it. The test failed on the first run after the refactor.

**Fuzzing found the OpenAPI spec lying, twice.** Schemathesis discovered an
undocumented 404 and a `kind` filter declared nullable, so the spec claimed
`?kind=null` was valid while the server returned 422. A query parameter cannot
meaningfully be null; it became a repeated parameter, which is both honest and
more useful.

**A real broker found a bug no fake could.** `ContextVar.reset()` may only be
called in the context that created the token, and an async generator can resume
in a different one. Breaking out of a consumer loop raised `ValueError` from
inside a `finally`, masking whatever the caller was doing. Saving and restoring
*by value* fixes it.

**My own at-least-once test asserted at-most-once, and failed correctly.** It
required a restarted consumer to see each message exactly once. The consumer
stopped after taking a message and before committing its offset, so the message
arrived again — which is the guarantee working. The system was right and the test
was wrong.

**A consumer-group rebalance could delete a live aircraft.** The tracker kept
every Kalman filter it had ever created. When Kafka moved a partition, the old
replica kept ageing those aircraft and thirty seconds later published
`TERMINATED` and deleted them from Redis — while the new owner was actively
maintaining the same track. An aircraft would vanish from the display because
somebody added a replica. It was invisible to 500-odd passing tests, because
every one of them ran a single consumer. [ADR 0011](adr/0011-partition-ownership-is-state.md)
has the fix and the two follow-on defects found *in* the fix.

---

## What went wrong that was not code

**Plausible-but-wrong documentation was the dominant failure mode.** Not broken
logic — the type checker and tests catch that — but confident, specific, false
claims in prose. A docstring asserted the flat-earth projection was accurate "to
under 0.1% within 100 NM"; measuring it gave **1.3%** for the quantity the
detector actually computes. The corrected figures are now a table pinned by a
test. The defence is to make claims executable, not to review them harder.

**The evaluation measured the wrong thing three times before it measured the
right one.** First the overall median was dominated by straight-and-level flight,
where dead reckoning is accurate to tens of metres. Then stratifying on the
*noisy filtered* turn rate put three quarters of steady cruise into the
"manoeuvring" bucket. Then merging turns with climbs buried 907 hard samples
under 3,121 easy ones. Each was caught by looking at the sample counts and asking
whether they were plausible.

**A documented gap was nearly preferred to fixing the gap.** The first version of
the Prometheus config explained at length why the three Kafka workers could not
be scraped, and said so honestly rather than papering over it. Writing that
paragraph made it obvious the honest answer was a background exposition server,
not a paragraph. Prose that argues well for its own constraints is a smell.

**A CI step that executed nothing passed for a whole milestone.** `docker run`
without `-i` leaves stdin closed, so a heredoc never reaches `python -`, which
runs an empty program and exits 0. It was written from a local command that did
work and was never watched to fail. The rule since: **do not ship a check without
inverting it once and confirming it goes red.**

**Unexecuted CI is not CI.** This repository had no remote for its first six
milestones, so the pipeline was configured code nobody had run. Three rounds of
external review found eighteen defects in it — a nonexistent action reference, an
image that failed its own vulnerability scan, a publish job that shipped a
different build than the one tested, a Kubernetes redeploy that deployed nothing
— and each round found defects in the previous round's fixes. All are fixed and
verified locally, which is precisely the phrase that produced the list.

---

## How AI was used

Heavily, with Claude Code, and separately from the question of whether a
generative model belongs *inside* the software — which it does not
([ADR 0008](adr/0008-no-generative-model-in-the-safety-path.md)). The distinction
is where review happens: AI-written code is read, typed, and tested before it
runs; an AI component inside a system acts before anyone can review it.

The guardrail that mattered was refusing to let prose stand unverified. Every
example in the section above is one it caught. Details in
[`ai-assisted-development.md`](ai-assisted-development.md).
