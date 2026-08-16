"""Tests for what happens when Kafka moves a partition to another consumer.

## The defect these exist because of

The tracker keeps a Kalman filter per aircraft, so partition ownership is
**state**, not just a cursor. Kafka reassigns partitions freely — a replica
joining, leaving, or being restarted is enough — and until M5's remediation
nothing told the tracker when that happened.

The consequence was not a crash. A replica that had lost a partition kept the
filters for the aircraft on it, kept ageing them on its sweep timer, and thirty
seconds later published `TERMINATED` for each and deleted them from Redis —
while another replica was actively maintaining the same deterministic
`track_id`. An aircraft would blink off the display and out of the shared
picture while still reporting normally, and the only trigger was scaling the
service the README describes as "the one service that genuinely scales out".

Two external reviews found it independently. `m1-walking-skeleton.md` had
already admitted that mid-stream rebalance was untested; this is that test.

## Why these are unit tests with a fake

Partition assignment against a real broker is covered by
`tests/integration/`. What is asserted here is the *decision* — release without
terminating — which is the part that was wrong, and which a fake can drive
deterministically without waiting on a real rebalance.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

from aiokafka import TopicPartition

from acp.common.contracts import (
    TOPIC_SURVEILLANCE_REPORTS,
    TOPIC_TRACK_UPDATES,
    SurveillanceReport,
    TrackState,
    TrackUpdate,
)
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.services.track.estimator import TrackEstimator
from acp.services.track.runner import TrackRunner
from acp.storage.stores import LiveTrackStore, TrackHistoryStore
from tests.unit.fakes import FakeHistoryStore, FakeLiveStore, FakePublisher, FakeSubscriber

START = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def a_report(*, at: datetime, icao24: str, lon: float = -75.0) -> SurveillanceReport:
    return SurveillanceReport(
        report_id=f"r-{at.timestamp()}-{icao24}",
        icao24=icao24,
        callsign="TEST01",
        observed_at=at,
        lat=40.0,
        lon=lon,
        altitude_baro_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=90.0,
        scenario_id="rebalance-test",
    )


async def _tracker_holding(
    reports: list[SurveillanceReport], partitions: list[int]
) -> tuple[TrackRunner, FakePublisher, FakeLiveStore]:
    """Run a tracker over reports arriving on the given partitions."""
    publisher = FakePublisher()
    live = FakeLiveStore()
    runner = TrackRunner(
        cast(
            MessageSubscriber[SurveillanceReport],
            FakeSubscriber(reports, topic=TOPIC_SURVEILLANCE_REPORTS, partitions=partitions),
        ),
        cast(MessagePublisher, publisher),
        cast(TrackHistoryStore, FakeHistoryStore()),
        cast(LiveTrackStore, live),
        sweep_interval_s=3600.0,
    )
    await runner.run()
    return runner, publisher, live


def _terminations(publisher: FakePublisher) -> list[TrackUpdate]:
    return [
        cast(TrackUpdate, m)
        for m in publisher.messages_on(TOPIC_TRACK_UPDATES)
        if cast(TrackUpdate, m).state is TrackState.TERMINATED
    ]


async def test_releasing_a_partition_drops_only_its_aircraft() -> None:
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24=address, lon=-75.0 + i * 0.01)
        for i in range(4)
        for address in ("aaa111", "bbb222")
    ]
    partitions = [0 if r.icao24 == "aaa111" else 1 for r in reports]
    runner, _, _ = await _tracker_holding(reports, partitions)
    assert runner._estimator.live_track_count == 2

    await runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)}))

    # Only the aircraft on partition 0 is gone. Counted rather than probed by
    # address, because `innovation_for` returns 0.0 both for a track that was
    # released and for one that has only just been initiated -- a proxy that
    # would pass whether or not the release worked.
    assert runner._estimator.live_track_count == 1


async def test_releasing_a_partition_publishes_no_termination() -> None:
    """The assertion the whole fix exists for.

    An aircraft whose partition moved has not gone anywhere. Publishing
    `TERMINATED` for it tells every downstream consumer -- and the display --
    that it has, while the new owner is still updating the same track_id.
    """
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24="aaa111", lon=-75.0 + i * 0.01)
        for i in range(4)
    ]
    runner, publisher, live = await _tracker_holding(reports, [0] * len(reports))
    before = len(publisher.messages_on(TOPIC_TRACK_UPDATES))

    await runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)}))

    assert _terminations(publisher) == []
    assert len(publisher.messages_on(TOPIC_TRACK_UPDATES)) == before, (
        "releasing a partition must publish nothing at all"
    )
    assert live.forgotten == [], "the new owner's Redis entry must not be deleted"


async def test_a_released_aircraft_is_not_swept_into_termination_later() -> None:
    """The actual failure path: release, then let the sweep timer fire.

    Before the fix this is where `TERMINATED` appeared -- not at the rebalance
    itself but thirty seconds afterwards, which is what made it so hard to
    attribute to scaling the tracker.
    """
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24="aaa111", lon=-75.0 + i * 0.01)
        for i in range(4)
    ]
    runner, publisher, live = await _tracker_holding(reports, [0] * len(reports))
    await runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)}))

    # Well past TERMINATE_AFTER_S.
    await runner.sweep_now(START + timedelta(minutes=5))

    assert _terminations(publisher) == []
    assert live.forgotten == []


async def test_an_aircraft_that_really_stops_reporting_is_still_terminated() -> None:
    """The negative control. The fix must not disable the sweep it sits next to.

    Without this test, `release()` could be doing nothing at all and the three
    tests above would still pass.
    """
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24="aaa111", lon=-75.0 + i * 0.01)
        for i in range(4)
    ]
    runner, publisher, live = await _tracker_holding(reports, [0] * len(reports))

    await runner.sweep_now(START + timedelta(minutes=5))

    assert len(_terminations(publisher)) == 1
    assert live.forgotten == ["trk-aaa111"]


async def test_releasing_a_partition_we_never_held_is_harmless() -> None:
    reports = [a_report(at=START, icao24="aaa111")]
    runner, publisher, _ = await _tracker_holding(reports, [0])
    before = len(publisher.messages_on(TOPIC_TRACK_UPDATES))

    await runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 5)}))

    assert len(publisher.messages_on(TOPIC_TRACK_UPDATES)) == before
    assert runner._estimator.live_track_count == 1


async def test_a_revocation_on_another_topic_is_ignored() -> None:
    """The listener is generic; the tracker only owns surveillance reports."""
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24="aaa111", lon=-75.0 + i * 0.01)
        for i in range(4)
    ]
    runner, _, _ = await _tracker_holding(reports, [0] * len(reports))

    await runner.release_partitions(frozenset({TopicPartition(TOPIC_TRACK_UPDATES, 0)}))

    assert runner._estimator.live_track_count == 1


async def test_the_estimator_release_reports_how_many_it_dropped() -> None:
    estimator = TrackEstimator()
    for address in ("aaa111", "bbb222"):
        estimator.on_report(a_report(at=START, icao24=address))

    assert estimator.release(["aaa111", "never-seen"]) == 1
    assert estimator.live_track_count == 1


# ---------------------------------------------------------------------------
# Concurrency
#
# The tests above all call `release_partitions` after `run()` has finished, so
# they cannot see an interleaving -- and a review pointed out that a real
# rebalance does not wait for a convenient moment. Kafka's coordinator invokes
# the listener from its own task, so it can land while a handler is suspended
# inside `publisher.publish()`.
#
# Both orderings are tested, because they fail in different ways: handler-first
# leaves a released aircraft in the write buffer, and revoke-first lets a report
# rebuild an estimator for an aircraft that is no longer ours.
# ---------------------------------------------------------------------------


class _BlockingPublisher(FakePublisher):
    """A publisher that can be suspended mid-publish, the way a real one is."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.blocking = False

    async def publish(self, topic: str, *, key: str, message: object) -> None:
        if self.blocking:
            self.entered.set()
            await self.gate.wait()
        await super().publish(topic, key=key, message=message)  # type: ignore[arg-type]


