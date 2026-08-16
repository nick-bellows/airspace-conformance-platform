# Interview brief

Notes for talking about this project. Not part of the system.

---

## The sixty-second version

> Four Python microservices connected by Kafka. A simulator emits noisy aircraft
> position reports; a tracker turns them into smoothed tracks with a Kalman
> filter; a conformance service predicts where each aircraft will be and raises
> advisory alerts when two are heading for a loss of separation. A FastAPI
> service serves a live plan-view display over a WebSocket.
>
> The part I'd point at first is the evaluation. The conflict detector is scored
> against ground truth from the simulator that no part of the pipeline can see,
> so the measurement isn't circular even though the traffic is synthetic. It
> catches every real loss of separation with a median of four minutes' warning —
> and its precision is 0.57, which isn't good enough, and the README says so
> above the good numbers.

If they only remember one thing, make it the last sentence.

---

## Walking through it

Roughly fifteen minutes if you let them interrupt, which you should.

**1. Why these four services** — `README.md` architecture diagram. Reports are
keyed by aircraft address, so Kafka orders everything for one aircraft while
processing different aircraft in parallel. That's the only ordering guarantee the
tracker needs, and it's the one that costs nothing. It's also why the tracker
scales horizontally and the conformance service doesn't: the tracker shards by
partition, the conformance service holds the whole picture.

**2. The filter, and why it's deliberately weak** — a constant-velocity Kalman
filter lags during turns. That lag *is* the manoeuvre signal: it's published
downstream and the conformance monitor thresholds on it. Upgrading to a
constant-turn model would smooth away the thing the system exists to notice.
This usually gets a reaction.

**3. The ML, and why it's a residual** — the model predicts the *correction* to
dead reckoning, not the position. So a broken, missing, or NaN-producing model
degrades to physics rather than to nonsense, and that's executed in CI with the
dependency genuinely uninstalled. The linear baseline ships alongside, and on
shifted traffic the neural net's advantage isn't statistically distinguishable
from it — a scenario-clustered bootstrap, because consecutive samples from one
flight are near-duplicates.

**4. The tests, one story each** — property tests found two geodesy bugs on their
first run; an architecture test caught me importing one service from another;
schemathesis found my own OpenAPI spec lying twice; a real broker found a
`ContextVar` bug no fake could.

**5. What's wrong with it** — `limitations.md`. Have this ready rather than
waiting to be asked.

---

## Questions, with answers

### On the data

**"You trained and tested on data your own simulator produced. Isn't that
circular?"**
For the ML, partly — and the model card says so. For the conflict detector, no:
it consumes only the noisy observation stream, and ground truth comes from
noiseless simulator state nothing in the pipeline sees. It's a real algorithm on
degraded input scored against what actually happened. What's invented is the
traffic, not the measurement.

**"Why not use real ADS-B? It's freely available."**
Because ground truth is the whole point. With real data I'd have no idea which
predicted conflicts were real, so the one non-circular number in the repository
would disappear. It's [ADR 0002](adr/0002-synthetic-traffic-only.md), and the
cost is written down: the manoeuvre distribution is invented, the sensor model is
a plausible guess, and the false-alerts-per-hour figure isn't comparable to an
operational rate.

**"So what would the numbers be on real traffic?"**
Unknown, and I won't guess. `limitations.md` says the size of that gap is
unmeasured. What would close it is a local ADS-B capture, retraining, and
reporting both numbers side by side.

### On the ML

**"Your model barely beats a linear one."**
Correct, and that's the reported result. It halves turning error against dead
reckoning; against ridge regression on shifted traffic the confidence interval
spans zero. Shipping the linear model would be defensible and the model card says
so. Predetermining that the neural net had to win would have been the actual
failure.

**"Why no LLM anywhere?"**
[ADR 0008](adr/0008-no-generative-model-in-the-safety-path.md). A
non-deterministic generative model in a decision loop that produces safety
advisories is the wrong tool — you can't bound its failure mode, and the residual
design exists precisely so failure is bounded. I used one heavily to *build* it,
which is a different question, and that's written up separately.

**"Precision of 0.57 seems bad."**
It is. The cause is thresholding a point estimate — a predicted 4.9 NM miss
alerts, 5.1 NM doesn't, and the velocity estimate carries enough noise to move
the answer across that line. The fix is probabilistic detection using the
covariance the filter already maintains, and it's the one technical thing in
`future-work.md` I'd build next.

### On the engineering

