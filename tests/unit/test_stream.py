"""Tests for the WebSocket broadcaster.

The behaviour worth protecting is that **the cost is a function of the airspace,
not of the audience**. One reader, many viewers; no viewers, no reads at all.
A regression to per-connection polling would not fail any other test and would
not be visible until someone opened the display on ten screens.

The other is backpressure. A viewer that stops reading must not be able to stall
the broadcast for everyone else -- dropping one slow client is strictly better
than freezing the display for all of them.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from acp.common.contracts import (
    Alert,
    AlertKind,
    AlertState,
    DataSource,
    Severity,
    TrackState,
    TrackUpdate,
)
from acp.services.api.stream import AirspaceBroadcaster, origin_is_allowed
from acp.storage.stores import LiveAlertStore, LiveTrackStore
from tests.unit.fakes import FakeAlertStore, FakeLiveStore

NOW = datetime.now(UTC)


@dataclass
class FakeSocket:
    """A WebSocket that records what was pushed to it."""

    sent: list[str] = field(default_factory=list)
    accepted: bool = False
    closed_with: int | None = None
    #: Seconds to stall inside send, for exercising the backpressure path.
    stall_s: float = 0.0
    fail: bool = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise ConnectionResetError("socket is gone")
        if self.stall_s:
            await asyncio.sleep(self.stall_s)
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


@dataclass
class CountingLiveStore(FakeLiveStore):
    """Counts reads, so "one read per tick regardless of viewers" is testable."""

    reads: int = 0

    async def live(self, *, since: datetime | None = None) -> list[TrackUpdate]:
        self.reads += 1
        return await super().live(since=since)


def a_track(track_id: str = "trk-a1b2c3", *, callsign: str = "ACP101") -> TrackUpdate:
    return TrackUpdate(
        track_id=track_id,
        icao24="a1b2c3",
        callsign=callsign,
        updated_at=NOW,
        last_report_at=NOW,
        state=TrackState.CONFIRMED,
        lat=40.0,
        lon=-75.0,
        altitude_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=90.0,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=42,
        source=DataSource.SIMULATOR,
    )


def an_alert() -> Alert:
    return Alert(
        alert_id="al-1",
        alert_key="predicted_conflict:trk-a:trk-b",
        kind=AlertKind.PREDICTED_CONFLICT,
        severity=Severity.CAUTION,
        state=AlertState.NEW,
        raised_at=NOW,
        updated_at=NOW,
        track_ids=("trk-a", "trk-b"),
        reason_codes=("horizontal_below_standard",),
        summary="stream test",
        source=DataSource.SIMULATOR,
    )


def a_broadcaster(
    live: FakeLiveStore | None = None, alerts: FakeAlertStore | None = None, **kwargs: object
) -> AirspaceBroadcaster:
    return AirspaceBroadcaster(
        cast(LiveTrackStore, live or FakeLiveStore()),
        cast(LiveAlertStore, alerts or FakeAlertStore()),
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


async def test_a_client_is_accepted_and_sent_the_picture_immediately() -> None:
    """Without the immediate frame a viewer stares at an empty display until
    the next tick, which reads as broken rather than loading."""
    live = FakeLiveStore(current={"trk-a1b2c3": a_track()})
    broadcaster = a_broadcaster(live)
    socket = FakeSocket()

    await broadcaster.register(cast(object, socket))  # type: ignore[arg-type]

    assert socket.accepted
    assert len(socket.sent) == 1
    frame = json.loads(socket.sent[0])
    assert [t["callsign"] for t in frame["tracks"]] == ["ACP101"]


async def test_a_client_can_be_registered_in_a_set() -> None:
    """A plain @dataclass sets __hash__ to None and this raised TypeError at
    runtime. The end-to-end suite found it; this is the cheap guard."""
    broadcaster = a_broadcaster()
    for _ in range(3):
        await broadcaster.register(cast(object, FakeSocket()))  # type: ignore[arg-type]
    assert broadcaster.client_count == 3


async def test_unregistering_removes_the_client() -> None:
    broadcaster = a_broadcaster()
    client = await broadcaster.register(cast(object, FakeSocket()))  # type: ignore[arg-type]
    broadcaster.unregister(client)
    assert broadcaster.client_count == 0


async def test_unregistering_twice_is_harmless() -> None:
    """The socket handler's finally block runs even when the broadcast loop has
    already dropped the client."""
    broadcaster = a_broadcaster()
    client = await broadcaster.register(cast(object, FakeSocket()))  # type: ignore[arg-type]
    broadcaster.unregister(client)
    broadcaster.unregister(client)
    assert broadcaster.client_count == 0


# --------------------------------------------------------------------------
# One reader, many viewers
# --------------------------------------------------------------------------


async def test_every_client_receives_the_same_frame() -> None:
    live = FakeLiveStore(current={"trk-a1b2c3": a_track()})
    broadcaster = a_broadcaster(live)
    sockets = [FakeSocket() for _ in range(4)]
    for socket in sockets:
        await broadcaster.register(cast(object, socket))  # type: ignore[arg-type]

    delivered = await broadcaster.broadcast_once()

    assert delivered == 4
    payloads = {socket.sent[-1] for socket in sockets}
    assert len(payloads) == 1, "clients received different frames"


async def test_one_broadcast_reads_the_store_once_regardless_of_viewers() -> None:
    """The property the design exists for.

    A per-connection poll loop would make Redis load grow with the audience.
    The number of aircraft is a fact about the world; the number of people
    watching is not.
    """
    live = CountingLiveStore(current={"trk-a1b2c3": a_track()})
    broadcaster = a_broadcaster(live)
    for _ in range(8):
        await broadcaster.register(cast(object, FakeSocket()))  # type: ignore[arg-type]
    live.reads = 0  # registration sends an immediate frame each; measure the tick

    await broadcaster.broadcast_once()

    assert live.reads == 1, f"one tick read the store {live.reads} times for 8 viewers"


async def test_the_frame_carries_tracks_and_alerts_together() -> None:
    """One frame, so the display can never render them a beat apart the way two
    independent requests could."""
    live = FakeLiveStore(current={"trk-a1b2c3": a_track()})
    alerts = FakeAlertStore(current={"predicted_conflict:trk-a:trk-b": an_alert()})
    frame = await a_broadcaster(live, alerts).snapshot()

    assert len(frame.tracks) == 1
    assert len(frame.alerts) == 1
    assert frame.generated_at is not None


async def test_stale_tracks_are_excluded_from_the_frame() -> None:
    old = a_track().model_copy(update={"updated_at": NOW - timedelta(minutes=10)})
    live = FakeLiveStore(current={"trk-a1b2c3": old})
    frame = await a_broadcaster(live, window_s=20.0).snapshot()
    assert frame.tracks == []


async def test_tracks_are_ordered_by_callsign() -> None:
    live = FakeLiveStore(
        current={
            "trk-2": a_track("trk-2", callsign="ZZZ999"),
            "trk-1": a_track("trk-1", callsign="AAA111"),
        }
    )
    frame = await a_broadcaster(live).snapshot()
    assert [t.callsign for t in frame.tracks] == ["AAA111", "ZZZ999"]


# --------------------------------------------------------------------------
# Backpressure and failure
# --------------------------------------------------------------------------


async def test_a_dead_socket_is_dropped_without_affecting_the_others() -> None:
    broadcaster = a_broadcaster()
    healthy = FakeSocket()
    dead = FakeSocket(fail=True)
    await broadcaster.register(cast(object, healthy))  # type: ignore[arg-type]
    await broadcaster.register(cast(object, dead))  # type: ignore[arg-type]

    delivered = await broadcaster.broadcast_once()

    assert delivered == 1
    assert broadcaster.client_count == 1
    assert len(healthy.sent) == 2  # the registration frame plus this one


async def test_a_stalled_client_is_disconnected_rather_than_blocking_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure this bound exists to prevent: one viewer that stops reading
    freezing the display for every other viewer."""
    import acp.services.api.stream as stream_module

    monkeypatch.setattr(stream_module, "SEND_TIMEOUT_S", 0.05)
    broadcaster = a_broadcaster()
    healthy = FakeSocket()
    stalled = FakeSocket(stall_s=5.0)
    await broadcaster.register(cast(object, healthy))  # type: ignore[arg-type]
    await broadcaster.register(cast(object, stalled))  # type: ignore[arg-type]

    async with asyncio.timeout(3.0):
        delivered = await broadcaster.broadcast_once()

    assert delivered == 1
    assert broadcaster.client_count == 1
    assert stalled.closed_with == 1011


