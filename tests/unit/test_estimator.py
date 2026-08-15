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
from acp.services.track.estimator import TrackEstimator, track_id_for

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


def test_turn_rate_is_reported_when_an_aircraft_turns() -> None:
    """Derived from *filtered* headings, so the sign and rough size are what
    matter rather than an exact figure - the filter deliberately lags a turn."""
    estimator = TrackEstimator()
    for i in range(20):
        estimator.on_report(report(at=START + timedelta(seconds=i), track_deg=90.0))
    turning = [
        estimator.on_report(report(at=START + timedelta(seconds=20 + i), track_deg=90.0 + i * 3.0))
        for i in range(1, 10)
    ]
    assert max(u.turn_rate_deg_s for u in turning) > 0.5


def test_turn_rate_takes_the_short_way_round_north() -> None:
    """A track crossing north must not register as a 359 deg/s turn.

    Without the signed shortest-turn helper this reads as a violent manoeuvre
    every single time an aircraft tracks through 360.
    """
    estimator = TrackEstimator()
    updates = []
    for i in range(40):
        heading = (350.0 + i) % 360.0
        updates.append(
            estimator.on_report(report(at=START + timedelta(seconds=i), track_deg=heading))
        )
    assert max(abs(u.turn_rate_deg_s) for u in updates[5:]) < 10.0


def test_a_report_missing_speed_and_heading_still_produces_an_estimate() -> None:
    """Real surveillance drops fields. The filter carries its velocity estimate
    forward rather than inventing a measurement or discarding the report."""
    estimator = TrackEstimator()
    for i in range(10):
        estimator.on_report(report(at=START + timedelta(seconds=i)))
    update = estimator.on_report(
        report(at=START + timedelta(seconds=10), track_deg=None, speed_kt=None)
    )
    assert update.ground_speed_kt > 0.0
    assert 45.0 < update.track_deg < 135.0  # still heading roughly east


def test_a_report_missing_altitude_keeps_the_last_known_value() -> None:
    estimator = TrackEstimator()
    for i in range(10):
        estimator.on_report(report(at=START + timedelta(seconds=i), altitude_ft=35000.0))
    update = estimator.on_report(report(at=START + timedelta(seconds=10), altitude_ft=None))
    assert update.altitude_ft == pytest.approx(35000.0, abs=100.0)


def test_the_estimate_is_smoother_than_the_reports() -> None:
    """The reason the filter exists, asserted at the estimator boundary.

    Reported altitude is jittered by 40 ft either side of a level aircraft; the
    published estimate must not follow it.
    """
    estimator = TrackEstimator()
    published = []
    for i in range(60):
        jitter = 40.0 if i % 2 else -40.0
        published.append(
            estimator.on_report(
                report(at=START + timedelta(seconds=i), altitude_ft=35000.0 + jitter)
            ).altitude_ft
        )
    settled = published[20:]
    assert max(settled) - min(settled) < 40.0


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


def test_uncertainty_falls_as_the_track_is_corroborated() -> None:
    """Now a computed covariance rather than M1's constant, so it responds."""
    estimator = TrackEstimator()
    first = estimator.on_report(report())
    for i in range(1, 30):
        last = estimator.on_report(report(at=START + timedelta(seconds=i)))
    assert last.position_uncertainty_m < first.position_uncertainty_m


def test_uncertainty_grows_while_a_track_coasts() -> None:
    """A coasting track is a guess. It must not look as trustworthy as a fix.

    This falls out of the filter running on prediction alone: process noise
    accumulates with no measurement to check it. No ad-hoc growth constant.
    """
    estimator = TrackEstimator(coast_after_s=5.0)
    for i in range(20):
        settled = estimator.on_report(report(at=START + timedelta(seconds=i)))
    coasting = estimator.sweep(START + timedelta(seconds=40))[0]
    assert coasting.position_uncertainty_m > settled.position_uncertainty_m * 2.0


def test_the_innovation_travels_downstream() -> None:
    """The conformance service reads this off the wire as the manoeuvre signal."""
    estimator = TrackEstimator()
    for i in range(10):
        update = estimator.on_report(report(at=START + timedelta(seconds=i)))
    assert update.innovation_nm is not None
    assert update.innovation_nm == pytest.approx(estimator.innovation_for("a1b2c3"))


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
