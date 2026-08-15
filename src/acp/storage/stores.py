"""Persistence for tracks: Postgres for history, Redis for the live picture.

Two stores because they answer different questions.

*Postgres* answers "where has this aircraft been?" -- an append-only history,
queried rarely, kept for as long as the retention policy says. It is written
with SQLAlchemy Core rather than the ORM: these are bulk inserts of flat rows at
one per aircraft per second, and object materialisation would be pure overhead.

*Redis* answers "what is in the air right now?" -- read on every display refresh,
overwritten constantly, and worthless the moment it is stale. Keys carry a TTL,
so if the tracker dies the picture empties by itself instead of showing aircraft
that stopped existing minutes ago. **That expiry is a feature, not a cache
detail:** a frozen display is more dangerous than an empty one.

Both writes are idempotent, because at-least-once delivery means any message may
arrive twice (ADR 0005).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from acp.common.contracts import TrackUpdate
from acp.storage.schema import track_points_table, tracks_table


class TrackHistoryStore:
    """Postgres-backed track history."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_dsn(cls, dsn: str) -> TrackHistoryStore:
        return cls(create_async_engine(dsn, pool_size=5, max_overflow=5, pool_pre_ping=True))

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> bool:
        """Readiness check. Returns False rather than raising."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(select(1))
        except Exception:  # noqa: BLE001 - readiness must never propagate
            return False
        return True

    async def record(self, updates: Sequence[TrackUpdate]) -> None:
        """Persist a batch of updates. Safe to call with the same batch twice."""
        if not updates:
            return

        async with self._engine.begin() as conn:
            # One row per track, not one per update. A batch normally holds
            # several updates for the same aircraft, and Postgres rejects an
            # ON CONFLICT DO UPDATE whose input touches the same row twice in a
            # single statement ("cannot affect row a second time"). Collapsing
            # to the newest update per track is both what the table means and
            # what makes the statement legal.
            latest: dict[str, TrackUpdate] = {}
            first_seen: dict[str, datetime] = {}
            for update in updates:
                existing = latest.get(update.track_id)
                if existing is None or update.updated_at >= existing.updated_at:
                    latest[update.track_id] = update
                earliest = first_seen.get(update.track_id)
                if earliest is None or update.last_report_at < earliest:
                    first_seen[update.track_id] = update.last_report_at

            track_rows = [
                {
                    "track_id": u.track_id,
                    "icao24": u.icao24,
                    "callsign": u.callsign,
                    "scenario_id": u.scenario_id,
                    # Only meaningful on the initial insert -- the upsert below
                    # deliberately leaves it alone thereafter, so a track keeps
                    # the time it was really first seen.
                    "first_seen_at": first_seen[u.track_id],
                    "last_seen_at": u.updated_at,
                    "state": str(u.state),
                    "update_count": u.update_count,
                }
                for u in latest.values()
            ]
            upsert = pg_insert(tracks_table).values(track_rows)
            await conn.execute(
                upsert.on_conflict_do_update(
                    index_elements=[tracks_table.c.track_id],
                    set_={
                        "last_seen_at": upsert.excluded.last_seen_at,
                        "state": upsert.excluded.state,
                        "update_count": upsert.excluded.update_count,
                        "callsign": upsert.excluded.callsign,
                    },
                )
            )

            # Deduplicated on the same key as the unique constraint. A sweep
            # update and a report update can land on the same timestamp for the
            # same track; letting both through would rely on the database to
            # sort it out mid-statement.
            points: dict[tuple[str, datetime], TrackUpdate] = {
                (u.track_id, u.updated_at): u for u in updates
            }
            point_rows = [
                {
                    "track_id": u.track_id,
                    "observed_at": u.updated_at,
                    "lat": u.lat,
                    "lon": u.lon,
                    "altitude_ft": u.altitude_ft,
                    "ground_speed_kt": u.ground_speed_kt,
                    "track_deg": u.track_deg,
                    "vertical_rate_fpm": u.vertical_rate_fpm,
                    "turn_rate_deg_s": u.turn_rate_deg_s,
                    "position_uncertainty_m": u.position_uncertainty_m,
                }
                for u in points.values()
            ]
            # Redelivery lands here. Dropping the duplicate is the whole
            # idempotency strategy, and it costs one index lookup.
            await conn.execute(
                pg_insert(track_points_table)
                .values(point_rows)
                .on_conflict_do_nothing(constraint="uq_track_points_track_observed")
            )

    async def history(self, track_id: str, *, limit: int = 600) -> list[dict[str, object]]:
        """Most recent points for a track, oldest first."""
        query = (
            select(track_points_table)
            .where(track_points_table.c.track_id == track_id)
            .order_by(track_points_table.c.observed_at.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [dict(row) for row in reversed(rows)]


class LiveTrackStore:
    """Redis-backed current airspace picture.

    One key per track holding the latest update as JSON, plus a sorted set
    indexed by update time so the API can fetch "everything seen recently"
    without scanning the keyspace.
    """

    KEY_PREFIX = "acp:track:"
    INDEX_KEY = "acp:tracks:live"

    def __init__(self, client: redis.Redis, *, ttl_s: int = 120) -> None:
        self._client = client
        self._ttl_s = ttl_s

    @classmethod
    def from_url(cls, url: str, *, ttl_s: int = 120) -> LiveTrackStore:
        # redis-py ships type hints but leaves `from_url` itself unannotated, so
        # strict mode rejects the call. Suppressed at the single call site rather
        # than by relaxing `disallow_untyped_calls` for the whole module.
        client: redis.Redis = redis.from_url(url, decode_responses=True)  # type: ignore[no-untyped-call]
        return cls(client, ttl_s=ttl_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:  # noqa: BLE001 - readiness must never propagate
            return False

    async def publish(self, updates: Sequence[TrackUpdate]) -> None:
        """Overwrite the live entry for each track. Last write wins, by design."""
        if not updates:
            return
        async with self._client.pipeline(transaction=False) as pipe:
            for update in updates:
                key = f"{self.KEY_PREFIX}{update.track_id}"
                pipe.set(key, update.model_dump_json(), ex=self._ttl_s)
                pipe.zadd(self.INDEX_KEY, {update.track_id: update.updated_at.timestamp()})
            await pipe.execute()

    async def forget(self, track_ids: Sequence[str]) -> None:
        """Remove terminated tracks immediately rather than waiting for the TTL."""
        if not track_ids:
            return
        async with self._client.pipeline(transaction=False) as pipe:
            for track_id in track_ids:
                pipe.delete(f"{self.KEY_PREFIX}{track_id}")
                pipe.zrem(self.INDEX_KEY, track_id)
            await pipe.execute()

    async def live(self, *, since: datetime | None = None) -> list[TrackUpdate]:
        """Every track updated recently enough to still be believable."""
        minimum = since.timestamp() if since else "-inf"
        track_ids = await self._client.zrangebyscore(self.INDEX_KEY, minimum, "+inf")
        if not track_ids:
            return []

        payloads = await self._client.mget([f"{self.KEY_PREFIX}{t}" for t in track_ids])
        updates = []
        expired = []
        for track_id, payload in zip(track_ids, payloads, strict=True):
            if payload is None:
                # The key's TTL fired but the index entry outlived it. Tidy up
                # so the index does not grow without bound.
                expired.append(track_id)
                continue
            updates.append(TrackUpdate.model_validate(json.loads(payload)))
        if expired:
            await self.forget(expired)
        return updates
