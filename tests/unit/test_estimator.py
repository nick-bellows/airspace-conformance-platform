"""Tests for track estimation and lifecycle.

M1's estimator does not filter, so there is nothing to assert about smoothing.
What it *does* own is the track lifecycle, and that logic survives into M2
unchanged -- so it is worth pinning now, before the Kalman filter arrives and
makes any regression harder to attribute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acp.common.contracts import SurveillanceReport, TrackState
from acp.services.track.estimator import (
    PLACEHOLDER_UNCERTAINTY_M,
    TrackEstimator,
    track_id_for,
)

START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def report(
    *,
    at: datetime = START,
    icao24: str = "a1b2c3",
    lat: float = 40.0,
    lon: float = -75.0,
    track_deg: float | None = 90.0,
    speed_kt: float | None = 450.0,
    altitude_ft: float | None = 35000.0,
    **extra: object,
) -> SurveillanceReport:
    return SurveillanceReport(
        report_id=f"r-{at.timestamp()}",
        icao24=icao24,
        callsign="TEST01",
        observed_at=at,
        lat=lat,
        lon=lon,
        altitude_baro_ft=altitude_ft,
        ground_speed_kt=speed_kt,
        track_deg=track_deg,
        **extra,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_a_first_report_initiates_rather_than_confirms() -> None:
    """One detection is not a track. Confirmation needs corroboration."""
    update = TrackEstimator().on_report(report())
    assert update.state is TrackState.INITIATING
    assert update.update_count == 1
    assert update.track_id == track_id_for("a1b2c3")


def test_a_track_is_confirmed_after_enough_updates() -> None:
    estimator = TrackEstimator(confirmation_updates=3)
    states = [
        estimator.on_report(report(at=START + timedelta(seconds=i), lon=-75.0 + i * 0.01)).state
        for i in range(3)
    ]
    assert states == [TrackState.INITIATING, TrackState.INITIATING, TrackState.CONFIRMED]


def test_a_silent_track_starts_coasting() -> None:
    estimator = TrackEstimator(coast_after_s=5.0, terminate_after_s=30.0)
    estimator.on_report(report())
    changed = estimator.sweep(START + timedelta(seconds=6))
    assert [u.state for u in changed] == [TrackState.COASTING]


def test_a_track_silent_for_long_enough_terminates() -> None:
    estimator = TrackEstimator(coast_after_s=5.0, terminate_after_s=30.0)
    estimator.on_report(report())
    assert estimator.live_track_count == 1
    changed = estimator.sweep(START + timedelta(seconds=31))
    assert [u.state for u in changed] == [TrackState.TERMINATED]
    assert estimator.live_track_count == 0


def test_a_sweep_reports_only_tracks_whose_state_changed() -> None:
    """Otherwise every sweep would republish the whole airspace picture."""
    estimator = TrackEstimator(coast_after_s=5.0)
    estimator.on_report(report())
    assert estimator.sweep(START + timedelta(seconds=6))  # initiating -> coasting
    assert estimator.sweep(START + timedelta(seconds=7)) == ()  # still coasting


def test_a_reappearing_aircraft_starts_a_fresh_track() -> None:
    """After termination, a new report is a new track rather than a resurrection."""
    estimator = TrackEstimator(terminate_after_s=30.0)
    estimator.on_report(report())
    estimator.sweep(START + timedelta(seconds=31))

    revived = estimator.on_report(report(at=START + timedelta(seconds=60)))
    assert revived.state is TrackState.INITIATING
    assert revived.update_count == 1


def test_terminated_tracks_are_eventually_dropped_from_memory() -> None:
    """A long-running tracker must not accumulate every aircraft it ever saw."""
    estimator = TrackEstimator(terminate_after_s=30.0)
    estimator.on_report(report())
    estimator.sweep(START + timedelta(seconds=31))
    removed = estimator.prune_terminated(
        START + timedelta(minutes=30), older_than=timedelta(minutes=10)
    )
    assert removed == 1


def test_pruning_leaves_live_tracks_alone() -> None:
    estimator = TrackEstimator()
    estimator.on_report(report())
    assert estimator.prune_terminated(START + timedelta(hours=1), older_than=timedelta(0)) == 0
    assert estimator.live_track_count == 1


# --------------------------------------------------------------------------
# Kinematics
# --------------------------------------------------------------------------


def test_turn_rate_is_derived_from_consecutive_headings() -> None:
    estimator = TrackEstimator()
    estimator.on_report(report(track_deg=90.0))
    update = estimator.on_report(report(at=START + timedelta(seconds=2), track_deg=100.0))
    assert update.turn_rate_deg_s == pytest.approx(5.0)


def test_turn_rate_takes_the_short_way_round_north() -> None:
    """A 350 -> 010 turn is +20 degrees, not -340.

    Without the signed shortest-turn helper this reads as a 170 deg/s turn,
    which would look like a violent manoeuvre every time an aircraft tracked
    through north.
    """
    estimator = TrackEstimator()
    estimator.on_report(report(track_deg=350.0))
    update = estimator.on_report(report(at=START + timedelta(seconds=2), track_deg=10.0))
    assert update.turn_rate_deg_s == pytest.approx(10.0)


def test_a_report_missing_speed_and_heading_falls_back_to_the_track() -> None:
    """Real surveillance drops fields; the estimate must still be complete."""
    estimator = TrackEstimator()
    estimator.on_report(report(lat=40.0, lon=-75.0))
    update = estimator.on_report(
        report(at=START + timedelta(seconds=10), lat=40.0, lon=-74.9, track_deg=None, speed_kt=None)
    )
    assert update.ground_speed_kt > 0.0  # derived from distance covered
    assert 80.0 < update.track_deg < 100.0  # derived bearing, roughly east


def test_a_report_missing_altitude_keeps_the_last_known_value() -> None:
    estimator = TrackEstimator()
    estimator.on_report(report(altitude_ft=35000.0))
    update = estimator.on_report(report(at=START + timedelta(seconds=2), altitude_ft=None))
    assert update.altitude_ft == 35000.0


def test_a_stationary_aircraft_does_not_produce_a_nonsense_heading() -> None:
    """Identical consecutive positions have no bearing; keep the previous one."""
    estimator = TrackEstimator()
    estimator.on_report(report(track_deg=270.0))
    update = estimator.on_report(report(at=START + timedelta(seconds=2), track_deg=None))
    assert update.track_deg == pytest.approx(270.0)


def test_an_out_of_order_report_is_ignored() -> None:
    """Per-aircraft partitioning should prevent this; accepting it would drag
    the track backwards, so it is dropped rather than trusted."""
    estimator = TrackEstimator()
    estimator.on_report(report(at=START + timedelta(seconds=10), lon=-75.0))
    update = estimator.on_report(report(at=START, lon=-70.0))
    assert update.lon == -75.0
    assert update.update_count == 1


# --------------------------------------------------------------------------
# Uncertainty
# --------------------------------------------------------------------------


def test_uncertainty_is_the_placeholder_while_reports_are_arriving() -> None:
    update = TrackEstimator().on_report(report())
    assert update.position_uncertainty_m == pytest.approx(PLACEHOLDER_UNCERTAINTY_M)


def test_uncertainty_grows_while_a_track_coasts() -> None:
    """A coasting track is a guess. It must not look as trustworthy as a fix."""
    estimator = TrackEstimator(coast_after_s=5.0)
    fresh = estimator.on_report(report())
    coasting = estimator.sweep(START + timedelta(seconds=10))[0]
    assert coasting.position_uncertainty_m > fresh.position_uncertainty_m


# --------------------------------------------------------------------------
# Multiple aircraft
# --------------------------------------------------------------------------


def test_tracks_are_kept_separate_per_aircraft() -> None:
    estimator = TrackEstimator()
    first = estimator.on_report(report(icao24="a1b2c3", lat=40.0))
    second = estimator.on_report(report(icao24="ffffff", lat=41.0))
    assert first.track_id != second.track_id
    assert estimator.live_track_count == 2
    assert (first.lat, second.lat) == (40.0, 41.0)


def test_the_same_report_twice_advances_the_track_only_once() -> None:
    """At-least-once delivery means this happens. The second copy has the same
    timestamp, so it is treated as out of order and ignored."""
    estimator = TrackEstimator()
    duplicate = report(at=START + timedelta(seconds=1))
    estimator.on_report(report())
    estimator.on_report(duplicate)
    update = estimator.on_report(duplicate)
    assert update.update_count == 2
