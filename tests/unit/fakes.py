"""In-memory doubles for the I/O adapters.

These stand in for Kafka, Postgres, and Redis so the *logic* in the runners can
be tested without infrastructure. They are deliberately dumb: they record what
they were asked to do and nothing else.

They do not verify that the real adapters behave the same way -- that is what
the integration tests against real containers are for, arriving at M4. A fake
that drifts from the thing it replaces is worse than no fake, so the split is
recorded here rather than assumed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel

from acp.common.contracts import TrackUpdate
from acp.common.messaging import Envelope


@dataclass(frozen=True, slots=True)
class Published:
    """One message handed to the publisher."""

    topic: str
    key: str
    message: BaseModel


class FakePublisher:
    """Records publishes instead of sending them."""

    def __init__(self) -> None:
        self.published: list[Published] = []

    async def publish(self, topic: str, *, key: str, message: BaseModel) -> None:
        self.published.append(Published(topic=topic, key=key, message=message))

    def messages_on(self, topic: str) -> list[BaseModel]:
        return [p.message for p in self.published if p.topic == topic]


class FakeSubscriber[MessageT: BaseModel]:
    """Replays a fixed list of messages, then stops."""

    def __init__(self, messages: Sequence[MessageT], *, topic: str = "test.topic") -> None:
        self._messages = list(messages)
        self._topic = topic

    async def stream(self) -> AsyncIterator[Envelope[MessageT]]:
        for offset, message in enumerate(self._messages):
            yield Envelope(
                message=message,
                key=getattr(message, "icao24", None),
                topic=self._topic,
                partition=0,
                offset=offset,
                trace_id=f"trace-{offset}",
            )


@dataclass
class FakeHistoryStore:
    """Records batches handed to Postgres."""

    batches: list[list[TrackUpdate]] = field(default_factory=list)
    healthy: bool = True

    async def record(self, updates: Sequence[TrackUpdate]) -> None:
        self.batches.append(list(updates))

    async def history(self, track_id: str, *, limit: int = 600) -> list[dict[str, object]]:
        return [
            {
                "observed_at": u.updated_at,
                "lat": u.lat,
                "lon": u.lon,
                "altitude_ft": u.altitude_ft,
                "ground_speed_kt": u.ground_speed_kt,
                "track_deg": u.track_deg,
                "vertical_rate_fpm": u.vertical_rate_fpm,
                "position_uncertainty_m": u.position_uncertainty_m,
            }
            for batch in self.batches
            for u in batch
            if u.track_id == track_id
        ][:limit]

    async def ping(self) -> bool:
        return self.healthy

    @property
    def written(self) -> list[TrackUpdate]:
        return [u for batch in self.batches for u in batch]


@dataclass
class FakeLiveStore:
    """Records the live airspace picture, keyed like Redis would."""

    current: dict[str, TrackUpdate] = field(default_factory=dict)
    forgotten: list[str] = field(default_factory=list)
    healthy: bool = True

    async def publish(self, updates: Sequence[TrackUpdate]) -> None:
        for update in updates:
            self.current[update.track_id] = update

    async def forget(self, track_ids: Sequence[str]) -> None:
        for track_id in track_ids:
            self.forgotten.append(track_id)
            self.current.pop(track_id, None)

    async def live(self, *, since: datetime | None = None) -> list[TrackUpdate]:
        if since is None:
            return list(self.current.values())
        return [u for u in self.current.values() if u.updated_at >= since]

    async def ping(self) -> bool:
        return self.healthy
