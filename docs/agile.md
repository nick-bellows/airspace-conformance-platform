# Planning and delivery

One page, deliberately. A solo project cannot honestly produce a Program
Increment's worth of ceremony, and inventing standup notes for a team of one
would be theatre. What follows is the part of the practice that actually shaped
the work: objectives with acceptance criteria, a definition of done that was
enforced rather than aspired to, and a risk log whose entries are real.

---

## PI objectives

Six iterations, one per milestone. Each has a business-value statement because
"why would anyone fund this" is the question objectives exist to answer.

| # | Objective | Value if met | Met? |
| --- | --- | --- | --- |
| 1 | A vertical slice from sensor to display, with nothing stubbed | Proves the architecture end to end before any depth is added, so no later work is built on an unproven path | ✅ M1 |
| 2 | Detect a predicted loss of separation and measure how well | Turns "it processes data" into "it does the job, this well" — the first defensible number | ✅ M2 |
| 3 | Beat physics at trajectory prediction, or report that it does not | Establishes whether ML earns its place, with the baseline shipped alongside | ✅ M3 — beats it while turning, adds nothing in cruise |
| 4 | Test at every level the system claims to work at | Distinguishes "the tests pass" from "the guarantees hold" | ✅ M4 |
| 5 | Make it operable, scannable, and deployable | Everything an on-call engineer needs before a system is anyone's responsibility | ⚠️ M5 — built, pipeline never executed |
| 6 | Make the work legible to someone who has ten minutes | An unread system demonstrates nothing | ▶ M6 — in progress |

**Objective 5 is the honest one.** It is marked incomplete rather than done,
because its evidence would be a green pipeline and there has never been one. The
work exists; the proof does not.

---

## Stories, with acceptance criteria

A representative slice rather than the full backlog — enough to show the shape
without padding.

**As an air traffic analyst, I need a predicted loss of separation flagged before
it happens, so I have time to act.**
- Given two aircraft on converging tracks, when predicted separation breaches
  5 NM laterally *and* 1000 ft vertically within the lookahead, an alert is
  raised naming both aircraft — ✅ `tests/e2e/test_pipeline.py`
- Given a pair laterally close but 4,000 ft apart, no alert is raised — ✅ same
- Recall, precision, and median lead time are measured against simulator ground
  truth the detector never sees, and published — ✅ `eval/results/conflict_detection.md`
- The alert states its predicted separation and time to closest approach — ✅

**As an operator, I need alerts that do not flap, so I can trust them.**
- An alert transitions NEW → SUSTAINED → CLEARED, never oscillating on one
  noisy update — ✅ `tests/unit/test_alerts.py`
- Clearing requires the condition to be absent for longer than raising requires
  it to be present (asymmetric hysteresis) — ✅

**As an on-call engineer, I need to know whether the system is keeping up.**
- Consumer lag, per service and partition, is exported and dashboarded — ✅
- A report's journey through the filter is measurable against the documented
  budget — ✅ `acp_report_filter_seconds`, 1 ms p95
- The dashboard is provisioned, not clicked in — ✅

**As a maintainer, I need a broken model to degrade rather than break.**
- A missing, corrupt, or NaN-producing model falls back to dead reckoning and
  says so in the alert's reason codes — ✅ `tests/unit/test_ml.py`
- The fallback is exercised in CI with the dependency genuinely absent — ✅
  `degradation` job

---

## Definition of done

Applied to every milestone, and the reason several took longer than planned.

1. `scripts/run_checks.ps1` green: ruff, ruff format, mypy strict, tests at ≥80%
   coverage with no exclusions, contract drift clean.
2. Any published number has a committed runner, reproducible from a recorded
   seed, stamped with component versions.
3. Any non-obvious decision has an ADR naming the alternatives and why they lost.
4. Any new limitation is written into `limitations.md` in the same change.
5. A walkthrough explains what was built and where it is weak.
6. **No claim in prose that nothing checks.** Added after M2, when a docstring
   was found asserting an accuracy figure that was wrong by an order of
   magnitude.
7. **No check shipped without watching it fail once.** Added after M5, when a CI
   step was found to have executed nothing for an entire milestone.

Rules 6 and 7 both exist because the project broke them first. That is the
honest version of a definition of done: it grows from the defects it failed to
prevent.

---

## Risk log (ROAM)

**Resolved**

| Risk | How it was resolved |
| --- | --- |
| Synthetic data makes every metric circular | The conflict detector is scored against ground truth it never sees; only the *traffic* is invented, not the *measurement*. Argued in [ADR 0002](adr/0002-synthetic-traffic-only.md) before any number was published |
| The ML component might not beat physics | Baselines shipped alongside and the honest result published: it halves turning error and adds nothing in cruise. Predetermining the answer was the actual risk |
| Kafka + testcontainers flakiness in CI | Redpanda single-container, readiness probed via the admin API rather than by sleeping |

**Owned** — accepted, with a named mitigation

| Risk | Mitigation |
| --- | --- |
| The pipeline has never executed, so its guarantees are unverified | Marked incomplete rather than done; every CI claim in the docs is qualified. Resolves on the first green run |
| A rolling upgrade combines migration, pod rollout, and Kafka rebalance — three paths each recently found broken | Each fixed and unit-tested independently; the combination is the top item in [`future-work.md`](future-work.md) and is stated as unproven |
| Domain resemblance could be mistaken for a real ATC system | [`safety-notes.md`](safety-notes.md) states the boundary; no FAA or employer association anywhere |

**Accepted** — no mitigation, and that is the decision

| Risk | Why accepted |
| --- | --- |
| Conflict-detection precision is 0.57 | Published prominently rather than buried. The fix is probabilistic detection, which is scoped as future work |
| Track filter state is lost on partition reassignment | Costs seconds of convergence; the alternative is an inter-replica protocol and a new consistency problem |
| No real-data validation | Would remove the one non-circular measurement in the repository ([ADR 0002](adr/0002-synthetic-traffic-only.md)) |

**Mitigated** — reduced, not eliminated

| Risk | What reduced it |
| --- | --- |
| Documentation drifting from behaviour | Fitness tests assert the dashboard, manifests, scrape config, and local gate agree with the code — `tests/unit/test_deployment.py` |
| A schema change silently breaking a consumer | Committed JSON Schemas, drift-gated, plus a backward-compatibility diff against git |

---

## What was learned about the estimates

Every milestone that slipped, slipped for the same reason: **measuring something
revealed it was measuring the wrong thing.** M3 was re-planned three times
because the evaluation stratification kept hiding the effect it was meant to
show. M5 doubled in length because three review rounds found defects in code
nobody had executed.

The estimate that held was M1, the walking skeleton — the one milestone with no
number to publish.
