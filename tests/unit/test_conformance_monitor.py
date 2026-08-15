"""Tests for conformance monitoring.

The monitor's job is to remember a prediction, wait a minute, and check it. Most
of the risk is in the bookkeeping rather than the geometry: a prediction matched
against the wrong moment, or never matched at all, produces alerts about nothing
or silence about something.

The threshold behaviour is the other half. It is *relative* to how wrong physics
turned out to be, so that a turning aircraft -- which always beats its
dead-reckoning prediction badly -- is not permanently alerting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from acp.common.contracts import AlertKind, DataSource, Severity, TrackState, TrackUpdate
from acp.common.geodesy import destination_point
from acp.ml.models import ZeroModel
from acp.ml.predictor import TrajectoryPredictor
from acp.services.conformance.monitor import ConformanceMonitor

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)
HORIZON = 60.0


def a_track(
    *, at: datetime, lat: float = 40.0, lon: float = -75.0, track_deg: float = 90.0
) -> TrackUpdate:
    return TrackUpdate(
        track_id="trk-a1b2c3",
        icao24="a1b2c3",
        callsign="ACP101",
        updated_at=at,
        last_report_at=at,
        state=TrackState.CONFIRMED,
        lat=lat,
        lon=lon,
        altitude_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=track_deg,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=50,
        innovation_nm=0.01,
        source=DataSource.SIMULATOR,
    )


def a_monitor(**kwargs: object) -> ConformanceMonitor:
    # ZeroModel makes the predictor exactly dead reckoning, so the tests are
    # about the monitor's logic rather than about whatever the trained model
    # happens to output today.
    return ConformanceMonitor(
        TrajectoryPredictor(HORIZON, model=ZeroModel()),
        horizon_s=HORIZON,
        **kwargs,  # type: ignore[arg-type]
    )


def _fly_straight(monitor: ConformanceMonitor, seconds: int, *, start_lon: float = -75.0) -> None:
    """Feed the monitor an aircraft doing exactly what physics predicts."""
    lat, lon = 40.0, start_lon
    for second in range(seconds):
        monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        lat, lon = destination_point(lat, lon, 90.0, 450.0 / 3600.0)


# --------------------------------------------------------------------------
# The conforming case
# --------------------------------------------------------------------------


def test_an_aircraft_that_flies_as_predicted_raises_nothing() -> None:
    monitor = a_monitor()
    findings = []
    lat, lon = 40.0, -75.0
    for second in range(150):
        finding = monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        if finding is not None:
            findings.append(finding)
        lat, lon = destination_point(lat, lon, 90.0, 450.0 / 3600.0)
    assert findings == []


def test_no_finding_before_the_horizon_has_elapsed() -> None:
    """Nothing can be checked until the prediction matures."""
    monitor = a_monitor()
    _fly_straight(monitor, 30)
    assert monitor.tracked == 1


# --------------------------------------------------------------------------
# The diverging case
# --------------------------------------------------------------------------


def test_an_aircraft_that_diverges_is_flagged() -> None:
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    findings = []

    for second in range(200):
        finding = monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        if finding is not None:
            findings.append(finding)
        # A hard left turn at t=60, unannounced. Dead reckoning keeps predicting
        # due east; the aircraft goes north.
        bearing = 90.0 if second < 60 else 0.0
        lat, lon = destination_point(lat, lon, bearing, 450.0 / 3600.0)

    assert findings, "an unannounced 90 degree turn should not conform"
    assert findings[0].error_nm > findings[0].threshold_nm


def test_a_finding_carries_the_evidence_that_explains_it() -> None:
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    finding = None
    for second in range(200):
        result = monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        finding = finding or result
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 0.0, 450.0 / 3600.0)

    assert finding is not None
    evidence = finding.as_evidence()
    assert evidence.horizon_s == HORIZON
    assert evidence.error_nm > 0.0
    assert evidence.threshold_nm > 0.0
    assert evidence.predicted_at < NOW + timedelta(seconds=200)
    assert "acp-residual" in evidence.predictor_version


# --------------------------------------------------------------------------
# Severity and wording
# --------------------------------------------------------------------------


def test_a_non_conformance_is_only_ever_advisory() -> None:
    """It says the aircraft surprised our model, not that anything is wrong.

    Escalating that to a warning would imply a judgement about the flight that
    this system, with no flight plan, is in no position to make.
    """
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    finding = None
    for second in range(200):
        finding = finding or monitor.observe(
            a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon)
        )
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 0.0, 450.0 / 3600.0)

    assert finding is not None
    assert finding.severity is Severity.ADVISORY
    assert str(AlertKind.NON_CONFORMANCE) in finding.key


def test_the_summary_says_it_might_be_perfectly_normal() -> None:
    """The caveat belongs in the alert an operator reads, not only in the docs."""
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    finding = None
    for second in range(200):
        finding = finding or monitor.observe(
            a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon)
        )
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 0.0, 450.0 / 3600.0)

    assert finding is not None
    assert "no flight plan" in finding.summary
    assert "ACP101" in finding.summary


def test_reason_codes_record_whether_the_model_was_used() -> None:
    """An alert raised while the model was unavailable must be identifiable."""
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    finding = None
    for second in range(200):
        finding = finding or monitor.observe(
            a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon)
        )
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 0.0, 450.0 / 3600.0)

    assert finding is not None
    assert "model_prediction" in finding.reason_codes
    assert "prediction_error_above_threshold" in finding.reason_codes


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------


def test_a_small_deviation_does_not_alert() -> None:
    """A gentle heading change is normal and must stay below the threshold."""
    monitor = a_monitor()
    lat, lon = 40.0, -75.0
    findings = []
    for second in range(200):
        finding = monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        if finding is not None:
            findings.append(finding)
        # 5 degrees off: about 0.65 NM of cross-track over 60 s, well under the
        # calibrated 3.7 NM threshold at this horizon.
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 85.0, 450.0 / 3600.0)
    assert findings == []


def test_a_higher_threshold_factor_suppresses_marginal_findings() -> None:
    def count(factor: float) -> int:
        monitor = a_monitor(threshold_factor=factor)
        lat, lon = 40.0, -75.0
        found = 0
        for second in range(200):
            if monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon)):
                found += 1
            lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 40.0, 450.0 / 3600.0)
        return found

    assert count(1.0) > count(50.0)


def test_the_physics_fallback_can_still_raise_a_finding() -> None:
    """The regression this threshold design exists to prevent.

    An earlier version scaled the threshold by the same sample's dead-reckoning
    error. When the predictor falls back to physics the two are identical, so
    the comparison became `error > 3 * error` and conformance monitoring
    silently stopped working exactly when the model was unavailable.
    """
    monitor = ConformanceMonitor(
        TrajectoryPredictor(HORIZON, models_dir=Path("nonexistent-models-dir")),
        horizon_s=HORIZON,
    )
    lat, lon = 40.0, -75.0
    findings = []
    for second in range(200):
        finding = monitor.observe(a_track(at=NOW + timedelta(seconds=second), lat=lat, lon=lon))
        if finding is not None:
            findings.append(finding)
        lat, lon = destination_point(lat, lon, 90.0 if second < 60 else 0.0, 450.0 / 3600.0)

    assert findings, "physics-only mode must still detect a gross divergence"
    assert "physics_prediction_only" in findings[0].reason_codes


def test_the_physics_threshold_is_looser_than_the_model_threshold() -> None:
    """Physics is expected to be wrong more often, so holding it to the model's
    standard would flood on every turn."""
    monitor = a_monitor()
    assert monitor._threshold_for(used_model=False) > monitor._threshold_for(used_model=True)


def test_an_uncalibrated_horizon_falls_back_to_the_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Interpolating between measured figures would invent a number."""
    monitor = ConformanceMonitor(
        TrajectoryPredictor(45.0, model=ZeroModel()), horizon_s=45.0, minimum_threshold_nm=2.0
    )
    with caplog.at_level("WARNING"):
        assert monitor._threshold_for(used_model=True) == 2.0
    assert any("no calibrated typical error" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


def test_state_is_dropped_when_a_track_is_forgotten() -> None:
    """A long-running service must not accumulate every aircraft it ever saw."""
    monitor = a_monitor()
    _fly_straight(monitor, 30)
    assert monitor.tracked == 1
    monitor.forget(["trk-a1b2c3"])
    assert monitor.tracked == 0


def test_forgetting_an_unknown_track_is_harmless() -> None:
    monitor = a_monitor()
    _fly_straight(monitor, 30)
    monitor.forget(["trk-nonexistent"])
    assert monitor.tracked == 1


def test_a_gap_in_reports_does_not_produce_a_spurious_finding() -> None:
    """A prediction whose moment passed unobserved is discarded, not matched
    against whatever arrives next -- which could be minutes later and anywhere."""
    monitor = a_monitor()
    _fly_straight(monitor, 20)
    # Reappears 10 minutes later, far away. That is a dropout, not a divergence.
    far_lat, far_lon = destination_point(40.0, -75.0, 90.0, 75.0)
    finding = monitor.observe(a_track(at=NOW + timedelta(seconds=620), lat=far_lat, lon=far_lon))
    assert finding is None
