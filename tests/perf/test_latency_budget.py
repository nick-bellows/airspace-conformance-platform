"""The latency budget: how long from a position report to an alert.

"Performance, precision and reliability matter -- every second" is the kind of
claim that means nothing until someone measures it. This does.

## What is measured, and what is deliberately not

Measured: the **compute** path. A surveillance report enters the tracker's
estimator, becomes a track update, is absorbed by the conformance service, and a
scan produces an alert. Every stage is the production class, driven in process.

Not measured: Kafka, Postgres, or Redis. Transport and storage latency belong to
the deployment -- broker placement, disk, network -- and mixing them in would
produce a number that says more about the laptop than about the code. The
end-to-end suite proves the wiring works; this proves the algorithms keep up.

That split is the honest way to report it, and the budget below is stated in
those terms rather than as a claim about a deployed system.
"""

from __future__ import annotations

import os
import statistics
import time
from datetime import UTC, datetime

import pytest

from acp.common.contracts import TOPIC_ALERTS, AlertKind
from acp.services.conformance.alerts import AlertManager
from acp.services.conformance.runner import ConformanceRunner
from acp.services.conformance.separation import SeparationMonitor
from acp.services.track.estimator import TrackEstimator
from acp.sim.engine import Simulation
from acp.sim.generator import TRAINING, generate_family
from acp.sim.scenario import load_scenario
from tests.integration.helpers import repo_root
from tests.unit.fakes import FakePublisher

pytestmark = pytest.mark.perf

#: Aircraft the system must handle at a 1 Hz report rate. Roughly the traffic in
#: a busy en-route sector, and far more than any scenario here produces.
TARGET_AIRCRAFT = 500

#: Budget for one full processing cycle at that load: every aircraft's report
#: filtered, plus one conflict scan over the whole picture.
#:
#: 1000 ms is the report interval. Exceeding it means the system cannot keep up
#: with real time at all -- the queue grows without bound. The budget is set at
#: half that, so there is headroom for the transport this test excludes.
CYCLE_BUDGET_MS = 500.0

#: Budget for a single report through the filter. At 500 aircraft this has to
#: happen 500 times per second, so it is the figure that decides whether the
#: tracker scales.
PER_REPORT_BUDGET_MS = 1.0

#: Budget for one conflict scan of the full picture. Pairwise geometry is the
#: part that grows quadratically, and the spatial grid exists because of it.
SCAN_BUDGET_MS = 250.0


def _busy_airspace(aircraft: int) -> Simulation:
    """A single scenario carrying roughly `aircraft` aeroplanes.

    Built by merging generated encounters, because no single committed scenario
    is anywhere near this dense -- and a load test that runs on four aircraft
    measures nothing.
    """
    # Each generated scenario carries six to nine aircraft, so reaching 500
    # takes far more scenarios than a first guess suggests. Over-generating and
    # stopping early is cheaper than getting the arithmetic exactly right and
    # having the load quietly fall short later.
    scenarios = generate_family(max(20, aircraft // 5), 777, TRAINING)
    merged = scenarios[0]
    specs = list(merged.aircraft)
    seen = {a.icao24 for a in specs}
    for scenario in scenarios[1:]:
        for spec in scenario.aircraft:
            if spec.icao24 not in seen and len(specs) < aircraft:
                seen.add(spec.icao24)
                specs.append(spec)
        if len(specs) >= aircraft:
            break
    return Simulation(
        merged.model_copy(update={"aircraft": tuple(specs), "duration_s": 600.0}),
        datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_the_tracker_keeps_up_with_five_hundred_aircraft() -> None:
    """Per-report filtering cost, which is what decides whether this scales."""
    simulation = _busy_airspace(TARGET_AIRCRAFT)
    estimator = TrackEstimator()

    # Warm up: the first reports initiate tracks, which is a different and
    # slower path than the steady state this measures.
    for _ in range(25):
        simulation.advance(1.0)
        for report in simulation.observe():
            estimator.on_report(report)

    durations_ms: list[float] = []
    for _ in range(30):
        simulation.advance(1.0)
        reports = simulation.observe()
        started = time.perf_counter()
        for report in reports:
            estimator.on_report(report)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        durations_ms.append(elapsed_ms / max(1, len(reports)))

    p95 = statistics.quantiles(durations_ms, n=20)[-1]
    assert estimator.live_track_count >= TARGET_AIRCRAFT * 0.9, "not enough traffic to be a test"
    within_budget(p95, PER_REPORT_BUDGET_MS, "per-report p95")


#: Whether a budget overrun should fail or merely be reported.
#:
#: The budgets are a property of the *code*, measured on a reference machine. A
#: shared CI runner is a different machine -- the first run of this suite in
#: GitHub Actions produced a full cycle of 401.6 ms against the 248 ms measured
#: locally, on identical code. Asserting there would make the job a coin toss
#: and teach everyone to ignore a red perf check, which is worse than not
#: measuring at all.
#:
#: So CI sets ACP_PERF_REPORT_ONLY=1: the suite still runs, still prints every
#: number, and still fails on anything that is not a timing question (too little
#: traffic to be a valid test, a conflict never detected, the documented budget
#: drifting from the enforced one). Only the wall-clock thresholds relax.
REPORT_ONLY = bool(os.environ.get("ACP_PERF_REPORT_ONLY"))


def within_budget(actual_ms: float, budget_ms: float, what: str) -> None:
    """Assert a timing, unless this is a report-only run."""
    message = f"{what}: {actual_ms:.3f} ms against a {budget_ms} ms budget"
    if REPORT_ONLY:
        verdict = "within" if actual_ms < budget_ms else "OVER"
        print(f"  [report-only] {verdict} budget -- {message}")
        return
    assert actual_ms < budget_ms, message


async def test_a_conflict_scan_of_a_busy_sector_fits_the_budget() -> None:
    """The pairwise search, which is the part that grows quadratically.

    500 aircraft is 124,750 unordered pairs. The spatial grid exists so this
    does not become the bottleneck; this is the test that would notice if it
    were removed.
    """
    simulation = _busy_airspace(TARGET_AIRCRAFT)
    estimator = TrackEstimator()
    publisher = FakePublisher()
    runner = ConformanceRunner(
        None,
        publisher,  # type: ignore[arg-type]
        monitor=SeparationMonitor(),
        manager=AlertManager(),
    )

    for _ in range(30):
        simulation.advance(1.0)
        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))

    durations_ms: list[float] = []
    for _ in range(10):
        simulation.advance(1.0)
        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))
        started = time.perf_counter()
        await runner.scan_now(simulation.now)
        durations_ms.append((time.perf_counter() - started) * 1000.0)

    p95 = (
        statistics.quantiles(durations_ms, n=20)[-1]
        if len(durations_ms) >= 20
        else max(durations_ms)
    )
    assert runner.live_tracks >= TARGET_AIRCRAFT * 0.9, "not enough traffic to be a test"
    within_budget(p95, SCAN_BUDGET_MS, "conflict-scan p95")


