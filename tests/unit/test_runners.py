"""Tests for the feed and track service loops.

Infrastructure is replaced by the fakes in `fakes.py`, so what is under test is
the orchestration: what gets published, on which topic, with which key, and what
reaches the stores. Whether Kafka and Postgres behave as the fakes pretend is a
separate question, answered by the integration tests at M4.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from acp.common.contracts import (
    TOPIC_SIM_TRUTH,
    TOPIC_SURVEILLANCE_REPORTS,
    TOPIC_TRACK_UPDATES,
    SurveillanceReport,
    TrackState,
)
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.services.feed.runner import FeedRunner
from acp.services.track.estimator import TrackEstimator
from acp.services.track.runner import TrackRunner
from acp.sim.scenario import AircraftSpec, GeoPoint, InitialState, Scenario
from acp.storage.stores import LiveTrackStore, TrackHistoryStore
from tests.unit.fakes import FakeHistoryStore, FakeLiveStore, FakePublisher, FakeSubscriber

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_scenario(*, duration_s: float = 10.0, aircraft: int = 2) -> Scenario:
    return Scenario(
        scenario_id="runner-test",
        seed=7,
        duration_s=duration_s,
        reference=GeoPoint(lat=40.0, lon=-75.0),
        aircraft=tuple(
            AircraftSpec(
                icao24=f"a1b2c{i}",
                callsign=f"RUN{i:03d}",
                initial=InitialState(
                    lat=40.0 + i,
                    lon=-75.0,
                    altitude_ft=35000.0,
                    ground_speed_kt=450.0,
                    track_deg=90.0,
                ),
            )
            for i in range(aircraft)
        ),
    )


def a_report(*, at: datetime, icao24: str = "a1b2c3", lon: float = -75.0) -> SurveillanceReport:
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
        scenario_id="runner-test",
    )


# --------------------------------------------------------------------------
# Feed service
# --------------------------------------------------------------------------


async def test_the_feed_publishes_reports_and_truth_on_separate_topics() -> None:
    """The separation the whole evaluation strategy rests on."""
    publisher = FakePublisher()
    runner = FeedRunner(
        a_scenario(), cast(MessagePublisher, publisher), realtime=False, publish_truth=True
    )

    stats = await runner.run(start_time=START)

    assert stats.steps == 10
    assert publisher.messages_on(TOPIC_SURVEILLANCE_REPORTS)
    assert publisher.messages_on(TOPIC_SIM_TRUTH)
    assert stats.truth_published == 20  # 2 aircraft x 10 steps, never dropped


async def test_truth_publishing_can_be_switched_off() -> None:
    publisher = FakePublisher()
    runner = FeedRunner(
        a_scenario(), cast(MessagePublisher, publisher), realtime=False, publish_truth=False
    )

    stats = await runner.run(start_time=START)

    assert stats.truth_published == 0
    assert publisher.messages_on(TOPIC_SIM_TRUTH) == []


async def test_reports_are_keyed_by_aircraft_address() -> None:
    """The key is what buys per-aircraft ordering. Wrong key, silent corruption."""
    publisher = FakePublisher()
    await FeedRunner(a_scenario(), cast(MessagePublisher, publisher), realtime=False).run(
        start_time=START
    )

    for published in publisher.published:
        assert published.key == published.message.icao24  # type: ignore[attr-defined]


async def test_dropped_reports_mean_fewer_reports_than_truth_states() -> None:
    """Sanity check that the sensor model is actually in the path."""
    publisher = FakePublisher()
    stats = await FeedRunner(
        a_scenario(duration_s=600.0), cast(MessagePublisher, publisher), realtime=False
    ).run(start_time=START)

    assert stats.reports_published < stats.truth_published


# --------------------------------------------------------------------------
# Track service
# --------------------------------------------------------------------------


async def _run_tracker(
    reports: list[SurveillanceReport],
    *,
    history: FakeHistoryStore | None = None,
    live: FakeLiveStore | None = None,
    estimator: TrackEstimator | None = None,
) -> tuple[FakePublisher, FakeHistoryStore, FakeLiveStore]:
    publisher = FakePublisher()
    history = history or FakeHistoryStore()
    live = live or FakeLiveStore()
    runner = TrackRunner(
        cast(MessageSubscriber[SurveillanceReport], FakeSubscriber(reports)),
        cast(MessagePublisher, publisher),
        cast(TrackHistoryStore, history),
        cast(LiveTrackStore, live),
        estimator=estimator,
        # Long enough that the sweep never fires during these tests; sweep
        # behaviour is tested directly on the estimator instead.
        sweep_interval_s=3600.0,
    )
    await runner.run()
    return publisher, history, live


async def test_the_tracker_publishes_one_update_per_report() -> None:
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(5)]
    publisher, _, _ = await _run_tracker(reports)
    assert len(publisher.messages_on(TOPIC_TRACK_UPDATES)) == 5


async def test_track_updates_keep_the_aircraft_address_as_the_key() -> None:
    """Ordering must survive the hop from reports to updates, not just into it."""
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(3)]
    publisher, _, _ = await _run_tracker(reports)
    assert {p.key for p in publisher.published} == {"a1b2c3"}


async def test_everything_published_also_reaches_both_stores() -> None:
    """A buffered update lost at shutdown has no redelivery to recover it."""
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(7)]
    publisher, history, live = await _run_tracker(reports)

    assert len(history.written) == len(publisher.messages_on(TOPIC_TRACK_UPDATES))
    assert set(live.current) == {"trk-a1b2c3"}


async def test_the_live_picture_holds_only_the_newest_update_per_track() -> None:
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(5)]
    _, _, live = await _run_tracker(reports)

    assert len(live.current) == 1
    assert live.current["trk-a1b2c3"].lon == pytest.approx(-75.0 + 4 * 0.01)


async def test_several_aircraft_produce_several_tracks() -> None:
    reports = [
        a_report(at=START + timedelta(seconds=i), icao24=icao, lon=-75.0 + i * 0.01)
        for i in range(4)
        for icao in ("a1b2c3", "ffffff")
    ]
    _, _, live = await _run_tracker(reports)
    assert set(live.current) == {"trk-a1b2c3", "trk-ffffff"}


async def test_a_replayed_report_does_not_create_a_second_track() -> None:
    """At-least-once delivery: the same report arriving twice must be harmless."""
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(3)]
    _, _, live = await _run_tracker([*reports, *reports])
    assert set(live.current) == {"trk-a1b2c3"}


async def test_the_tracker_reports_what_it_did() -> None:
    publisher = FakePublisher()
    runner = TrackRunner(
        cast(
            MessageSubscriber[SurveillanceReport],
            FakeSubscriber([a_report(at=START + timedelta(seconds=i)) for i in range(3)]),
        ),
        cast(MessagePublisher, publisher),
        cast(TrackHistoryStore, FakeHistoryStore()),
        cast(LiveTrackStore, FakeLiveStore()),
        sweep_interval_s=3600.0,
    )
    stats = await runner.run()
    assert stats.reports_consumed == 3
    assert stats.updates_published == 3


# --------------------------------------------------------------------------
# The sweep: ageing tracks against the clock, not against the message flow
# --------------------------------------------------------------------------


def _tracker(
    reports: list[SurveillanceReport],
    history: FakeHistoryStore,
    live: FakeLiveStore,
    publisher: FakePublisher,
) -> TrackRunner:
    return TrackRunner(
        cast(MessageSubscriber[SurveillanceReport], FakeSubscriber(reports)),
        cast(MessagePublisher, publisher),
        cast(TrackHistoryStore, history),
        cast(LiveTrackStore, live),
        estimator=TrackEstimator(coast_after_s=5.0, terminate_after_s=30.0),
        sweep_interval_s=3600.0,
    )


async def test_a_silent_track_is_marked_coasting_by_the_sweep() -> None:
    """Nothing in the message flow can trigger this -- the aircraft has gone quiet."""
    publisher, history, live = FakePublisher(), FakeHistoryStore(), FakeLiveStore()
    runner = _tracker([a_report(at=START)], history, live, publisher)
    await runner.run()

    await runner.sweep_now(START + timedelta(seconds=10))

    assert live.current["trk-a1b2c3"].state is TrackState.COASTING


async def test_a_terminated_track_is_removed_from_the_live_picture() -> None:
    """An aircraft that stopped reporting must leave the display, not freeze on it."""
    publisher, history, live = FakePublisher(), FakeHistoryStore(), FakeLiveStore()
    runner = _tracker([a_report(at=START)], history, live, publisher)
    await runner.run()

    await runner.sweep_now(START + timedelta(seconds=45))

    assert "trk-a1b2c3" not in live.current
    assert live.forgotten == ["trk-a1b2c3"]


async def test_termination_is_still_written_to_history() -> None:
    """The live picture forgets it; the audit trail must not."""
    publisher, history, live = FakePublisher(), FakeHistoryStore(), FakeLiveStore()
    runner = _tracker([a_report(at=START)], history, live, publisher)
    await runner.run()

    await runner.sweep_now(START + timedelta(seconds=45))

    assert history.written[-1].state is TrackState.TERMINATED


async def test_termination_is_published_downstream() -> None:
    """Detectors need to know a track is gone, or they keep reasoning about it."""
    publisher, history, live = FakePublisher(), FakeHistoryStore(), FakeLiveStore()
    runner = _tracker([a_report(at=START)], history, live, publisher)
    await runner.run()

    await runner.sweep_now(START + timedelta(seconds=45))

    published = publisher.messages_on(TOPIC_TRACK_UPDATES)
    assert published[-1].state is TrackState.TERMINATED  # type: ignore[attr-defined]


async def test_a_sweep_with_nothing_to_do_publishes_nothing() -> None:
    publisher, history, live = FakePublisher(), FakeHistoryStore(), FakeLiveStore()
    runner = _tracker([a_report(at=START)], history, live, publisher)
    await runner.run()
    before = len(publisher.published)

    await runner.sweep_now(START + timedelta(seconds=1))

    assert len(publisher.published) == before


async def test_the_first_updates_show_a_track_being_established() -> None:
    reports = [a_report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01) for i in range(4)]
    publisher, _, _ = await _run_tracker(reports)
    states = [u.state for u in publisher.messages_on(TOPIC_TRACK_UPDATES)]  # type: ignore[attr-defined]
    assert states[0] is TrackState.INITIATING
    assert states[-1] is TrackState.CONFIRMED
