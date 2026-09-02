# Roadmap

Last verified: 2026-09-02

## Handoff snapshot

| Field | Current state |
| --- | --- |
| Lifecycle | `PORTFOLIO-READY` |
| Portfolio role | Distributed systems, event streaming, observability, test depth, and engineering-judgment evidence |
| Public presentation | Existing GitHub Pages system tour plus a real-simulator demo GIF |
| Data boundary | Synthetic surveillance scenarios with generated ground truth |
| Runtime boundary | No public hosted system and no cloud-deployment claim |

The project is intentionally finished as a portfolio piece. Its most valuable result is not feature breadth: it is the measured test pyramid, the explicit operational limits, and a probabilistic-detector improvement that failed to replicate on shifted scenarios.

## Current milestone - 60-second reviewer route

Goal: reduce the existing Pages site to a clear first-minute path without building another runtime feature.

### Work

1. Add a prominent `Start the 60-second tour` route on `docs/index.html`.
2. Route the reviewer through exactly four evidence points: live synthetic scenario, event/service architecture, one end-to-end/failure-mode proof, and the non-replicating detector result.
3. Add direct code links for the message contract, idempotent consumer path, conformance rule, WebSocket fan-out, and representative test.
4. Keep the current demo GIF and committed replay synchronized with the real simulator. Do not create illustrative control-room imagery.
5. Verify mobile layout, keyboard focus, contrast, asset weight, and logged-out links.

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

## Stop conditions

- No real ADS-B ingestion, maneuver recommendations, or production/safety claims.
- No decorative uncertainty visualization unsupported by the selected detector.
- No hosted infrastructure merely to add a cloud URL.
- No claim that Kubernetes manifests prove a live cluster or rolling upgrade.

## Verification before changing status

Run the repository quality workflow locally where possible, rebuild the static tour from retained assets, verify evaluation drift checks, inspect the logged-out Pages site, and keep local, CI, Compose, kind/Kubernetes, and cloud claims separate.
