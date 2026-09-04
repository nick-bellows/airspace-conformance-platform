"""Tests for the conformance service loop.

The loop's job is to hold a coherent picture of the airspace and scan it. Most
of the risk is in the picture rather than the geometry: a stale track, a
resurrected terminated track, or an out-of-order update all manufacture
conflicts that never happened, and none of them would be caught by testing the
detector alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from acp.common.contracts import (
    TOPIC_ALERTS,
    AlertKind,
    AlertState,
    DataSource,
    Severity,
    TrackState,
    TrackUpdate,
)
from acp.common.geodesy import destination_point
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.services.conformance.alerts import AlertManager
from acp.services.conformance.monitor import ConformanceMonitor
from acp.services.conformance.runner import ConformanceRunner
from acp.services.conformance.separation import SeparationMonitor
from tests.unit.fakes import FakePublisher, FakeSubscriber

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_track(
    track_id: str,
    *,
    lat: float = 40.0,
    lon: float = -75.0,
    altitude_ft: float = 35000.0,
    track_deg: float = 90.0,
    at: datetime = NOW,
    state: TrackState = TrackState.CONFIRMED,
    squawk: str | None = None,
) -> TrackUpdate:
    return TrackUpdate(
        track_id=track_id,
        icao24=f"{abs(hash(track_id)) % 0xFFFFFF:06x}",
        callsign=track_id.upper()[:8],
        updated_at=at,
        last_report_at=at,
        state=state,
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        ground_speed_kt=450.0,
        track_deg=track_deg,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=50,
        squawk=squawk,
        source=DataSource.SIMULATOR,
    )


def converging_pair(*, separation_nm: float = 60.0, at: datetime = NOW) -> list[TrackUpdate]:
    east_lat, east_lon = destination_point(40.0, -75.0, 90.0, separation_nm)
    return [
        a_track("trk-west", lat=40.0, lon=-75.0, track_deg=90.0, at=at),
        a_track("trk-east", lat=east_lat, lon=east_lon, track_deg=270.0, at=at),
    ]


def a_runner(publisher: FakePublisher, **kwargs: object) -> ConformanceRunner:
    return ConformanceRunner(
        cast(MessageSubscriber[TrackUpdate], FakeSubscriber([])),
        cast(MessagePublisher, publisher),
        monitor=SeparationMonitor(),
        manager=AlertManager(),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Maintaining the picture
# --------------------------------------------------------------------------


def test_absorbing_updates_builds_the_picture() -> None:
    runner = a_runner(FakePublisher())
    for track in converging_pair():
        runner.absorb(track)
    assert runner.live_tracks == 2


def test_a_terminated_track_is_removed_from_the_picture() -> None:
    """Otherwise the detector keeps reasoning about an aircraft that is gone."""
    runner = a_runner(FakePublisher())
    runner.absorb(a_track("trk-a"))
    assert runner.live_tracks == 1
    runner.absorb(a_track("trk-a", state=TrackState.TERMINATED))
    assert runner.live_tracks == 0


def test_an_out_of_order_update_is_ignored() -> None:
    """Moving a track backwards in time can manufacture a closing geometry."""
    runner = a_runner(FakePublisher())
    runner.absorb(a_track("trk-a", lon=-75.0, at=NOW + timedelta(seconds=10)))
    runner.absorb(a_track("trk-a", lon=-70.0, at=NOW))
    assert runner.live_tracks == 1


async def test_stale_tracks_are_dropped_at_scan_time() -> None:
    """A position half a minute old is worse than no position at all."""
    runner = a_runner(FakePublisher(), stale_after_s=15.0)
    for track in converging_pair():
        runner.absorb(track)
    await runner.scan_now(NOW + timedelta(seconds=60))
    assert runner.live_tracks == 0


# --------------------------------------------------------------------------
# Scanning and publishing
# --------------------------------------------------------------------------


async def test_a_conflict_produces_a_published_alert() -> None:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair():
        runner.absorb(track)

    await runner.scan_now(NOW)

    published = publisher.messages_on(TOPIC_ALERTS)
    assert len(published) == 1
    assert published[0].kind is AlertKind.PREDICTED_CONFLICT  # type: ignore[attr-defined]
    assert published[0].state is AlertState.NEW  # type: ignore[attr-defined]


async def test_alerts_are_keyed_by_alert_key() -> None:
    """Every state change for one condition must land on one partition.

    A CLEARED overtaking its own NEW would leave a consumer showing an alert
    that no longer exists, forever.
    """
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair():
        runner.absorb(track)

    await runner.scan_now(NOW)

    assert publisher.published[0].key == publisher.published[0].message.alert_key  # type: ignore[attr-defined]


async def test_a_quiet_airspace_publishes_nothing() -> None:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    runner.absorb(a_track("trk-a", lat=40.0, lon=-75.0))
    runner.absorb(a_track("trk-b", lat=42.0, lon=-72.0))

    await runner.scan_now(NOW)

    assert publisher.published == []


async def test_a_repeated_scan_does_not_republish_the_same_alert() -> None:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair():
        runner.absorb(track)

    await runner.scan_now(NOW)
    await runner.scan_now(NOW + timedelta(seconds=1))

    assert len(publisher.published) == 1


async def test_an_alert_clears_when_the_track_vanishes() -> None:
    """The conflict cannot resolve itself if one aircraft simply stops existing."""
    publisher = FakePublisher()
    runner = a_runner(publisher, stale_after_s=15.0)
    for track in converging_pair():
        runner.absorb(track)
    await runner.scan_now(NOW)

    await runner.scan_now(NOW + timedelta(seconds=60))

    states = [m.state for m in publisher.messages_on(TOPIC_ALERTS)]  # type: ignore[attr-defined]
    assert states[-1] is AlertState.CLEARED


async def test_rule_alerts_are_raised_alongside_conflicts() -> None:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    runner.absorb(a_track("trk-a", squawk="7700"))

    await runner.scan_now(NOW)

    published = publisher.messages_on(TOPIC_ALERTS)
    assert [m.kind for m in published] == [AlertKind.EMERGENCY_SQUAWK]  # type: ignore[attr-defined]
    assert published[0].severity is Severity.WARNING  # type: ignore[attr-defined]


async def test_the_scan_reports_which_alert_keys_changed() -> None:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair():
        runner.absorb(track)

    keys = await runner.scan_now(NOW)

    assert len(keys) == 1
    assert keys[0].startswith("predicted_conflict:")


# --------------------------------------------------------------------------
# Severity reflects urgency
# --------------------------------------------------------------------------


async def _severity_at(separation_nm: float) -> Severity:
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair(separation_nm=separation_nm):
        runner.absorb(track)
    await runner.scan_now(NOW)
    return publisher.messages_on(TOPIC_ALERTS)[0].severity  # type: ignore[attr-defined,no-any-return]


async def test_an_imminent_conflict_is_a_warning() -> None:
    """Closing at 900 kt from 10 NM is about 40 seconds out."""
    assert await _severity_at(10.0) is Severity.WARNING


async def test_a_distant_conflict_is_only_an_advisory() -> None:
    """Time is the scarce resource, so time drives severity - not miss distance."""
    assert await _severity_at(70.0) is Severity.ADVISORY


# --------------------------------------------------------------------------
# The consume loop
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The scan clock, which has to work under replay as well as live
# --------------------------------------------------------------------------


def test_the_clock_follows_the_wall_when_there_is_no_data() -> None:
    runner = a_runner(FakePublisher())
    assert (datetime.now(UTC) - runner.clock).total_seconds() < 5.0


def test_the_clock_follows_the_data_when_it_runs_ahead_of_the_wall() -> None:
    """Under replay the feed runs flat out, so data time races ahead.

    Judging staleness by the wall clock there would mark nothing stale, the
    picture would grow without bound, and conflicts would be computed across
    inconsistent timestamps.
    """
    runner = a_runner(FakePublisher())
    future = datetime.now(UTC) + timedelta(hours=2)
    runner.absorb(a_track("trk-a", at=future))
    assert runner.clock == future


def test_the_clock_falls_back_to_the_wall_when_the_feed_stops() -> None:
    """Otherwise a dead feed freezes the picture instead of expiring it."""
    runner = a_runner(FakePublisher())
    runner.absorb(a_track("trk-a", at=datetime.now(UTC) - timedelta(hours=1)))
    assert (datetime.now(UTC) - runner.clock).total_seconds() < 5.0


async def test_replayed_traffic_still_expires() -> None:
    """The regression this clock exists to prevent."""
    runner = a_runner(FakePublisher(), stale_after_s=15.0)
    base = datetime.now(UTC) + timedelta(hours=3)
    for track in converging_pair(at=base):
        runner.absorb(track)
    assert runner.live_tracks == 2

    runner.absorb(a_track("trk-c", lat=41.0, lon=-70.0, at=base + timedelta(seconds=60)))
    await runner.scan_now(runner.clock)

    assert runner.live_tracks == 1  # only the newest survived


async def test_run_without_a_subscriber_fails_loudly() -> None:
    """The evaluation harness builds one without a subscriber on purpose.

    Silently doing nothing would make a broken service look healthy.
    """
    runner = ConformanceRunner(None, cast(MessagePublisher, FakePublisher()))
    with pytest.raises(RuntimeError, match="without a subscriber"):
        await runner.run()


async def test_the_runner_consumes_updates_until_the_stream_ends() -> None:
    publisher = FakePublisher()
    runner = ConformanceRunner(
        cast(MessageSubscriber[TrackUpdate], FakeSubscriber(converging_pair())),
        cast(MessagePublisher, publisher),
        # Long enough that the background scan never fires; scanning is tested
        # directly above against a chosen clock instead of a real timer.
        scan_interval_s=3600.0,
    )
    stats = await runner.run()
    assert stats.updates_consumed == 2


async def test_explicit_termination_clears_an_active_alert_immediately() -> None:
    """The other half of `test_a_terminated_track_is_removed_from_the_picture`.

    That test asserts the track leaves the picture, which it always did. An
    external review found the rest of the cleanup missing: a `TERMINATED`
    update popped the picture and returned, without telling the alert manager
    or the conformance monitor. Only the *timeout* path called `forget`.

    The observable cost is an alert for an aircraft the system has been told no
    longer exists, surviving until ordinary hysteresis clears it several scans
    later -- and conformance prediction state retained for a track id that can
    be reused, because track ids are derived from the aircraft address.
    """
    publisher = FakePublisher()
    runner = a_runner(publisher)
    for track in converging_pair():
        runner.absorb(track)
    await runner.scan_now(NOW)
    published = publisher.messages_on(TOPIC_ALERTS)
    raised = [m for m in published if m.state is AlertState.NEW]  # type: ignore[attr-defined]
    assert raised, "expected a conflict alert before terminating"

    runner.absorb(a_track("trk-west", state=TrackState.TERMINATED))
    await runner.scan_now(NOW + timedelta(seconds=1))

    cleared = [
        m
        for m in publisher.messages_on(TOPIC_ALERTS)
        if m.state is AlertState.CLEARED  # type: ignore[attr-defined]
    ]
    assert cleared, (
        "an alert about a track the system was explicitly told is gone must clear "
        "on the next scan, not wait for hysteresis"
    )


async def test_explicit_termination_releases_conformance_state() -> None:
    """The other half: prediction windows must not outlive the track.

    Track ids are derived from the aircraft address, so a retained window is
    not merely a leak -- a later aircraft on the same address inherits the
    prediction history of a different flight.
    """
    monitor = ConformanceMonitor()
    runner = a_runner(FakePublisher(), conformance=monitor)
    for track in converging_pair():
        runner.absorb(track)
    await runner.scan_now(NOW)
    assert monitor.tracked > 0, "expected the monitor to be holding window state"

    for track in converging_pair():
        runner.absorb(a_track(track.track_id, state=TrackState.TERMINATED))
    await runner.scan_now(NOW + timedelta(seconds=1))

    assert monitor.tracked == 0, (
        "conformance windows survived a track the system was told no longer exists"
    )
