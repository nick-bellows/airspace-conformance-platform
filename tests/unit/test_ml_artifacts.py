"""Tests for artifact handling: version stamps, calibration, and training.

These cover the things an external review found missing. Each corresponds to a
way the system could be confidently wrong rather than visibly broken:

* a **stale artifact** trained under a different feature order loads cleanly and
  predicts nonsense forever;
* a **stale calibration** leaves alerting thresholds behind a retrained model,
  desensitising it with nothing to indicate that;
* the **training loop** itself was entirely unexercised, so a change that stopped
  it learning would have been caught only by noticing the numbers got worse.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from acp.ml.features import N_FEATURES, Standardiser
from acp.ml.models import (
    CALIBRATION_FILENAME,
    MODEL_VERSION,
    N_TARGETS,
    RidgeResidualModel,
    version_mismatch,
)
from acp.ml.predictor import TrajectoryPredictor
from acp.services.conformance.monitor import (
    FALLBACK_MODEL_ERROR_NM,
    FALLBACK_PHYSICS_ERROR_NM,
    Calibration,
)

torch = pytest.importorskip("torch", reason="the ml extra is not installed")
from acp.ml.neural import NeuralResidualModel  # noqa: E402


def a_ridge(tmp_path: Path) -> Path:
    rng = np.random.default_rng(5)
    features = rng.normal(size=(200, N_FEATURES))
    targets = rng.normal(size=(200, N_TARGETS))
    model = RidgeResidualModel.train(features, targets, Standardiser.fit(features))
    path = tmp_path / "residual_ridge_60s.json"
    model.save(path)
    return path


# --------------------------------------------------------------------------
# Version stamps are checked on load, not only written on save
# --------------------------------------------------------------------------


def test_a_matching_version_is_accepted() -> None:
    assert version_mismatch(MODEL_VERSION, Path("x.json")) is None


def test_a_missing_version_stamp_is_refused() -> None:
    problem = version_mismatch(None, Path("x.json"))
    assert problem is not None
    assert "no model_version" in problem


def test_a_mismatched_version_is_refused_and_names_both() -> None:
    problem = version_mismatch("acp-residual-v0", Path("x.json"))
    assert problem is not None
    assert "acp-residual-v0" in problem
    assert MODEL_VERSION in problem


def test_a_ridge_artifact_from_another_version_will_not_load(tmp_path: Path) -> None:
    """The failure this exists to prevent: a stale file loading confidently."""
    path = a_ridge(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_version"] = "acp-residual-v0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="acp-residual-v0"):
        RidgeResidualModel.load(path)


def test_a_ridge_artifact_with_the_wrong_width_will_not_load(tmp_path: Path) -> None:
    """A hand-edited file can carry the right stamp and the wrong shape."""
    path = a_ridge(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["coef"] = [row[:-1] for row in payload["coef"]]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="coefficients are"):
        RidgeResidualModel.load(path)


def test_a_neural_artifact_from_another_version_will_not_load(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    features = rng.normal(size=(300, N_FEATURES))
    targets = rng.normal(size=(300, N_TARGETS))
    model = NeuralResidualModel.train(features, targets, Standardiser.fit(features), epochs=1)
    path = tmp_path / "residual_neural_60s.pt"
    model.save(path)

    payload = torch.load(path, weights_only=True)
    payload["model_version"] = "acp-residual-v0"
    torch.save(payload, path)

    with pytest.raises(ValueError, match="acp-residual-v0"):
        NeuralResidualModel.load(path)


def test_a_version_mismatch_degrades_to_physics_rather_than_crashing(tmp_path: Path) -> None:
    """The service must still start. That is the whole degradation contract."""
    path = a_ridge(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_version"] = "acp-residual-v0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    predictor = TrajectoryPredictor(60.0, models_dir=tmp_path)
    assert not predictor.has_model


# --------------------------------------------------------------------------
# Calibration travels with the model
# --------------------------------------------------------------------------


def test_calibration_loads_from_the_artifact(tmp_path: Path) -> None:
    (tmp_path / CALIBRATION_FILENAME).write_text(
        json.dumps(
            {
                "model_version": MODEL_VERSION,
                "horizons": {
                    "60": {"model_median_nm": 0.9, "physics_median_nm": 2.0},
                    "120": {"model_median_nm": 2.1, "physics_median_nm": 4.5},
                },
            }
        ),
        encoding="utf-8",
    )
    calibration = Calibration.load(tmp_path)
    assert calibration.model_nm[60.0] == 0.9
    assert calibration.physics_nm[120.0] == 4.5
    assert "calibration.json" in calibration.source


def test_a_missing_calibration_falls_back_to_compiled_constants(tmp_path: Path) -> None:
    """A deployment without an artifact should have defensible thresholds
    rather than none -- and should say which it is using."""
    calibration = Calibration.load(tmp_path)
    assert calibration.model_nm == FALLBACK_MODEL_ERROR_NM
    assert calibration.physics_nm == FALLBACK_PHYSICS_ERROR_NM
    assert calibration.source == "compiled fallback"


def test_a_corrupt_calibration_falls_back_rather_than_crashing(tmp_path: Path) -> None:
    (tmp_path / CALIBRATION_FILENAME).write_text("{ not json", encoding="utf-8")
    assert Calibration.load(tmp_path).source == "compiled fallback"


def test_a_calibration_missing_its_fields_falls_back(tmp_path: Path) -> None:
    (tmp_path / CALIBRATION_FILENAME).write_text(
        json.dumps({"horizons": {"60": {"wrong_key": 1.0}}}), encoding="utf-8"
    )
    assert Calibration.load(tmp_path).source == "compiled fallback"


def test_the_committed_calibration_is_usable() -> None:
    """The artifact shipped in the repository must actually parse."""
    root = Path(__file__).resolve().parents[2]
    calibration = Calibration.load(root / "models")
    assert calibration.source.endswith(CALIBRATION_FILENAME)
    assert set(calibration.model_nm) == {30.0, 60.0, 120.0}
    # Physics should be worse than the model at every horizon, or the model is
    # not earning its place and the thresholds are the wrong way round.
    for horizon, model_error in calibration.model_nm.items():
        assert calibration.physics_nm[horizon] > model_error


# --------------------------------------------------------------------------
# The training loop actually learns
# --------------------------------------------------------------------------


def test_the_neural_loop_learns_a_non_linear_mapping() -> None:
    """A smoke test for the training path, which had no coverage at all.

    The target is deliberately non-linear in the features, so a model that
    merely converged to the mean -- or one whose optimiser was silently doing
    nothing -- would fail. Ridge is included as the control: the network has to
    beat the linear model on a relationship ridge structurally cannot fit.
    """
    rng = np.random.default_rng(17)
    features = rng.normal(size=(4000, N_FEATURES))
    targets = np.stack(
        [
            np.sin(features[:, 0]) * 2.0,
            features[:, 1] * features[:, 2],
            np.abs(features[:, 3]) * 3.0,
        ],
        axis=1,
    )
    standardiser = Standardiser.fit(features)
    split = 3000

    neural = NeuralResidualModel.train(
        features[:split],
        targets[:split],
        standardiser,
        validation=(features[split:], targets[split:]),
        epochs=60,
    )
    ridge = RidgeResidualModel.train(features[:split], targets[:split], standardiser)

    def error(model: object) -> float:
        predicted = model.predict(features[split:])  # type: ignore[attr-defined]
        return float(np.abs(predicted - targets[split:]).mean())

    assert error(neural) < error(ridge) * 0.7, "the network did not beat ridge on a non-linear map"
    assert neural.epochs_trained > 0


def test_early_stopping_reports_how_long_it_ran() -> None:
    rng = np.random.default_rng(23)
    features = rng.normal(size=(400, N_FEATURES))
    targets = rng.normal(size=(400, N_TARGETS))
    standardiser = Standardiser.fit(features)
    model = NeuralResidualModel.train(
        features[:300],
        targets[:300],
        standardiser,
        validation=(features[300:], targets[300:]),
        epochs=200,
        patience=2,
    )
    # Pure noise: validation loss stops improving almost immediately, so this
    # must stop well short of 200 epochs or early stopping is not working.
    assert 0 < model.epochs_trained < 200


def test_training_is_deterministic() -> None:
    """A model card that quotes an error figure has to be reproducible."""
    rng = np.random.default_rng(29)
    features = rng.normal(size=(500, N_FEATURES))
    targets = rng.normal(size=(500, N_TARGETS))
    standardiser = Standardiser.fit(features)

    first = NeuralResidualModel.train(features, targets, standardiser, epochs=5)
    second = NeuralResidualModel.train(features, targets, standardiser, epochs=5)
    assert np.allclose(first.predict(features), second.predict(features))


def test_a_neural_model_survives_a_save_and_load(tmp_path: Path) -> None:
    rng = np.random.default_rng(31)
    features = rng.normal(size=(300, N_FEATURES))
    targets = rng.normal(size=(300, N_TARGETS))
    model = NeuralResidualModel.train(features, targets, Standardiser.fit(features), epochs=3)
    path = tmp_path / "residual_neural_60s.pt"
    model.save(path)

    restored = NeuralResidualModel.load(path)
    assert np.allclose(model.predict(features), restored.predict(features))
    assert restored.parameter_count() == model.parameter_count()
