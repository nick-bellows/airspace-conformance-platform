"""Serving side of trajectory prediction.

Loads whichever model the training run shipped and turns a window of track
updates into a predicted position. Two properties are load-bearing at runtime:

**It always answers.** If no model artifact is present, if the artifact fails to
load, or if the track has too little history, the predictor returns the
dead-reckoning prediction and says so in `source`. A missing model degrades the
system to physics rather than stopping it. In a monitoring system that is the
right failure: an advisory that is merely less clever still beats no advisory.

**It never silently substitutes.** The returned prediction carries which path
produced it, and the conformance monitor puts that in the alert evidence, so an
alert raised while the model was unavailable is identifiable afterwards.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from acp.common.contracts import TrackUpdate
from acp.common.logging import get_logger
from acp.common.metrics import METRICS
from acp.ml.baselines import Prediction, apply_along_cross, dead_reckon
from acp.ml.features import WINDOW, extract, state_from
from acp.ml.models import MODEL_VERSION, ResidualModel, RidgeResidualModel

_log = get_logger(__name__)

DEFAULT_MODELS_DIR = Path("models")

PredictionSource = Literal["model", "dead_reckoning"]


@dataclass(frozen=True, slots=True)
class TrajectoryPrediction:
    """A predicted position, and an honest account of where it came from."""

    lat: float
    lon: float
    altitude_ft: float
    horizon_s: float
    source: PredictionSource
    predictor_version: str

    @property
    def used_model(self) -> bool:
        return self.source == "model"


def _artifact_for(models_dir: Path, horizon_s: float) -> tuple[Path, str] | None:
    """Find the shipped artifact for a horizon, whichever kind won."""
    neural = models_dir / f"residual_neural_{int(horizon_s)}s.pt"
    ridge = models_dir / f"residual_ridge_{int(horizon_s)}s.json"
    if neural.exists():
        return neural, "neural"
    if ridge.exists():
        return ridge, "ridge"
    return None


class TrajectoryPredictor:
    """Predicts a position at a fixed horizon from a window of track updates."""

    def __init__(
        self,
        horizon_s: float,
        *,
        model: ResidualModel | None = None,
        models_dir: Path = DEFAULT_MODELS_DIR,
    ) -> None:
        self._horizon_s = horizon_s
        self._model = model if model is not None else self._load(models_dir, horizon_s)
        self._version = (
            f"{MODEL_VERSION}:{self._model.name}" if self._model else f"{MODEL_VERSION}:physics"
        )

    @staticmethod
    def _load(models_dir: Path, horizon_s: float) -> ResidualModel | None:
        found = _artifact_for(models_dir, horizon_s)
        if found is None:
            _log.warning(
                "no trajectory model artifact; falling back to dead reckoning",
                extra={"horizon_s": horizon_s, "models_dir": str(models_dir)},
            )
            return None

        path, kind = found
        try:
            if kind == "neural":
                # Imported here, not at module scope, so that an installation
                # without the `ml` extra can still run the service on physics.
                # A top-level torch import made the documented "degrades to
                # dead reckoning" promise false: the process died at import
                # rather than starting and logging.
                from acp.ml.neural import NeuralResidualModel

                return NeuralResidualModel.load(path)
            return RidgeResidualModel.load(path)
        except Exception:  # noqa: BLE001 - see below
            # Deliberately blind, and ImportError is one of the cases it must
            # cover. A corrupt artifact, a version mismatch, or a missing
            # machine-learning dependency must not stop the service starting;
            # each degrades to physics and logs loudly. Refusing to boot
            # because a model file is bad would take conflict detection down
            # with it.
            _log.exception("failed to load trajectory model; falling back to dead reckoning")
            return None

    @property
    def version(self) -> str:
        return self._version

    @property
    def horizon_s(self) -> float:
        return self._horizon_s

    @property
    def has_model(self) -> bool:
        return self._model is not None

    def predict(self, window: Sequence[TrackUpdate]) -> TrajectoryPrediction:
        """Predict where the aircraft in this window will be at the horizon."""
        prediction = self._predict(window)
        # Counted by source, because a permanent silent fallback to physics is
        # exactly the degradation that goes unnoticed for months. A dashboard
        # showing 100% `dead_reckoning` is the signal.
        METRICS.predictions.labels(service="conformance", source=prediction.source).inc()
        return prediction

    def _predict(self, window: Sequence[TrackUpdate]) -> TrajectoryPrediction:
        current = window[-1]
        baseline = dead_reckon(state_from(current), self._horizon_s)

        if self._model is None or len(window) < WINDOW:
            return self._as_prediction(baseline, "dead_reckoning")

        try:
            residual = self._model.predict(extract(window).reshape(1, -1))[0]
        except Exception:  # noqa: BLE001 - inference must never take the service down
            _log.exception("trajectory model inference failed; using dead reckoning")
            return self._as_prediction(baseline, "dead_reckoning")

        if not np.isfinite(residual).all():
            # A NaN would propagate into the alert evidence and into anything
            # that compared against it. Physics is the safe answer.
            _log.warning("model produced a non-finite residual; using dead reckoning")
            return self._as_prediction(baseline, "dead_reckoning")

        lat, lon = apply_along_cross(
            baseline, float(residual[0]), float(residual[1]), current.track_deg
        )
        return TrajectoryPrediction(
            lat=lat,
            lon=lon,
            altitude_ft=max(0.0, baseline.altitude_ft + float(residual[2])),
            horizon_s=self._horizon_s,
            source="model",
            predictor_version=self._version,
        )

    def _as_prediction(
        self, baseline: Prediction, source: PredictionSource
    ) -> TrajectoryPrediction:
        return TrajectoryPrediction(
            lat=baseline.lat,
            lon=baseline.lon,
            altitude_ft=baseline.altitude_ft,
            horizon_s=self._horizon_s,
            source=source,
            predictor_version=self._version,
        )