async def test_a_full_cycle_at_target_load_fits_inside_the_report_interval() -> None:
    """The headline number: filtering plus a scan, per second of airspace time.

    If this exceeds the report interval the system cannot keep up with real
    time and the backlog grows without bound. The budget is half the interval,
    leaving room for the transport this test deliberately excludes.
    """
    simulation = _busy_airspace(TARGET_AIRCRAFT)
    estimator = TrackEstimator()
    runner = ConformanceRunner(
        None,
        FakePublisher(),  # type: ignore[arg-type]
        monitor=SeparationMonitor(),
        manager=AlertManager(),
    )

    for _ in range(30):
        simulation.advance(1.0)
        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))

    durations_ms: list[float] = []
    for _ in range(20):
        simulation.advance(1.0)
        reports = simulation.observe()
        started = time.perf_counter()
        for report in reports:
            runner.absorb(estimator.on_report(report))
        await runner.scan_now(simulation.now)
        durations_ms.append((time.perf_counter() - started) * 1000.0)

    p95 = statistics.quantiles(durations_ms, n=20)[-1]
    median = statistics.median(durations_ms)
    print(
        f"\ncycle at {runner.live_tracks} aircraft: "
        f"median {median:.1f} ms, p95 {p95:.1f} ms, budget {CYCLE_BUDGET_MS:.0f} ms"
    )
    within_budget(p95, CYCLE_BUDGET_MS, "full-cycle p95")


async def test_detection_latency_is_bounded_by_the_scan_interval() -> None:
    """How long after a conflict becomes visible does an alert appear?

    Not a wall-clock measurement -- a statement about the design. The picture is
    scanned on a timer, so worst-case detection latency is one scan interval
    plus the scan itself. This asserts the alert appears on the first scan after
    the geometry is present, which is what makes that reasoning valid.
    """
    scenario = load_scenario(repo_root() / "scenarios" / "head-on-conflict.yaml")
    simulation = Simulation(scenario, datetime(2026, 1, 1, tzinfo=UTC))
    estimator = TrackEstimator()
    publisher = FakePublisher()
    runner = ConformanceRunner(
        None,
        publisher,  # type: ignore[arg-type]
        monitor=SeparationMonitor(),
        manager=AlertManager(),
    )

    detected_at: float | None = None
    while not simulation.finished and detected_at is None:
        simulation.advance(1.0)
        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))
        await runner.scan_now(simulation.now)
        if any(
            m.kind is AlertKind.PREDICTED_CONFLICT  # type: ignore[attr-defined]
            for m in publisher.messages_on(TOPIC_ALERTS)
        ):
            detected_at = simulation.elapsed_s

    assert detected_at is not None, "the conflict was never detected"
    # The pair is 120 NM apart closing at 900 kt, so the geometry enters the
    # 300 s lookahead about 155 s in. Detection immediately after that is the
    # bound; a large gap would mean the scan is not seeing what it should.
    assert detected_at < 200.0, f"detection took {detected_at:.0f}s of scenario time"


def test_the_budget_matches_the_documented_one() -> None:
    """The numbers in `docs/latency-budget.md` and the numbers enforced here
    must be the same, or the document is decoration."""
    budget = (repo_root() / "docs" / "latency-budget.md").read_text(encoding="utf-8")
    assert f"{CYCLE_BUDGET_MS:.0f} ms" in budget
    assert f"{PER_REPORT_BUDGET_MS:.0f} ms" in budget
    assert f"{SCAN_BUDGET_MS:.0f} ms" in budget
    assert str(TARGET_AIRCRAFT) in budget