async def _runner_with(publisher: FakePublisher, live: FakeLiveStore) -> TrackRunner:
    return TrackRunner(
        cast(MessageSubscriber[SurveillanceReport], FakeSubscriber([])),
        cast(MessagePublisher, publisher),
        cast(TrackHistoryStore, FakeHistoryStore()),
        cast(LiveTrackStore, live),
        sweep_interval_s=3600.0,
    )


async def test_a_revocation_during_a_publish_does_not_resurrect_the_aircraft() -> None:
    """The reproduced defect: released, then written back by an in-flight handler.

    Before the lock: the handler suspends inside `publish()`, the revocation
    flushes and drops the estimator, the handler resumes and appends its update
    to `_pending`, and the next flush writes the released aircraft back into
    Redis -- over whatever its new owner has since written there.
    """
    publisher = _BlockingPublisher()
    live = FakeLiveStore()
    runner = await _runner_with(publisher, live)

    runner._partition_of["aaa111"] = 0
    await runner._handle(a_report(at=START, icao24="aaa111"), 0)
    await runner._flush()
    live.current.clear()

    publisher.blocking = True
    handler = asyncio.create_task(
        runner._handle(a_report(at=START + timedelta(seconds=1), icao24="aaa111"), 0)
    )
    await publisher.entered.wait()

    revoke = asyncio.create_task(
        runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)}))
    )
    await asyncio.sleep(0)  # let the revocation reach the lock and wait there
    publisher.gate.set()
    await asyncio.gather(handler, revoke)
    await runner._flush()

    assert runner._estimator.live_track_count == 0
    assert live.current == {}, "a released aircraft was written back to the shared picture"


