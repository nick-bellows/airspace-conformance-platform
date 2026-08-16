"""Track service: surveillance reports in, track updates out.

The loop is deliberately plain -- consume, estimate, persist, publish -- but two
things in it are worth reading closely.

**Batching.** Updates accumulate in memory and flush either when the batch is
full or when the flush interval expires, whichever comes first. Writing one row
per report would put a database round trip in the critical path of every
aircraft every second. The interval bound is what stops a quiet airspace from
holding the last few updates hostage indefinitely.

**The sweep.** A track that stops reporting produces nothing to trigger its own
expiry, so a timer ages tracks against the clock independently of the message
flow. Without it, an aircraft that leaves coverage would sit on the display
forever at its last known position -- the most misleading failure this system
could have.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from acp.common.contracts import (
    TOPIC_TRACK_UPDATES,
    SurveillanceReport,
    TrackState,
    TrackUpdate,
)
from acp.common.logging import get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.common.metrics import METRICS
from acp.common.tracing import span
from acp.services.track.estimator import TrackEstimator
from acp.storage.stores import LiveTrackStore, TrackHistoryStore

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrackerStats:
    """End-of-run totals for the shutdown log line.

    Not the same thing as the Prometheus counters in `acp.common.metrics`.
    Those are live and scrapeable; these summarise one run and are what the
    tests assert on.
    """

    reports_consumed: int
    updates_published: int
    tracks_terminated: int


class TrackRunner:
    """Consumes reports, maintains tracks, and publishes estimates."""

    def __init__(
        self,
        subscriber: MessageSubscriber[SurveillanceReport],
        publisher: MessagePublisher,
        history: TrackHistoryStore,
        live: LiveTrackStore,
        *,
        estimator: TrackEstimator | None = None,
        batch_size: int = 50,
        flush_interval_s: float = 1.0,
        sweep_interval_s: float = 5.0,
    ) -> None:
        self._subscriber = subscriber
        self._publisher = publisher
        self._history = history
        self._live = live
        self._estimator = estimator or TrackEstimator()
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._sweep_interval_s = sweep_interval_s

        self._pending: list[TrackUpdate] = []
        self._reports = 0
        self._published = 0
        self._terminated = 0
        self._last_flush = 0.0

    async def run(self, *, stop_after: int | None = None) -> TrackerStats:
        """Consume until cancelled, or until `stop_after` reports for tests."""
        self._last_flush = asyncio.get_running_loop().time()
        sweeper = asyncio.create_task(self._sweep_forever())
        try:
            async for envelope in self._subscriber.stream():
                await self._handle(envelope.message)
                if stop_after is not None and self._reports >= stop_after:
                    break
        finally:
            sweeper.cancel()
            # The consumer's offset is already committed for everything handled,
            # so anything still buffered must reach the stores before shutdown
            # or it is lost with no redelivery to recover it.
            await self._flush()
            await asyncio.gather(sweeper, return_exceptions=True)

        return TrackerStats(
            reports_consumed=self._reports,
            updates_published=self._published,
            tracks_terminated=self._terminated,
        )

    async def _handle(self, report: SurveillanceReport) -> None:
        self._reports += 1
        METRICS.reports_consumed.labels(service="track").inc()
        with (
            METRICS.time(METRICS.pipeline_latency, "track"),
            span("estimate", icao24=report.icao24),
        ):
            update = self._estimator.on_report(report)
        METRICS.live_tracks.labels(service="track").set(self._estimator.live_track_count)
        await self._publish(update)
        self._pending.append(update)

        elapsed = asyncio.get_running_loop().time() - self._last_flush
        if len(self._pending) >= self._batch_size or elapsed >= self._flush_interval_s:
            await self._flush()

    async def _publish(self, update: TrackUpdate) -> None:
        # Keyed by aircraft address, exactly as the inbound reports were, so a
        # downstream consumer sees one aircraft's updates in order too.
        await self._publisher.publish(TOPIC_TRACK_UPDATES, key=update.icao24, message=update)
        self._published += 1
        METRICS.track_updates_published.labels(service="track").inc()

    async def _flush(self) -> None:
        if not self._pending:
            self._last_flush = asyncio.get_running_loop().time()
            return
        batch, self._pending = self._pending, []
        await self._history.record(batch)
        await self._live.publish(batch)
        self._last_flush = asyncio.get_running_loop().time()

    async def _sweep_forever(self) -> None:
        """Age tracks on a timer, independently of the message flow."""
        while True:
            await asyncio.sleep(self._sweep_interval_s)
            try:
                await self.sweep_now(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see below
                # Deliberately blind. The sweep is a background timer; whatever
                # it fails on (a dropped database connection, a Redis blip) must
                # not take the consumer down with it. Losing the sweep degrades
                # the picture -- stale tracks linger -- but reports keep being
                # processed. Losing the consumer stops everything.
                _log.exception("sweep failed")

    async def sweep_now(self, now: datetime) -> None:
        """Age tracks against `now` and publish anything whose state changed.

        Public rather than private so the tests can drive it against a chosen
        clock. Waiting on a real timer to observe a 30 second timeout would make
        the suite slow and flaky for no gain.
        """
        changed = self._estimator.sweep(now)
        if not changed:
            return

        terminated = [u for u in changed if u.state is TrackState.TERMINATED]
        for update in changed:
            await self._publish(update)
        self._pending.extend(changed)
        await self._flush()

        if terminated:
            self._terminated += len(terminated)
            await self._live.forget([u.track_id for u in terminated])
            _log.info(
                "terminated stale tracks",
                extra={"count": len(terminated), "track_ids": [u.track_id for u in terminated]},
            )
        self._estimator.prune_terminated(now, older_than=timedelta(minutes=10))
