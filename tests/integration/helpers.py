"""Shared helpers for the integration suite."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from acp.common.contracts import DataSource, SurveillanceReport, TrackState, TrackUpdate


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


async def until[T](
    condition: Callable[[], Coroutine[Any, Any, T | None]],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.2,
    description: str = "condition",
) -> T:
    """Poll until a condition returns something truthy, or fail with a message.

    Sleeping a fixed amount and hoping is the standard way integration suites
    become flaky. This retries against a deadline and, on timeout, says what it
    was waiting for -- which is the difference between a five-minute debug and
    an hour of one.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        result = await condition()
        if result:
            return result
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {description}")


def a_report(
    *,
    icao24: str = "a1b2c3",
    at: datetime | None = None,
    lat: float = 40.0,
    lon: float = -75.0,
    sequence: int = 0,
) -> SurveillanceReport:
    moment = at or datetime.now(UTC)
    return SurveillanceReport(
        report_id=f"r-{icao24}-{sequence}",
        icao24=icao24,
        callsign="ITG001",
        observed_at=moment,
        lat=lat,
        lon=lon,
        altitude_baro_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=90.0,
        vertical_rate_fpm=0.0,
        source=DataSource.SIMULATOR,
        scenario_id="integration",
    )


def a_track_update(
    *,
    track_id: str = "trk-a1b2c3",
    icao24: str = "a1b2c3",
    at: datetime | None = None,
    lat: float = 40.0,
    lon: float = -75.0,
) -> TrackUpdate:
    moment = at or datetime.now(UTC)
    return TrackUpdate(
        track_id=track_id,
        icao24=icao24,
        callsign="ITG001",
        updated_at=moment,
        last_report_at=moment,
        state=TrackState.CONFIRMED,
        lat=lat,
        lon=lon,
        altitude_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=90.0,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=42,
        innovation_nm=0.01,
        source=DataSource.SIMULATOR,
        scenario_id="integration",
    )


def seconds_apart(count: int, *, start: datetime | None = None) -> list[datetime]:
    """Consecutive one-second timestamps, for building a plausible track."""
    base = start or datetime.now(UTC)
    return [base + timedelta(seconds=i) for i in range(count)]
