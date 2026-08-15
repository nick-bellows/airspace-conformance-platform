# Safety notes

Status: `draft` — the scope statement below is final; the metrics it refers to arrive at M2.

## What this system is

A demonstration of event-driven software engineering, using air traffic
conformance monitoring as the problem domain. It ingests simulated surveillance
reports, estimates aircraft state, and raises **advisory** alerts about
predicted losses of separation and unmodelled maneuvers.

## What this system is not

**This is not an air traffic control system and must never be used as one.**

Specifically, it is none of the following:

- **Certified.** No part of it was developed under DO-178C, ED-109A, or any
  other airborne or ground software assurance standard. There is no
  requirements-to-code traceability matrix, no structural coverage analysis at
  MC/DC, no tool qualification, and no independent verification.
- **Fed by real data.** Every position it has ever processed came from the
  simulator in `src/acp/sim/`. It has never been connected to ADS-B, radar,
  multilateration, or any operational feed.
- **Aware of intent.** It has no flight plans, no clearances, no controller
  inputs, and no airspace structure. It cannot distinguish an aircraft
  descending because it was told to from one descending because something is
  wrong. Every "non-conformance" it reports means only *the aircraft did not do
  what constant-velocity physics predicted*.
- **A resolution advisor.** It never proposes a heading, altitude, or speed. It
  reports geometry and stops.
- **Fault tolerant to the standard the domain requires.** There is no
  redundancy, no voting between dissimilar implementations, no fail-operational
  behaviour, and no formally bounded worst-case execution time.

## Why the boundary is stated this loudly

The separation standards it applies (5 NM lateral, 1000 ft vertical) are real
ones. The alert vocabulary borrows real terminology. That resemblance is
exactly what makes an explicit boundary necessary: a reader should never have
to guess whether this is a toy or a product. It is a portfolio project, and
saying so plainly costs nothing and prevents a genuine misunderstanding.

## Limits that affect the numbers

These are summarised here and treated in full in `docs/limitations.md` once
there are metrics to qualify:

1. Results are measured on synthetic traffic. They characterise the algorithms
   and the pipeline, not real-world performance.
2. Surveillance reports carry an aircraft identifier, so the tracker never has
   to solve data association — the hard part of real radar tracking is absent
   by construction.
3. The simulator's maneuver distribution is an invention. Real traffic is
   shaped by procedures, weather, and controller workload that it does not
   model.

## Attribution

No affiliation with, endorsement by, or data from the FAA, EUROCONTROL, any air
navigation service provider, or any employer is claimed or implied.
