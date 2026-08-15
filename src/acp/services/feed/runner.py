"""Feed service: play a scenario and publish what the sensor saw.

This is the system's edge. It owns the simulation clock and is the only place
that touches truth. Two publishing paths leave here and they are kept strictly
apart:

* ``surveillance.reports.v1`` -- noisy observations. Everything downstream reads
  this and nothing else.
* ``sim.truth.v1`` -- exact state, for the evaluation harness only.

If those two ever merged, every metric in the project would become worthless,
so the split is enforced by the topic names and stated in every relevant doc.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from acp.common.contracts import TOPIC_SIM_TRUTH, TOPIC_SURVEILLANCE_REPORTS
from acp.common.logging import get_logger, trace_id_var
from acp.common.messaging import MessagePublisher
from acp.sim.engine import Simulation
from acp.sim.scenario import Scenario

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FeedStats:
    """What one run produced. Returned so tests can assert on it."""

    steps: int
    reports_published: int
    truth_published: int


class FeedRunner:
    """Drives a :class:`Simulation` and publishes its output.

    ``realtime`` paces the run against the wall clock, which is what a demo
    needs. ``replay`` runs flat out, which is what tests and dataset generation
    need -- a 15-minute scenario finishes in about a second.
    """

    def __init__(
        self,
        scenario: Scenario,
        publisher: MessagePublisher,
        *,
        realtime: bool = True,
        publish_truth: bool = True,
    ) -> None:
        self._scenario = scenario
        self._publisher = publisher
        self._realtime = realtime
        self._publish_truth = publish_truth

    async def run(self, *, start_time: datetime | None = None) -> FeedStats:
        """Play the scenario to completion."""
        simulation = Simulation(self._scenario, start_time or datetime.now(UTC))
        interval = self._scenario.sensor.report_interval_s
        steps = reports = truths = 0

        _log.info(
            "starting scenario",
            extra={
                "scenario_id": self._scenario.scenario_id,
                "duration_s": self._scenario.duration_s,
                "aircraft": len(self._scenario.aircraft),
                "mode": "realtime" if self._realtime else "replay",
            },
        )

        while not simulation.finished:
            tick_started = asyncio.get_running_loop().time()
            simulation.advance(interval)
            steps += 1

            reports += await self._publish_reports(simulation)
            if self._publish_truth:
                truths += await self._publish_truth_states(simulation)

            if self._realtime:
                # Subtract the work already done so the feed does not drift
                # slower than real time under load. If a tick overruns, the
                # sleep is skipped rather than negative and the run catches up.
                elapsed = asyncio.get_running_loop().time() - tick_started
                await asyncio.sleep(max(0.0, interval - elapsed))

        stats = FeedStats(steps=steps, reports_published=reports, truth_published=truths)
        _log.info(
            "scenario complete",
            extra={
                "scenario_id": self._scenario.scenario_id,
                "steps": stats.steps,
                "reports": stats.reports_published,
            },
        )
        return stats

    async def _publish_reports(self, simulation: Simulation) -> int:
        count = 0
        for report in simulation.observe():
            # A fresh correlation id per report. Everything the pipeline does
            # because of this observation carries it, across three services.
            token = trace_id_var.set(uuid.uuid4().hex)
            try:
                await self._publisher.publish(
                    TOPIC_SURVEILLANCE_REPORTS, key=report.icao24, message=report
                )
                count += 1
            finally:
                trace_id_var.reset(token)
        return count

    async def _publish_truth_states(self, simulation: Simulation) -> int:
        count = 0
        for state in simulation.truth():
            await self._publisher.publish(TOPIC_SIM_TRUTH, key=state.icao24, message=state)
            count += 1
        return count