async def test_a_report_arriving_after_revocation_is_discarded() -> None:
    """The other ordering: revocation wins the lock, then the report arrives.

    Processing it would rebuild a filter for an aircraft that now belongs to
    another replica and republish it over the new owner's state. The offset was
    never committed, so the new owner replays the report.
    """
    publisher = FakePublisher()
    live = FakeLiveStore()
    runner = await _runner_with(publisher, live)

    await runner.release_partitions(frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)}))
    await runner._handle(a_report(at=START, icao24="aaa111"), 0)

    assert runner._estimator.live_track_count == 0
    assert publisher.messages_on(TOPIC_TRACK_UPDATES) == []
    assert runner._discarded == 1


async def test_a_reassigned_partition_is_processed_again() -> None:
    """Without `claim_partitions` a returned partition stays discarded forever."""
    publisher = FakePublisher()
    live = FakeLiveStore()
    runner = await _runner_with(publisher, live)
    tp = frozenset({TopicPartition(TOPIC_SURVEILLANCE_REPORTS, 0)})

    await runner.release_partitions(tp)
    await runner.claim_partitions(tp)
    await runner._handle(a_report(at=START, icao24="aaa111"), 0)

    assert runner._estimator.live_track_count == 1
    assert len(publisher.messages_on(TOPIC_TRACK_UPDATES)) == 1


async def test_the_partition_map_does_not_grow_without_bound() -> None:
    """It was only ever cleaned on a rebalance, so a stable consumer leaked.

    One entry per aircraft ever seen, kept long after the filter itself had been
    pruned -- small each, unbounded in total, and only visible in a soak nobody
    runs.
    """
    publisher = FakePublisher()
    runner = await _runner_with(publisher, FakeLiveStore())

    runner._partition_of["aaa111"] = 0
    await runner._handle(a_report(at=START, icao24="aaa111"), 0)
    assert len(runner._partition_of) == 1

    # Terminate, then push past the ten-minute prune window.
    await runner.sweep_now(START + timedelta(minutes=5))
    await runner.sweep_now(START + timedelta(minutes=20))

    assert runner._estimator.live_track_count == 0
    assert runner._partition_of == {}
