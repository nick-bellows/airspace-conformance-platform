# ADR 0002 — Synthetic traffic only, and no third-party flight data in the repository

Status: accepted · Date: 2026-08-15

## Context

The system needs aircraft trajectories to process. Two sources were considered.

**The OpenSky Network** publishes crowd-sourced ADS-B state vectors through a
REST API. Reviewed before deciding:

- Since March 2026 it requires OAuth2 client credentials; anonymous access is
  limited to 400 credits per day at 10-second resolution with no historical
  window. A free account raises this to 4000 credits per day at 5-second
  resolution with one hour of history — enough to capture a few hours of traffic
  over a bounding box.
- The data licence grants use for *non-profit research, non-profit education,
  internal commercial evaluation, or government purposes*. Redistribution is not
  granted, and use by a for-profit entity requires written permission.

**A simulator** written for this project generates trajectories from a
kinematic flight model driven by a seeded random number generator.

## Decision

Synthetic traffic is the only data source. No third-party flight data is
captured, committed, or redistributed by this repository.

## Consequences

**What this buys**

- *Ground truth.* The simulator knows each aircraft's exact position while the
  pipeline sees only noisy, dropout-afflicted reports. The conflict detector can
  therefore be scored against truth it never observed — real precision, recall,
  and warning lead time. **This is the strongest metric in the project and it
  only exists because the data is synthetic.** With real ADS-B there is no
  labelled set of conflicts to score against.
- *A reproducible build.* A seed yields byte-identical output, so CI is
  deterministic and an evaluation report can be regenerated years later.
- *No licence exposure.* Nothing in the repository is anyone else's data.
  This also satisfies the portfolio-wide rule against raw third-party data in
  version control.
- *Scenarios on demand.* Head-on conflicts, go-arounds, and emergency squawks
  can be produced deliberately. Waiting for one to occur on a live feed is not
  a test strategy.

**What it costs — stated plainly because it is the project's main weakness**

The trajectory prediction model is trained and evaluated on data produced by
this project's own simulator. It is therefore measured against the same physics
that generated it, and the reported error **is not evidence of real-world
accuracy**. Three things limit how much that matters, and none of them
eliminate it:

1. The model never observes the simulator's intent — waypoints, commanded
   altitudes, and the stochastic decision to turn or hold are hidden. It sees
   only noisy positions, so it cannot invert the generator; it can only learn
   maneuver statistics.
2. Evaluation splits are by *scenario family*, not at random: the model trains
   on one set of airport layouts, densities, wind fields, and maneuver mixes and
   is tested on a different set with disjoint seeds. That measures
   generalisation across distribution shift rather than memorisation.
3. Physics baselines are reported next to every model number, so a reader can
   see how much of the performance is the model and how much is dead reckoning.

The unmeasured quantity is the gap between simulated and real traffic. Closing
it would require a local capture under an OpenSky account, retraining, and a
separate report; that data would remain outside version control. This is
recorded in `docs/limitations.md` rather than left for a reader to discover.

**Rejected: commit a small real sample.** Would strengthen the ML claim, but
redistributing OpenSky data is outside the licence grant and puts third-party
data in Git.

**Rejected: real data locally, gitignored, with only metrics committed.**
Defensible, and the strongest option for credibility. Rejected for this build to
keep the pipeline reproducible end-to-end from a clean clone with no account,
no credentials, and no capture step. The trade is honesty about a known gap in
exchange for a build anyone can reproduce.

## References

- OpenSky REST API — https://openskynetwork.github.io/opensky-api/rest.html
- OpenSky terms of use — https://opensky-network.org/about/terms-of-use
