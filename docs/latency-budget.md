# Latency budget

Status: `validated` against the compute path, on synthetic traffic. **Not
validated against a deployed system** — see the scope section.

"Performance, precision and reliability matter — every second" is the kind of
claim that means nothing until it has a number attached. This is the number, and
the boundaries around it.

---

## The budgets, and what enforces them

Every figure below is asserted by `tests/perf/test_latency_budget.py`. That
suite reads this document and fails if the two disagree, so the budget cannot
drift away from what is actually enforced.

| Stage | Budget | Why that figure |
| --- | --- | --- |
| One report through the filter | **1 ms** (p95) | At 500 aircraft this runs 500 times a second. It is what decides whether the tracker scales. |
| One conflict scan of the full picture | **250 ms** (p95) | Pairwise geometry grows quadratically. 500 aircraft is 124,750 unordered pairs. |
| A full cycle: 500 reports filtered plus one scan | **500 ms** (p95) | Half the 1 s report interval, leaving headroom for the transport this measurement excludes. |

**Target load: 500 aircraft at 1 Hz.** Roughly the traffic in a busy en-route
sector, and far more than any committed scenario produces — a load test that
runs on four aircraft measures nothing.

**Why 500 ms rather than 1000 ms.** The report interval is one second. Exceeding
it means the system cannot keep up with real time at all and the backlog grows
without bound. Budgeting half of it leaves room for Kafka, Postgres, and Redis,
which are excluded below.

---

## What is measured, and what deliberately is not

**Measured — the compute path.** A surveillance report enters the tracker's
Kalman filter, becomes a track update, is absorbed into the conformance
service's airspace picture, and a scan turns it into an alert. Every stage is
the production class, driven in process.

**Not measured — transport and storage.** Kafka, Postgres, and Redis are
excluded. Their latency is a property of the deployment: broker placement, disk,
network, contention. Including them would produce a figure that says more about
the machine the test ran on than about the code, and it would move for reasons
that have nothing to do with a change under review.

The end-to-end suite proves the wiring works. This proves the algorithms keep
up. **Neither is a claim about a deployed system**, and there is currently no
measurement of one.

---

## Detection latency

Distinct from throughput, and a property of the design rather than a
measurement.

The conformance service **scans the airspace picture on a timer** rather than on
each incoming update, because a conflict is a property of the *set* of aircraft
and re-running pairwise geometry per update would repeat near-identical work
hundreds of times a second. Worst-case detection latency is therefore:

```
one scan interval  +  one scan duration
= 1000 ms          +  <250 ms          = under 1.25 s
```

`test_detection_latency_is_bounded_by_the_scan_interval` asserts the alert
appears on the first scan after the geometry enters the lookahead window, which
is what makes that reasoning valid rather than merely plausible.

**The scan interval is the tuning knob.** `acp-conformance --scan-interval-s`
trades detection promptness against CPU. Halving it halves worst-case detection
latency and doubles the scan cost.

Against the five-minute conflict lookahead, a second of detection latency is
negligible — the alert still arrives minutes before separation is lost. The
median warning lead time in `eval/results/conflict_detection.md` is 249 s.

---

## Measured

Re-measured 2026-09-04 on an **AMD Ryzen 9 7900X (12C/24T), Windows 11,
CPython 3.13**, 500 aircraft, 20 cycles:

| | Median | p95 | Budget | |
| --- | --- | --- | --- | --- |
| Per report through the filter | — | 0.08 ms | 1 ms | within |
| Conflict scan of the whole picture | — | **342 ms** | 250 ms | **over** |
| Full cycle (500 reports filtered + one scan) | 363 ms | **406 ms** | 500 ms | within |

**The conflict-scan budget is not met, and has not been for some time.** This
document previously reported a 204 ms median and 251 ms p95 full cycle "on a
laptop CPU" with no CPU named and no date, measured at M4 on 2026-08-15. Those
figures do not reproduce on current code, on hardware that should comfortably
beat the original. They are superseded rather than explained: the original
hardware was never recorded, so the gap cannot be attributed.

The scan miss is **not** caused by the interval-overlap fix in
[ADR 0014](adr/0014-conflict-is-an-interval-not-an-instant.md), though that fix
is not free. Measured on the same machine, back to back:

| Detector | Conflict-scan p95 | Full-cycle p95 |
| --- | --- | --- |
| `acp-separation-v1` (before the fix) | 324 ms | 392 ms |
| `acp-separation-v2` (after) | 342 ms | 406 ms |

So the correct geometry costs about **5%**, and the budget was already being
missed by 30% without it. Solving two intervals is cheaper than the alternatives
considered in that ADR, and a scan that is fast and wrong is not a trade this
project makes.

**Why CI stays green anyway.** The `perf` job sets `ACP_PERF_REPORT_ONLY=1` and
is `continue-on-error`, because wall-clock assertions on a shared runner measure
the runner. That is a deliberate decision recorded in `limitations.md`, and its
cost is exactly this: a budget can be missed for weeks without the pipeline
saying so. **"All jobs green" must never be paraphrased as "all budgets met."**

The full cycle still fits its 500 ms budget against a 1000 ms report interval,
so the system keeps up with real time. The scan is the component that does
not fit the budget written for it.

## What actually made the scan fast, and what did not

**The spatial grid barely helps.** Measured directly at 500 aircraft in a 124 NM
sector: 119,805 unordered pairs, of which the grid eliminated **7.6%**. The
interaction radius is 105 NM — how far apart two aircraft can be and still
converge inside the lookahead — which is comparable to the entire airspace, so
the grid barely partitions anything. It earns its place only towards the 300 NM
upper limit the projection allows, and `test_the_grid_finds_the_same_conflicts_as_an_exhaustive_search`
is what stops it quietly changing the answer in the meantime.

**A vertical rejection helps considerably more**, and it was added because that
measurement showed where the time was going. Before the trigonometry, the pair
is rejected if the closest they could possibly come *vertically* within the
lookahead still exceeds 1000 ft. Four floating-point operations, exact rather
than heuristic, and it took the cycle p95 from 283 ms to 251 ms.

The reason it works is a fact about air traffic rather than about code:
**aircraft are separated by flight level far more often than by distance.** Most
pairs in a busy sector are vertically clear and always will be, so that is the
cheapest place to say no.

**Known limits:**

- **Beyond a few hundred aircraft in one picture**, the shared tangent-plane
  projection stops being accurate enough (see `docs/limitations.md`), so the
  right answer is a monitor per airspace sector rather than a faster scan. The
  geometry constraint binds before the performance one does.
- **The tracker is single-threaded per instance.** Scaling past one instance's
  throughput means more instances in the same consumer group, which works
  because reports are keyed by aircraft address. Untested above one instance.
- **The conformance service does not shard.** Every instance would hold the
  whole picture and duplicate every alert. Partitioning it by sector is real
  work and is not done.

---

## Reproducing

```powershell
python -m pytest tests/perf -v
```

Takes a couple of minutes: most of it is generating and filtering enough
traffic to be a real test. Runs in CI as its own job, informational at first —
a perf budget that fails the build on a noisy shared runner teaches people to
ignore it.