**"Why Kafka rather than HTTP between services?"**
[ADR 0001](adr/0001-event-streaming-over-request-response.md). HTTP makes the
producer responsible for knowing every consumer, loses ordering on retry, and has
no answer for a consumer that's temporarily down except dropping data. The cost
is a broker to run and understand, and consumers that must be idempotent — which
is [ADR 0005](adr/0005-at-least-once-delivery-and-idempotent-consumers.md).

**"How do you know at-least-once actually works?"**
An integration test restarts a consumer mid-stream against a real broker and
asserts nothing is lost and at most one message replays. I wrote that test wrong
first — it asserted at-most-once — and it failed correctly, which is how I found
out the system was right and the test wasn't.

**"What happens when you scale the tracker?"**
Kafka assigns partitions across the consumer group and each aircraft stays with
one replica. That was broken until recently in a way worth describing: the
tracker kept every filter it had created, so when a partition moved, the old
replica kept ageing those aircraft and thirty seconds later published TERMINATED
and deleted them from Redis — while the new owner was actively maintaining the
same track. An aircraft would vanish from the display because someone added a
replica. It was invisible to 500 passing tests because every one ran a single
consumer. [ADR 0011](adr/0011-partition-ownership-is-state.md).

**"What's your test strategy?"**
Five levels, each catching something the others can't: unit and property tests
for logic and invariants; contract tests for spec drift and schema
compatibility; integration on real Redpanda/Postgres/Redis via testcontainers;
end-to-end on the whole compose stack; and a latency budget. The argument for
having levels is that each one has actually caught the class of bug it was built
for — there's a list.

**"Is the CI green?"**
Yes — thirteen jobs, and it took four attempts. The more interesting answer is
that it had never run at all for six milestones, because the repository had no
remote. Three review rounds found eighteen defects in that unexecuted config,
and the first real runs found five more that nothing local could have shown: a
dependency closure I guessed twice instead of deriving, a scenario fingerprint
that differed between MSVC and glibc because `sin`/`cos` disagree in the last
ULP, three tests depending on a clock they didn't own, a wall-clock budget
asserted on a shared runner, and CPU requests that wouldn't fit a two-core
node.

**"Why is `create_app` 180 lines?"**
Because it's a flat registry of seven thin route handlers and splitting it would
scatter one readable screen across four files. mccabe charges nested FastAPI
handlers to the enclosing factory, which is where the complexity number comes
from. I've had two reviewers agree; if a third disagreed I'd want their argument
rather than the metric.

### On process

**"How did you use AI to build this?"**
Heavily, with Claude Code, and the guardrail that mattered was refusing to let
prose stand unverified. The dominant failure mode wasn't broken code — types and
tests catch that — it was confident, specific, false claims in comments and
docs. A docstring asserted a projection was accurate to 0.1%; measuring it gave
1.3%. `ai-assisted-development.md` lists what it got wrong.

**"What would you do differently?"**
Push to a remote on day one. Six milestones of CI that had never executed
produced twenty-three defects between review and the first real runs, and most
of them a single early run would have surfaced. I'd been treating a green local
gate as evidence about a pipeline it says nothing about — the gate runs on my
laptop, with every extra installed, on one operating system, with one CPU
count.

**"What's this project's biggest weakness?"**
That it's measured entirely against a simulator I also wrote. Everything
downstream inherits that.

### On the domain

**"Have you worked in air traffic before?"**
No. The domain was chosen because it makes the engineering legible — why ordering
matters per aircraft, why a stale track is worse than no track, why latency has a
budget, why a false alert is expensive. Those are abstract in most demo projects
and concrete here. `safety-notes.md` states the boundary explicitly: advisory
only, uncertified, never for actual air traffic control.

**"Where did the 5 NM / 1000 ft come from?"**
En-route separation standards. Real airspace varies them by class, altitude, and
surveillance type, which the config makes overridable and the docs say plainly.

---

## What I'd change at real scale

- **Probabilistic conflict detection**, for the precision problem.
- **Sector-partition the conformance service** so it can run more than one
  replica. It holds the whole picture today.
- **A dead-letter topic** — malformed messages are logged, counted, and dropped.
- **Per-service images.** One image carries every service's dependencies,
  including PyTorch in the API. Fine at this size, wrong at any other.
- **Real data association.** Reports carry aircraft identifiers; real
  surveillance doesn't, and that's the genuinely hard part of tracking.

---

## Things not to do in the room

- Don't lead with the recall of 1.00. Lead with what's weak; the good numbers
  survive scrutiny better when you got there first.
- Don't oversell the green pipeline. The interesting part is that it was
  unexecuted for six milestones and what that cost.
- Don't defend `create_app` for more than a sentence.
- Don't say "production-ready". It isn't, deliberately, and
  [`future-work.md`](future-work.md) says exactly where the line is.