# --------------------------------------------------------------------------
# The background loop
# --------------------------------------------------------------------------


async def test_the_loop_starts_and_stops_cleanly() -> None:
    broadcaster = a_broadcaster(interval_s=0.01)
    await broadcaster.start()
    assert broadcaster.running
    await broadcaster.stop()
    assert not broadcaster.running


async def test_stopping_a_broadcaster_that_never_started_is_harmless() -> None:
    await a_broadcaster().stop()


async def test_no_viewers_means_no_reads_at_all() -> None:
    """A display nobody has open should cost nothing."""
    live = CountingLiveStore()
    broadcaster = a_broadcaster(live, interval_s=0.01)
    await broadcaster.start()
    await asyncio.sleep(0.15)
    await broadcaster.stop()
    assert live.reads == 0


# --------------------------------------------------------------------------
# Origin checking
# --------------------------------------------------------------------------


def test_a_request_with_no_origin_is_allowed() -> None:
    """curl, a test, another service. The header is browser-supplied and its
    absence is not a signal."""
    assert origin_is_allowed(None, "example.test", "")
    assert origin_is_allowed("", "example.test", "")


def test_a_same_origin_request_is_allowed() -> None:
    """The bundled display, served from the same host as the API."""
    assert origin_is_allowed("http://localhost:8000", "localhost:8000", "")
    assert origin_is_allowed("https://acp.example", "acp.example", "")


