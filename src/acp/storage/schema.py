"""Database schema for track storage.

Kept separate from `store.py` so Alembic can import the table definitions
without dragging in the Redis client and the rest of the runtime.

SQLAlchemy Core tables, not ORM models: these rows are written in bulk at one
per aircraft per second and read back as plain dictionaries. There is no object
graph to navigate, so there is nothing for an ORM to buy.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

#: One row per track, upserted on every update so it always holds current state.
tracks_table = Table(
    "tracks",
    metadata,
    Column("track_id", String(64), primary_key=True),
    Column("icao24", String(6), nullable=False, index=True),
    Column("callsign", String(16)),
    Column("scenario_id", String(64), index=True),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("last_seen_at", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False),
    Column("update_count", Integer, nullable=False),
)

#: Append-only position history.
#:
#: The unique constraint on (track_id, observed_at) is the project's idempotency
#: mechanism. At-least-once delivery means any report may be processed twice
#: after a consumer restart; the duplicate insert is dropped instead of
#: silently inflating the track. Without it a crash loop would corrupt history
#: in a way that is very hard to notice afterwards.
track_points_table = Table(
    "track_points",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("track_id", String(64), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("altitude_ft", Float, nullable=False),
    Column("ground_speed_kt", Float, nullable=False),
    Column("track_deg", Float, nullable=False),
    Column("vertical_rate_fpm", Float, nullable=False),
    Column("turn_rate_deg_s", Float, nullable=False),
    Column("position_uncertainty_m", Float, nullable=False),
    UniqueConstraint("track_id", "observed_at", name="uq_track_points_track_observed"),
    # History is always read as "the recent tail of one track", never as a
    # whole-table scan, so the index leads with track_id and descends by time.
    Index("ix_track_points_track_time", "track_id", "observed_at"),
)
