# Roadmap

Last verified: 2026-09-04

## Handoff snapshot

| Field | Current state |
| --- | --- |
| Lifecycle | `PORTFOLIO-READY` |
| Portfolio role | Distributed systems, event streaming, observability, test depth, and engineering-judgment evidence |
| Public presentation | Existing GitHub Pages system tour plus a real-simulator demo GIF |
| Data boundary | Synthetic surveillance scenarios with generated ground truth |
| Runtime boundary | No public hosted system and no cloud-deployment claim |

The project is intentionally finished as a portfolio piece. Its most valuable result is not feature breadth: it is the measured test pyramid, the explicit operational limits, and a set of published claims that were tested and withdrawn.

## Completed milestone - external review remediation (2026-09-04)

Two independent LLM reviews (Codex, Cursor) were run against `ff7d14f` with filesystem access. Both returned ADVANCE. Both found defects that three earlier review rounds and several self-audits had missed.

### Delivered

1. **A correctness defect in the core detector, fixed.** The detector solved for horizontal closest approach and evaluated the vertical standard at that instant, so it could miss a pair that passes inside the lateral standard while vertically clear and then converges vertically before separating. Now an interval-overlap test ([ADR 0014](docs/adr/0014-conflict-is-an-interval-not-an-instant.md)). Reproduced before it was fixed; three tests written first and watched fail, including a negative control.
2. **A state-lifecycle defect, fixed.** Explicit `TERMINATED` updates bypassed alert and conformance cleanup, which ran only on the timeout path. Both now release through one route.
3. **All six detector-dependent evaluations regenerated** under `acp-separation-v2`. Nominal precision 0.5735 to 0.5571; **shifted recall 0.867-0.933 to 0.9667, flat across every lookahead**, because the fix recovers real missed conflicts on manoeuvre-rich traffic.
4. **An argument withdrawn.** ADR 0013 gave two reasons for the 300 s default; the second (a shorter window costs recall) was an artefact of the defect. Withdrawn rather than quietly restated.
5. **The latency budget re-measured and a miss disclosed.** The conflict scan is 342 ms p95 against its 250 ms budget on named hardware. The previously published 204/251 figures are superseded, not explained -- their hardware was never recorded.
6. **Smaller findings, each with a guard watched to fail:** a broken `acp-sim` console script, a `.gitleaks.toml` comment claiming a test that did not exist, a missing `LICENSE`, retracted science still argued in the primary evaluation report, an unsupported "two days" claim, a false statement about CI job ordering, and three published counts that had drifted.

### The finding worth keeping

Nominal recall stayed at **1.00 for the entire life of the geometry defect** -- not because the detector was correct, but because the scenario generator never produced the geometry it was blind to. A metric is only as strong as the adversariality of the population it was measured on, and confidence-interval discipline does not repair a test set that never asks the hard question. Recorded in `limitations.md` next to the number.

## Completed milestone - 60-second reviewer route

Goal: reduce the existing Pages site to a clear first-minute path without building another runtime feature.

### Delivered

1. `docs/index.html` now opens with a four-stop, 60-second route through the real replay, code boundaries, broker integration proof, and negative result.
2. The route links directly to representative contracts, storage, conformance, API, integration, and end-to-end files.
3. Keyboard focus and reduced-motion behavior are explicit, while the replay remains a bounded static asset generated from the simulator.
4. `tests/unit/test_portfolio_site.py` guards tour anchors, direct evidence links, replay presence and size, accessibility controls, and claim-bearing metrics against the retained evaluation JSON.
5. The README points logged-out reviewers directly to the tour.

### Acceptance criteria

- A reviewer reaches working code or retained evaluation evidence in no more than two clicks from the landing page.
- The walkthrough states that the runtime is synthetic, advisory, locally/container validated, and not a deployed air-traffic system.
- The deterministic/probabilistic comparison includes the shifted-family result and does not market the nominal-only improvement.
- The static site continues to load without Kafka, PostgreSQL, Redis, or a public API.
- CI catches drift between claim-bearing evaluation summaries and committed results.

## Hosting decision

Keep GitHub Pages. Do not host the Kafka/PostgreSQL/Redis/Kubernetes stack. The repository already demonstrates packaging, contracts, local orchestration, CI, and operational boundaries; an internet-facing runtime would add authentication, TLS, rate limiting, durability, monitoring, and cost obligations without improving the core evidence.

Vercel, Replit, Render, and Railway are not justified for this project. The static tour is the interactive recruiter artifact. This preserves the explicit `no hosted public instance` decision in `docs/future-work.md`.

## Next engineering work, if resumed

No engineering feature is scheduled. Known scale and operations gaps remain in `docs/future-work.md`. Resume only when a target role makes one gap material and the work can be measured—for example a real two-consumer rebalance test or broker-inclusive latency study. Do not tune the detector again on the same scenario families.

Two additional prohibitions from the 2026-09-04 round:

- **Do not generate scenarios that specifically exercise the interval-overlap geometry and republish recall.** That is fitting the test set to a bug already fixed. If a vertical-crossing family is built, it is a new population measured on its own terms and reported separately.
- **Do not close the conflict-scan budget miss by raising the budget.** Either optimise the scan and show the measurement, or leave the miss disclosed. Moving the target to meet the result is the failure this repository documents other people making.

## Stop conditions

- No real ADS-B ingestion, maneuver recommendations, or production/safety claims.
- No decorative uncertainty visualization unsupported by the selected detector.
- No hosted infrastructure merely to add a cloud URL.
- No claim that Kubernetes manifests prove a live cluster or rolling upgrade.

## Verification before changing status

Run the repository quality workflow locally where possible, rebuild the static tour from retained assets, verify evaluation drift checks, inspect the logged-out Pages site, and keep local, CI, Compose, kind/Kubernetes, and cloud claims separate.
