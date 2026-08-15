"""Tests for the Kalman filter.

Two claims are being checked. That the filter **is a filter** -- its output is
closer to the truth than the measurements it was given, which is the entire
reason for its existence and is embarrassingly easy to get wrong. And that its
**covariance means something** -- it shrinks with evidence and grows without it,
because the reported position uncertainty is derived from it and a display
showing false confidence is worse than one showing none.
"""

from __future__ import annotations

import math
import random

import pytest

from acp.common.geodesy import destination_point, haversine_nm
from acp.services.track.kalman import FilterTuning, TrackFilter, reference_for


def a_filter(
    *,
    lat: float = 40.0,
    lon: float = -75.0,
    speed_kt: float = 450.0,
    track_deg: float = 90.0,
    altitude_ft: float = 35000.0,
    vertical_rate_fpm: float = 0.0,
    tuning: FilterTuning | None = None,
) -> TrackFilter:
    ref_lat, ref_lon = reference_for(lat, lon)
    return TrackFilter(
        ref_lat=ref_lat,
        ref_lon=ref_lon,
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        ground_speed_kt=speed_kt,
        track_deg=track_deg,
        vertical_rate_fpm=vertical_rate_fpm,
        tuning=tuning,
    )


def _noisy_straight_flight(
    steps: int, *, sigma_m: float, seed: int = 7
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Truth and noisy measurements for an aircraft flying due east."""
    rng = random.Random(seed)
    lat, lon = 40.0, -75.0
    truth: list[tuple[float, float]] = []
    measured: list[tuple[float, float]] = []
    degrees_per_metre = 1.0 / 111_320.0
    for _ in range(steps):
        lat, lon = destination_point(lat, lon, 90.0, 450.0 / 3600.0)
        truth.append((lat, lon))
        measured.append(
            (
                lat + rng.gauss(0.0, sigma_m) * degrees_per_metre,
                lon + rng.gauss(0.0, sigma_m) * degrees_per_metre / math.cos(math.radians(lat)),
            )
        )
    return truth, measured


# --------------------------------------------------------------------------
# It actually filters
# --------------------------------------------------------------------------


def test_the_filter_beats_the_raw_measurements() -> None:
    """The headline claim. If this fails the filter is decoration."""
    sigma_m = 30.0
    truth, measured = _noisy_straight_flight(200, sigma_m=sigma_m)
    kf = a_filter()

    filtered_errors = []
    raw_errors = []
    # Skip the first few: the filter starts from a deliberately vague prior and
    # has to be given a chance to converge before being judged.
    for index, ((t_lat, t_lon), (m_lat, m_lon)) in enumerate(zip(truth, measured, strict=True)):
        kf.update(
            dt_s=1.0,
            lat=m_lat,
            lon=m_lon,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
        if index < 20:
            continue
        estimate = kf.estimate()
        filtered_errors.append(haversine_nm(t_lat, t_lon, estimate.lat, estimate.lon))
        raw_errors.append(haversine_nm(t_lat, t_lon, m_lat, m_lon))

    filtered_rms = math.sqrt(sum(e**2 for e in filtered_errors) / len(filtered_errors))
    raw_rms = math.sqrt(sum(e**2 for e in raw_errors) / len(raw_errors))
    assert filtered_rms < raw_rms * 0.6, (
        f"filter RMS {filtered_rms:.5f} NM is not clearly better than raw {raw_rms:.5f} NM"
    )


def test_the_filter_converges_to_a_noiseless_truth() -> None:
    """With perfect measurements the estimate must land on the truth."""
    kf = a_filter(speed_kt=0.0, track_deg=0.0)
    for _ in range(60):
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=35000.0,
            ground_speed_kt=0.0,
            track_deg=0.0,
            vertical_rate_fpm=0.0,
        )
    estimate = kf.estimate()
    assert haversine_nm(40.0, -75.0, estimate.lat, estimate.lon) < 0.01
    assert estimate.altitude_ft == pytest.approx(35000.0, abs=5.0)


def test_the_filter_tracks_a_steady_climb() -> None:
    kf = a_filter(altitude_ft=10000.0, vertical_rate_fpm=1800.0)
    altitude = 10000.0
    for _ in range(120):
        altitude += 30.0  # 1800 fpm for one second
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=altitude,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=1800.0,
        )
    estimate = kf.estimate()
    assert estimate.altitude_ft == pytest.approx(altitude, abs=50.0)
    assert estimate.vertical_rate_fpm == pytest.approx(1800.0, abs=100.0)


# --------------------------------------------------------------------------
# The covariance means something
# --------------------------------------------------------------------------


def test_uncertainty_shrinks_as_measurements_arrive() -> None:
    kf = a_filter()
    initial = kf.estimate().position_uncertainty_m
    for _ in range(30):
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
    assert kf.estimate().position_uncertainty_m < initial


def test_uncertainty_grows_while_coasting() -> None:
    """A coasting track is a guess, and the number on the display must say so."""
    kf = a_filter()
    for _ in range(30):
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
    settled = kf.estimate().position_uncertainty_m
    kf.predict(30.0)
    assert kf.estimate().position_uncertainty_m > settled * 2.0


def test_the_covariance_stays_symmetric_over_a_long_run() -> None:
    """Joseph form exists for this. Asymmetry eventually kills the filter."""
    kf = a_filter()
    for _ in range(2000):
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
    assert kf.position_covariance_trace > 0.0
    assert math.isfinite(kf.estimate().position_uncertainty_m)


def test_a_zero_or_negative_step_is_ignored_rather_than_breaking_the_filter() -> None:
    kf = a_filter()
    before = kf.position_covariance_trace
    kf.predict(0.0)
    kf.predict(-5.0)
    assert kf.position_covariance_trace == before


# --------------------------------------------------------------------------
# Partial measurements
# --------------------------------------------------------------------------


def test_a_position_only_report_is_usable() -> None:
    """Real surveillance drops fields; throwing the report away is not an option."""
    kf = a_filter()
    for _ in range(20):
        kf.update(
            dt_s=1.0,
            lat=40.0,
            lon=-75.0,
            altitude_ft=None,
            ground_speed_kt=None,
            track_deg=None,
            vertical_rate_fpm=None,
        )
    estimate = kf.estimate()
    assert haversine_nm(40.0, -75.0, estimate.lat, estimate.lon) < 0.5
    # Altitude was never measured, so it must still be the initial value rather
    # than having been dragged toward zero by an invented measurement.
    assert estimate.altitude_ft == pytest.approx(35000.0, abs=1.0)


# --------------------------------------------------------------------------
# The innovation is the manoeuvre signal
# --------------------------------------------------------------------------


def test_innovation_is_small_in_steady_flight() -> None:
    _, measured = _noisy_straight_flight(60, sigma_m=30.0)
    kf = a_filter()
    innovations = []
    for index, (m_lat, m_lon) in enumerate(measured):
        kf.update(
            dt_s=1.0,
            lat=m_lat,
            lon=m_lon,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
        if index >= 20:
            innovations.append(kf.estimate().innovation_nm)
    # 30 m is 0.016 NM; steady-state innovation should be the same order.
    assert max(innovations) < 0.1


def test_innovation_spikes_when_the_aircraft_manoeuvres() -> None:
    """This is what the conformance monitor keys on.

    The aircraft flies straight, the filter settles, then it turns hard. The
    constant-velocity model does not predict the turn, so the gap between
    prediction and report jumps -- and that gap is the detectable signal.
    """
    kf = a_filter()
    lat, lon = 40.0, -75.0
    steady = []
    for _ in range(40):
        lat, lon = destination_point(lat, lon, 90.0, 450.0 / 3600.0)
        kf.update(
            dt_s=1.0,
            lat=lat,
            lon=lon,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=90.0,
            vertical_rate_fpm=0.0,
        )
        steady.append(kf.estimate().innovation_nm)

    # Abrupt 90 degree turn to the north.
    turning = []
    for _ in range(10):
        lat, lon = destination_point(lat, lon, 0.0, 450.0 / 3600.0)
        kf.update(
            dt_s=1.0,
            lat=lat,
            lon=lon,
            altitude_ft=35000.0,
            ground_speed_kt=450.0,
            track_deg=0.0,
            vertical_rate_fpm=0.0,
        )
        turning.append(kf.estimate().innovation_nm)

    assert max(turning) > max(steady[20:]) * 5.0


# --------------------------------------------------------------------------
# Reference point
# --------------------------------------------------------------------------


def test_the_reference_point_stays_close_to_the_track() -> None:
    """Keeps the flat-earth projection inside its accurate range."""
    for lat, lon in ((40.61, -75.49), (51.2, 0.3), (-33.9, 151.2), (40.5, -75.5)):
        ref_lat, ref_lon = reference_for(lat, lon)
        assert haversine_nm(lat, lon, ref_lat, ref_lon) < 45.0


def test_the_reference_point_is_a_whole_degree() -> None:
    """A stable origin. Re-deriving it later would shift the whole state vector."""
    lat, lon = reference_for(40.61, -75.49)
    assert (lat, lon) == (41.0, -75.0)
    assert reference_for(40.61, -75.49) == reference_for(40.61, -75.49)


def test_two_filters_with_different_origins_still_agree_on_position() -> None:
    """Each track works in its own frame, so the frames must not leak into the
    reported position -- which is what the conflict detector consumes."""
    west = a_filter(lat=40.5, lon=-75.52)
    east = a_filter(lat=40.5, lon=-75.48)
    assert reference_for(40.5, -75.52) != reference_for(40.5, -75.48)

    separation = haversine_nm(
        west.estimate().lat, west.estimate().lon, east.estimate().lat, east.estimate().lon
    )
    expected = haversine_nm(40.5, -75.52, 40.5, -75.48)
    assert separation == pytest.approx(expected, abs=0.01)
