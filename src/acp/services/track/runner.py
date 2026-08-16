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

from aiokafka import TopicPartition

from acp.common.contracts import (
    TOPIC_SURVEILLANCE_REPORTS,
    TOPIC_TRACK_UPDATES,
    SurveillanceReport,
    TrackState,
    TrackUpdate,
)
from acp.common.logging import get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.common.metrics import METRICS
from acp.common.tracing import span
from acp.services.track.estimator import TrackEstimator, track_id_for
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
    #: Tracks dropped because their partition was reassigned. Distinct from
    #: `tracks_terminated`: those aircraft stopped reporting, these moved to
    #: another replica and are somebody else's now.
    tracks_released: int = 0


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
        self._released = 0
        self._last_flush = 0.0

        #: Which Kafka partition each aircraft's reports arrive on.
        #:
        #: Recorded rather than computed. Kafka decides the partition from a
        #: hash of the key, and reimplementing that hash here to work out which
        #: aircraft to release on a rebalance would be a second, silently
        #: divergent copy of the broker's own partitioner. Observing where a
        #: report actually came from cannot disagree with the broker, because it
        #: *is* the broker's answer.
        self._partition_of: dict[str, int] = {}

        #: Serialises the three things that mutate tracker state: handling a
        #: report, sweeping, and releasing partitions on a rebalance.
        #:
        #: All three run in **different tasks** -- the consumer loop, the sweep
        #: timer, and Kafka's rebalance callback, which the aiokafka coordinator
        #: invokes from its own background task. Without this, a handler
        #: suspended inside `publisher.publish()` resumes after a revocation has
        #: already flushed and dropped its aircraft, appends its update to
        #: `_pending` anyway, and the next flush writes a released aircraft back
        #: into Redis and Postgres -- on top of whatever its new owner has since
        #: written. Reproduced before it was fixed; see
        #: `tests/unit/test_rebalance.py`.
        self._lock = asyncio.Lock()

        #: Partitions Kafka has taken away and not yet given back. Reports that
        #: were already in flight when the revocation landed are discarded
        #: rather than processed.
        self._revoked: set[int] = set()
        self._discarded = 0

    async def run(self, *, stop_after: int | None = None) -> TrackerStats:
        """Consume until cancelled, or until `stop_after` reports for tests."""
        self._last_flush = asyncio.get_running_loop().time()
        sweeper = asyncio.create_task(self._sweep_forever())
        try:
            async for envelope in self._subscriber.stream():
                self._partition_of[envelope.message.icao24] = envelope.partition
                await self._handle(envelope.message, envelope.partition)
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
            tracks_released=self._released,
        )

    async def release_partitions(self, revoked: frozenset[TopicPartition]) -> None:
        """Give up the aircraft on these partitions. Called on a rebalance.

        This is the fix for a defect that two external reviews found
        independently, and it is worth stating what the defect *was* because the
        broken version looked completely reasonable.

        The tracker kept every filter it had ever created. Kafka moves partition
        ownership during a rebalance, but nothing told the tracker that, so a
        replica that had just lost a partition carried on ageing those aircraft
        on its sweep timer. Thirty seconds later it published `TERMINATED` for
        them and deleted them from Redis -- while the *new* owner was actively
        maintaining the same deterministic `track_id`. The observable result is
        an aircraft blinking out of the shared picture and off the display while
        it is still reporting normally, caused by nothing worse than adding a
        replica.

        Three things happen here, in this order:

        1. Buffered history is flushed. Offsets for it are already committed, so
           it has no redelivery to recover it -- the same argument the shutdown
           path makes.
        2. The estimators are dropped **without publishing anything**. Ownership
           moving is not an aircraft disappearing, and the only correct number of
           `TERMINATED` messages to emit here is zero.
        3. The live-track gauge is corrected, because it is otherwise only ever
           updated while handling a report.

        Redis entries are deliberately left alone: they are the shared picture,
        the new owner is about to overwrite them, and they carry a TTL that
        cleans up if it does not.
        """
        partitions = {tp.partition for tp in revoked if tp.topic == TOPIC_SURVEILLANCE_REPORTS}
        if not partitions:
            return

        async with self._lock:
            # Marked before anything else, so a report that is already queued
            # for one of these partitions is discarded rather than processed
            # after we have given the aircraft up.
            self._revoked |= partitions

            addresses = [
                icao24
                for icao24, partition in self._partition_of.items()
                if partition in partitions
            ]
            # History yes, live picture no -- see `_flush`. An update handled
            # microseconds before the revocation is a true observation and a
            # stale claim, and the two stores need different answers.
            await self._flush(
                exclude_from_live=frozenset(track_id_for(icao24) for icao24 in addresses)
            )

            released = self._estimator.release(addresses)
            for icao24 in addresses:
                del self._partition_of[icao24]

            self._released += released
            METRICS.live_tracks.labels(service="track").set(self._estimator.live_track_count)

        if released:
            _log.info(
                "released tracks after a partition rebalance",
                extra={"partitions": sorted(partitions), "tracks": released},
            )

    async def claim_partitions(self, assigned: frozenset[TopicPartition]) -> None:
        """Take ownership of these partitions again. Called on a rebalance.

        The counterpart to `release_partitions`, and not merely bookkeeping: a
        partition revoked and then given back would otherwise stay in the
        discard set, and this instance would silently drop every report on it
        for the rest of the process's life.
        """
        partitions = {tp.partition for tp in assigned if tp.topic == TOPIC_SURVEILLANCE_REPORTS}
        if not partitions:
            return
        async with self._lock:
            self._revoked -= partitions

    async def _handle(self, report: SurveillanceReport, partition: int = 0) -> None:
        async with self._lock:
            if partition in self._revoked:
                # This partition was taken away while the report was in flight.
                # Processing it would rebuild an estimator for an aircraft that
                # is now somebody else's, and republish it into the shared
                # picture over the top of its new owner. The offset was never
                # committed, so the new owner replays this report.
                self._discarded += 1
                return

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

    async def _flush(self, *, exclude_from_live: frozenset[str] = frozenset()) -> None:
        """Write buffered updates to history, and to the live picture.

        `exclude_from_live` exists for one caller: a rebalance. The two stores
        answer different questions and a revocation splits them.

        **History is a record of what this instance observed.** It is true
        whether or not we still own the aircraft, it is append-only, and the
        unique constraint makes a duplicate a no-op. Dropping it would lose
        track points with no redelivery to recover them, because the offsets are
        already committed.

        **The live picture is a claim about the present.** After revocation this
        instance is no longer entitled to make it: the new owner may already
        have written something newer, and flushing our buffered update over the
        top would replace current state with stale state. Reproduced -- the lock
        alone does not fix it, because the revocation's own flush is what writes
        the in-flight update back.
        """
        if not self._pending:
            self._last_flush = asyncio.get_running_loop().time()
            return
        batch, self._pending = self._pending, []
        await self._history.record(batch)
        live = [u for u in batch if u.track_id not in exclude_from_live]
        if live:
            await self._live.publish(live)
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
        # The sweeper runs in its own task, so without the lock it interleaves
        # with report handling and with a rebalance the same way they interleave
        # with each other.
        async with self._lock:
            changed = self._estimator.sweep(now)
            if not changed:
                self._forget_pruned(now)
                return

            # The gauge was previously set only while handling a report, so once
            # the last aircraft went quiet and was terminated here it kept
            # exporting the old count forever -- a dashboard reading "4 live
            # tracks" over a dead pipeline, which is the most misleading state a
            # monitor can be in.
            METRICS.live_tracks.labels(service="track").set(self._estimator.live_track_count)

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
                    extra={
                        "count": len(terminated),
                        "track_ids": [u.track_id for u in terminated],
                    },
                )
            self._forget_pruned(now)

    def _forget_pruned(self, now: datetime) -> None:
        """Drop long-terminated tracks, and the partition map entries with them.

        The map was previously only ever cleaned on a rebalance, so on a stable
        consumer it grew for the life of the process -- one entry per aircraft
        ever seen, long after its filter had been pruned. Small per entry, and
        unbounded, which is the shape of leak that only shows up in the long
        soak nobody runs.
        """
        self._estimator.prune_terminated(now, older_than=timedelta(minutes=10))
        live = self._estimator.known_addresses
        for icao24 in [a for a in self._partition_of if a not in live]:
            del self._partition_of[icao24]
