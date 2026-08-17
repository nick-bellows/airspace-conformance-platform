"""Tests for trajectory prediction.

Three things carry the whole ML result and are pinned here.

**The residual decomposition round-trips.** A sign error in the along/cross
transform would be invisible in the loss and would put every prediction on the
wrong side of the aircraft.

**Failure degrades to physics.** No artifact, a corrupt one, or a NaN output
must produce dead reckoning rather than nonsense or an exception, because the
conformance service consumes this in the same loop as conflict detection.

**Features cannot see intent.** If the model could reach the simulator's plans it
would be inverting the generator, and every number in the model card would be
worthless.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from acp.common.contracts import DataSource, TrackState, TrackUpdate
from acp.common.geodesy import haversine_nm
from acp.ml.baselines import (
    KinematicState,
    along_cross_track_error,
    apply_along_cross,
    constant_turn,
    dead_reckon,
    persistence,
)
from acp.ml.features import FEATURE_NAMES, N_FEATURES, WINDOW, Standardiser, extract, sanity_check
from acp.ml.models import N_TARGETS, RidgeResidualModel, ZeroModel
from acp.ml.predictor import TrajectoryPredictor

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_track(
    *,
    at: datetime = NOW,
    lat: float = 40.0,
    lon: float = -75.0,
    altitude_ft: float = 35000.0,
    speed_kt: float = 450.0,
    track_deg: float = 90.0,
    vertical_rate_fpm: float = 0.0,
    turn_rate_deg_s: float = 0.0,
    innovation_nm: float | None = 0.01,
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
        altitude_ft=altitude_ft,
        ground_speed_kt=speed_kt,
        track_deg=track_deg,
        vertical_rate_fpm=vertical_rate_fpm,
        turn_rate_deg_s=turn_rate_deg_s,
        position_uncertainty_m=30.0,
        update_count=50,
        innovation_nm=innovation_nm,
        source=DataSource.SIMULATOR,
    )


def a_window(count: int = WINDOW, **overrides: object) -> list[TrackUpdate]:
    return [a_track(at=NOW + timedelta(seconds=i), **overrides) for i in range(count)]  # type: ignore[arg-type]


def a_state(**overrides: object) -> KinematicState:
    fields: dict[str, object] = {
        "lat": 40.0,
        "lon": -75.0,
        "altitude_ft": 35000.0,
        "ground_speed_kt": 450.0,
        "track_deg": 90.0,
        "vertical_rate_fpm": 0.0,
        "turn_rate_deg_s": 0.0,
    }
    fields.update(overrides)
    return KinematicState(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------


def test_persistence_does_not_move_the_aircraft() -> None:
    prediction = persistence(a_state(), 60.0)
    assert (prediction.lat, prediction.lon) == (40.0, -75.0)


def test_dead_reckoning_covers_speed_times_time() -> None:
    prediction = dead_reckon(a_state(), 60.0)
    assert haversine_nm(40.0, -75.0, prediction.lat, prediction.lon) == pytest.approx(7.5, rel=1e-6)


def test_dead_reckoning_applies_the_vertical_rate() -> None:
    prediction = dead_reckon(a_state(vertical_rate_fpm=1800.0), 60.0)
    assert prediction.altitude_ft == pytest.approx(36800.0)


def test_altitude_never_goes_below_the_ground() -> None:
    prediction = dead_reckon(a_state(altitude_ft=500.0, vertical_rate_fpm=-6000.0), 60.0)
    assert prediction.altitude_ft == 0.0


def test_constant_turn_degenerates_to_dead_reckoning_when_straight() -> None:
    straight = a_state(turn_rate_deg_s=0.0)
    turned = constant_turn(straight, 60.0)
    reckoned = dead_reckon(straight, 60.0)
    assert (turned.lat, turned.lon) == (reckoned.lat, reckoned.lon)


def test_constant_turn_curves_away_from_dead_reckoning() -> None:
    turning = a_state(turn_rate_deg_s=3.0)
    turned = constant_turn(turning, 60.0)
    reckoned = dead_reckon(turning, 60.0)
    assert haversine_nm(turned.lat, turned.lon, reckoned.lat, reckoned.lon) > 1.0


def test_a_standard_rate_turn_completes_half_a_circle_in_a_minute() -> None:
    """3 deg/s for 60 s is 180 degrees. At 450 kt the radius is v/omega, so the
    aircraft should end up two radii from where it started."""
    turning = a_state(turn_rate_deg_s=3.0)
    result = constant_turn(turning, 60.0, steps=240)
    radius_nm = (450.0 / 3600.0) / math.radians(3.0)
    assert haversine_nm(40.0, -75.0, result.lat, result.lon) == pytest.approx(
        2 * radius_nm, rel=0.02
    )


# --------------------------------------------------------------------------
# The residual decomposition
# --------------------------------------------------------------------------


@pytest.mark.parametrize("track_deg", [0.0, 45.0, 90.0, 180.0, 270.0, 359.0])
@pytest.mark.parametrize(("along", "cross"), [(1.0, 0.0), (0.0, 1.0), (-2.0, 3.0), (0.5, -0.5)])
def test_the_residual_decomposition_round_trips(
    track_deg: float, along: float, cross: float
) -> None:
    """A sign error here is invisible in the loss and puts every prediction on
    the wrong side of the aircraft."""
    reference = dead_reckon(a_state(track_deg=track_deg), 60.0)
    lat, lon = apply_along_cross(reference, along, cross, track_deg)
    recovered_along, recovered_cross = along_cross_track_error(reference, lat, lon, track_deg)
    assert recovered_along == pytest.approx(along, abs=1e-6)
    assert recovered_cross == pytest.approx(cross, abs=1e-6)


def test_a_zero_residual_leaves_the_prediction_untouched() -> None:
    """The property the whole graceful-degradation story rests on."""
    reference = dead_reckon(a_state(), 60.0)
    lat, lon = apply_along_cross(reference, 0.0, 0.0, 90.0)
    assert (lat, lon) == (reference.lat, reference.lon)


def test_positive_cross_track_is_to_the_right() -> None:
    """Fixing the sign convention so a later refactor cannot quietly flip it."""
    reference = dead_reckon(a_state(track_deg=0.0), 60.0)  # heading north
    _, lon = apply_along_cross(reference, 0.0, 5.0, 0.0)
    assert lon > reference.lon  # right of north is east


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------


def test_the_feature_vector_has_the_declared_shape() -> None:
    features = extract(a_window())
    assert features.shape == (N_FEATURES,)
    assert len(FEATURE_NAMES) == N_FEATURES


def test_a_short_window_is_rejected_rather_than_padded() -> None:
    """Padding would silently change what the recent-dynamics features mean."""
    with pytest.raises(ValueError, match="need 20 updates"):
        extract(a_window(5))


def test_features_are_finite_for_ordinary_input() -> None:
    sanity_check(extract(a_window()).reshape(1, -1))


def test_a_missing_innovation_reads_as_no_surprise() -> None:
    """Optional on the wire: a track that has only ever coasted has none."""
    features = extract(a_window(innovation_nm=None))
    assert features[FEATURE_NAMES.index("innovation_nm")] == 0.0


def test_the_feature_extractor_only_accepts_track_updates() -> None:
    """The structural guarantee that a predictor cannot see the simulator's plan.

    `extract` takes a sequence of TrackUpdate and nothing else, and TrackUpdate
    is a wire contract with no intent fields on it. If a future change added one,
    this list would grow and the test would fail.
    """
    assert set(TrackUpdate.model_fields) == {
        "schema_version",
        "track_id",
        "icao24",
        "callsign",
        "updated_at",
        "last_report_at",
        "state",
        "lat",
        "lon",
        "altitude_ft",
        "ground_speed_kt",
        "track_deg",
        "vertical_rate_fpm",
        "turn_rate_deg_s",
        "position_uncertainty_m",
        # Filter covariance, not intent: derived from the observation stream
        # alone, and the reason the probabilistic detector can weigh a
        # prediction by how well the track is known.
        "velocity_uncertainty_kt",
        "altitude_uncertainty_ft",
        "vertical_rate_uncertainty_fpm",
        "update_count",
        "innovation_nm",
        "squawk",
        "source",
        "scenario_id",
    }


def test_sanity_check_rejects_a_non_finite_feature() -> None:
    features = extract(a_window()).reshape(1, -1)
    features[0, 3] = np.nan
    with pytest.raises(ValueError, match="non-finite"):
        sanity_check(features)


def test_sanity_check_rejects_the_wrong_width() -> None:
    with pytest.raises(ValueError, match="expected"):
        sanity_check(np.zeros((4, 3)))


# --------------------------------------------------------------------------
# Standardiser
# --------------------------------------------------------------------------


def test_standardising_produces_zero_mean_unit_variance() -> None:
    rng = np.random.default_rng(7)
    features = rng.normal(loc=5.0, scale=3.0, size=(500, N_FEATURES))
    scaled = Standardiser.fit(features).transform(features)
    assert np.allclose(scaled.mean(axis=0), 0.0, atol=1e-9)
    assert np.allclose(scaled.std(axis=0), 1.0, atol=1e-9)


def test_a_constant_feature_does_not_produce_nan() -> None:
    """Zero variance divides by zero and poisons every downstream weight."""
    features = np.ones((10, N_FEATURES))
    assert np.isfinite(Standardiser.fit(features).transform(features)).all()


def test_the_standardiser_survives_a_round_trip() -> None:
    rng = np.random.default_rng(11)
    original = Standardiser.fit(rng.normal(size=(100, N_FEATURES)))
    restored = Standardiser.from_dict(original.to_dict())
    assert np.allclose(original.mean, restored.mean)
    assert np.allclose(original.scale, restored.scale)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


def test_the_zero_model_is_dead_reckoning() -> None:
    residuals = ZeroModel().predict(np.zeros((5, N_FEATURES)))
    assert residuals.shape == (5, N_TARGETS)
    assert not residuals.any()


def test_ridge_learns_a_linear_relationship() -> None:
    rng = np.random.default_rng(3)
    features = rng.normal(size=(2000, N_FEATURES))
    targets = np.stack([features[:, 0] * 2.0, features[:, 1] * -1.5, features[:, 2] * 0.5], axis=1)
    standardiser = Standardiser.fit(features)
    model = RidgeResidualModel.train(features, targets, standardiser)
    assert np.abs(model.predict(features) - targets).max() < 0.2


def test_ridge_survives_a_save_and_load(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    features = rng.normal(size=(300, N_FEATURES))
    targets = rng.normal(size=(300, N_TARGETS))
    model = RidgeResidualModel.train(features, targets, Standardiser.fit(features))

    path = tmp_path / "ridge.json"
    model.save(path)
    restored = RidgeResidualModel.load(path)
    assert np.allclose(model.predict(features), restored.predict(features))


def test_ridge_coefficients_are_readable() -> None:
    """Part of the argument for shipping the linear model if it ever wins."""
    rng = np.random.default_rng(5)
    features = rng.normal(size=(200, N_FEATURES))
    model = RidgeResidualModel.train(
        features, rng.normal(size=(200, N_TARGETS)), Standardiser.fit(features)
    )
    coefficients = model.coefficients()
    assert set(coefficients) == {"along_track_nm", "cross_track_nm", "altitude_ft"}
    assert set(coefficients["along_track_nm"]) == set(FEATURE_NAMES)


# --------------------------------------------------------------------------
# The predictor degrades to physics
# --------------------------------------------------------------------------


def test_a_missing_artifact_falls_back_to_dead_reckoning(tmp_path: Path) -> None:
    """A model that is not there must not stop the service."""
    predictor = TrajectoryPredictor(60.0, models_dir=tmp_path)
    assert not predictor.has_model

    prediction = predictor.predict(a_window())
    reckoned = dead_reckon(a_state(), 60.0)
    assert prediction.source == "dead_reckoning"
    assert haversine_nm(prediction.lat, prediction.lon, reckoned.lat, reckoned.lon) < 1e-6


def test_a_corrupt_artifact_falls_back_to_dead_reckoning(tmp_path: Path) -> None:
    (tmp_path / "residual_ridge_60s.json").write_text("{ not json", encoding="utf-8")
    predictor = TrajectoryPredictor(60.0, models_dir=tmp_path)
    assert not predictor.has_model
    assert predictor.predict(a_window()).source == "dead_reckoning"


def test_a_short_window_falls_back_to_dead_reckoning() -> None:
    predictor = TrajectoryPredictor(60.0, model=ZeroModel())
    assert predictor.predict(a_window(3)).source == "dead_reckoning"


def test_a_model_producing_nan_falls_back_to_dead_reckoning() -> None:
    """A NaN would propagate into the alert evidence and anything comparing it."""

    class NaNModel:
        name = "nan"

        def predict(self, features: np.ndarray) -> np.ndarray:  # type: ignore[type-arg]
            return np.full((features.shape[0], N_TARGETS), np.nan)

    predictor = TrajectoryPredictor(60.0, model=NaNModel())
    assert predictor.predict(a_window()).source == "dead_reckoning"


def test_a_model_that_raises_falls_back_to_dead_reckoning() -> None:
    class ExplodingModel:
        name = "boom"

        def predict(self, features: np.ndarray) -> np.ndarray:  # type: ignore[type-arg]
            raise RuntimeError("inference exploded")

    predictor = TrajectoryPredictor(60.0, model=ExplodingModel())
    assert predictor.predict(a_window()).source == "dead_reckoning"


def test_a_zero_residual_model_reproduces_dead_reckoning_exactly() -> None:
    """The graceful-degradation guarantee, end to end through the serving path."""
    predictor = TrajectoryPredictor(60.0, model=ZeroModel())
    prediction = predictor.predict(a_window())
    reckoned = dead_reckon(a_state(), 60.0)
    assert prediction.source == "model"
    assert haversine_nm(prediction.lat, prediction.lon, reckoned.lat, reckoned.lon) < 1e-9


def test_the_prediction_records_where_it_came_from() -> None:
    """So an alert raised while the model was unavailable is identifiable later."""
    with_model = TrajectoryPredictor(60.0, model=ZeroModel()).predict(a_window())
    assert with_model.used_model
    assert "acp-residual" in with_model.predictor_version