def test_a_cross_origin_request_is_refused_by_default() -> None:
    """WebSockets do not respect CORS: a browser will open one to any origin
    and attach the user's cookies. That is Cross-Site WebSocket Hijacking, and
    the mitigation belongs in place before authentication exists rather than
    after."""
    assert not origin_is_allowed("http://evil.example", "localhost:8000", "")


def test_a_configured_origin_is_allowed() -> None:
    """For the case where the display is served from a different host."""
    allowed = "https://display.example, https://other.example"
    assert origin_is_allowed("https://display.example", "api.example", allowed)
    assert origin_is_allowed("https://other.example", "api.example", allowed)
    assert not origin_is_allowed("https://evil.example", "api.example", allowed)


def test_a_lookalike_origin_does_not_pass() -> None:
    """Substring matching would let `evil-localhost:8000` through."""
    assert not origin_is_allowed("http://evil-localhost:8000", "localhost:8000", "")
    assert not origin_is_allowed("http://localhost:8000.evil.example", "localhost:8000", "")


async def test_the_loop_pushes_frames_while_a_viewer_is_connected() -> None:
    live = FakeLiveStore(current={"trk-a1b2c3": a_track()})
    broadcaster = a_broadcaster(live, interval_s=0.02)
    socket = FakeSocket()
    await broadcaster.register(cast(object, socket))  # type: ignore[arg-type]

    await broadcaster.start()
    await asyncio.sleep(0.15)
    await broadcaster.stop()

    assert len(socket.sent) > 1, "the loop never pushed after the registration frame"
