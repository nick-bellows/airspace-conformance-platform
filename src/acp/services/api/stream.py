"""WebSocket fan-out of the live airspace picture.

## One reader, many viewers

A naive implementation gives every WebSocket connection its own poll loop. Ten
viewers then means ten times the Redis traffic for byte-identical data, and the
load grows with the audience rather than with the airspace.

Instead a **single** background task reads Redis once per interval and pushes the
same snapshot to every connected client. Redis load is constant in the number of
viewers, which is the property that matters: the number of aircraft is a fact
about the world, the number of people watching is not.

## Why this polls Redis rather than consuming Kafka

The API deliberately never consumes Kafka (ADR 0004 and the service table in the
README). Reading the read model the tracker already maintains keeps that true,
and keeps a display refresh entirely off the pipeline that is estimating the
picture. The cost is honest and worth stating: **this is server-side polling with
push, not event-driven streaming.** Update latency is bounded by the interval
rather than by how quickly an alert is produced. At a one-second interval against
a one-second report rate that is not the bottleneck, and if it ever became one
the fix is a Kafka consumer feeding this broadcaster rather than a redesign of
the client protocol.

## Backpressure

A client that stops reading must not be allowed to stall the broadcaster for
everyone else. Each send is bounded by a timeout, and a client that misses it is
disconnected. Dropping one slow viewer is strictly better than freezing the
display for all of them.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from fastapi import WebSocket
from pydantic import BaseModel

from acp.common.contracts import Alert, TrackUpdate
from acp.common.logging import get_logger
from acp.storage.stores import LiveAlertStore, LiveTrackStore

_log = get_logger(__name__)

#: How often the shared reader polls Redis and broadcasts.
DEFAULT_INTERVAL_S = 1.0

#: A client that cannot accept a frame within this is disconnected.
SEND_TIMEOUT_S = 5.0


def origin_is_allowed(origin: str | None, host: str | None, allowed: str) -> bool:
    """Whether a WebSocket handshake from this origin should be accepted.

    **WebSockets do not respect CORS.** A browser opens one to any origin and
    attaches the user's cookies, so an unchecked endpoint is a Cross-Site
    WebSocket Hijacking primitive the moment authentication exists. This system
    has no authentication today, so the check protects nothing yet -- which is
    exactly why it is cheap to add now and awkward to retrofit later.

    Three cases:

    * **No `Origin` header** -- not a browser. `curl`, a test, another service.
      Allowed: the header is browser-supplied and its absence is not a signal.
    * **Origin host matches the request host** -- the bundled display. Allowed.
    * **Anything else** -- allowed only if explicitly configured.
    """
    if not origin:
        return True

    parsed = urlparse(origin)
    origin_host = parsed.netloc or parsed.path
    if host and origin_host == host:
        return True

    permitted = {entry.strip() for entry in allowed.split(",") if entry.strip()}
    return origin in permitted or origin_host in permitted


class StreamFrame(BaseModel):
    """One snapshot pushed to every connected client.

    A full picture each time rather than a delta. The payload is a few kilobytes
    for a realistic airspace, and a stateless frame means a client that missed
    one is still correct after the next -- which matters more on a display whose
    whole job is to be trusted.
    """

    generated_at: datetime
    live_window_s: float
    tracks: list[TrackUpdate]
    alerts: list[Alert]


@dataclass(eq=False)
class _Client:
    """One connected viewer.

    `eq=False` so instances keep identity-based equality and hashing, which is
    what lets them live in a set. A plain `@dataclass` generates `__eq__` and
    therefore sets `__hash__ = None`, and registering a client raised
    `TypeError: unhashable type` at runtime -- a defect no unit test caught and
    the end-to-end suite found on its first run. Identity is also the correct
    semantics: two viewers are the same only if they are the same connection.
    """

    socket: WebSocket
    #: Frames dropped because this client was too slow. Logged on disconnect so
    #: a struggling viewer is visible rather than silently degraded.
    dropped: int = 0


class AirspaceBroadcaster:
    """Polls the read model once and fans the result out to every viewer."""

    def __init__(
        self,
        live: LiveTrackStore,
        alerts: LiveAlertStore,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        window_s: float = 20.0,
    ) -> None:
        self._live = live
        self._alerts = alerts
        self._interval_s = interval_s
        self._window_s = window_s
        self._clients: set[_Client] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._broadcast_forever())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def register(self, socket: WebSocket) -> _Client:
        """Accept a connection and send it the current picture immediately.

        Without the immediate frame a viewer stares at an empty display until
        the next tick, which reads as "broken" rather than "loading".
        """
        await socket.accept()
        client = _Client(socket=socket)
        self._clients.add(client)
        with contextlib.suppress(Exception):
            await socket.send_text((await self.snapshot()).model_dump_json())
        return client

    def unregister(self, client: _Client) -> None:
        self._clients.discard(client)
        if client.dropped:
            _log.info("client disconnected after dropped frames", extra={"dropped": client.dropped})

    async def snapshot(self) -> StreamFrame:
        """Read the current picture once."""
        now = datetime.now(UTC)
        tracks = await self._live.live(since=now - timedelta(seconds=self._window_s))
        tracks.sort(key=lambda t: t.callsign or t.icao24)
        return StreamFrame(
            generated_at=now,
            live_window_s=self._window_s,
            tracks=tracks,
            alerts=await self._alerts.active(),
        )

    async def _broadcast_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            if not self._clients:
                # Nobody is watching, so do not read Redis at all. A display
                # that nobody has open should cost nothing.
                continue
            try:
                await self.broadcast_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                _log.exception("broadcast tick failed")

    async def broadcast_once(self) -> int:
        """Send one frame to every client. Returns how many were reached."""
        payload = (await self.snapshot()).model_dump_json()
        delivered = 0
        for client in list(self._clients):
            try:
                async with asyncio.timeout(SEND_TIMEOUT_S):
                    await client.socket.send_text(payload)
                delivered += 1
            except (TimeoutError, asyncio.CancelledError):
                client.dropped += 1
                self.unregister(client)
                with contextlib.suppress(Exception):
                    await client.socket.close(code=1011)
            except Exception:  # noqa: BLE001 - a dead socket is routine, not exceptional
                self.unregister(client)
        return delivered
