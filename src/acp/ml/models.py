"""Residual models: ridge regression, and the interface the serving path uses.

The PyTorch model lives in `neural.py` so that torch stays an optional
dependency -- see that module for why. Nothing here imports torch, and scikit-learn
is imported inside the methods that need it, so this module can be imported by a
service that has neither installed.

## Why residual rather than direct prediction

A model that predicted absolute position would have to learn the physics as well
as the corrections, and its failure mode would be unbounded: a bad output puts
the aircraft anywhere. A residual model that outputs zero degrades **exactly** to
dead reckoning, which is a known, sane, well-understood prediction. The worst
case is the baseline rather than nonsense.

It also makes the learning problem far easier. Predicting where an aircraft will
be in a minute is mostly arithmetic; predicting how it will deviate from that
arithmetic is the only part that needs data.

## Why ridge is here at all

It is the control. If the neural network cannot clearly beat a linear model on
the same features, then the extra capacity is not buying anything, and the
honest engineering answer is to ship the linear model -- smaller, faster,
inspectable, and with weights a reviewer can read. The training CLI picks a
winner on validation data and the model card reports which one won and by how
much, including when the answer is "the linear one".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from acp.ml.features import FEATURE_NAMES, N_FEATURES, Standardiser

#: Bump when a model's architecture or training procedure changes.
MODEL_VERSION = "acp-residual-v1"

#: Typical-error figures written beside the model artifacts by the training run
#: and read by the conformance monitor. Declared here, in the module with no
#: heavy dependencies, so the monitor can find the name without importing the
#: training code -- which would drag torch back into the serving path and undo
#: the whole point of `neural.py` being separate.
CALIBRATION_FILENAME = "calibration.json"

N_TARGETS = 3
TARGET_NAMES = ("along_track_nm", "cross_track_nm", "altitude_ft")


def version_mismatch(stamped: object, path: Path) -> str | None:
    """Describe a version problem with an artifact, or None if it is fine.

    Artifacts carry the version they were trained under. Checking it on *load*
    rather than only stamping it on save is what stops a stale file -- trained
    against a different feature order, or a different target definition --
    loading cleanly and predicting confidently forever. The degradation path
    already catches crashes and NaNs; version skew produces neither.
    """
    if stamped is None:
        return f"{path.name}: no model_version stamp; refusing to load"
    if stamped != MODEL_VERSION:
        return f"{path.name}: trained as {stamped!r}, this build expects {MODEL_VERSION!r}"
    return None


class ResidualModel(Protocol):
    """Anything that can correct a dead-reckoning prediction."""

    name: str

    def predict(self, features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Map (n, N_FEATURES) to (n, 3) residuals."""
        ...


@dataclass
class ZeroModel:
    """Outputs no correction. Literally dead reckoning, wearing the model API.

    Present so the evaluation can run the baseline through the identical code
    path as the models. A baseline measured by a different path than the model
    is a baseline you cannot trust.
    """

    name: str = "dead_reckoning"

    def predict(self, features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return np.zeros((features.shape[0], N_TARGETS), dtype=np.float64)


@dataclass
class RidgeResidualModel:
    """Linear least squares with L2 regularisation, on standardised features."""

    standardiser: Standardiser
    model: Any
    name: str = "ridge"

    @classmethod
    def train(
        cls,
        features: npt.NDArray[np.float64],
        targets: npt.NDArray[np.float64],
        standardiser: Standardiser,
        *,
        alpha: float = 1.0,
    ) -> RidgeResidualModel:
        from sklearn.linear_model import Ridge

        ridge = Ridge(alpha=alpha)
        ridge.fit(standardiser.transform(features), targets)
        return cls(standardiser=standardiser, model=ridge)

    def predict(self, features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return np.asarray(
            self.model.predict(self.standardiser.transform(features)), dtype=np.float64
        )

    def coefficients(self) -> dict[str, dict[str, float]]:
        """Per-target feature weights, for the model card.

        A linear model that can be read is part of the argument for shipping it.
        """
        return {
            target: {
                name: round(float(weight), 4)
                for name, weight in zip(FEATURE_NAMES, self.model.coef_[index], strict=True)
            }
            for index, target in enumerate(TARGET_NAMES)
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "model_version": MODEL_VERSION,
                    "kind": "ridge",
                    "n_features": N_FEATURES,
                    "standardiser": self.standardiser.to_dict(),
                    "coef": self.model.coef_.tolist(),
                    "intercept": self.model.intercept_.tolist(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> RidgeResidualModel:
        from sklearn.linear_model import Ridge

        payload = json.loads(path.read_text(encoding="utf-8"))
        problem = version_mismatch(payload.get("model_version"), path)
        if problem is not None:
            raise ValueError(problem)

        coef = np.array(payload["coef"], dtype=np.float64)
        # A width check as well as a version check: a hand-edited artifact could
        # carry the right stamp and the wrong shape, and a silent broadcast
        # would be worse than a refusal.
        if coef.shape != (N_TARGETS, N_FEATURES):
            raise ValueError(
                f"{path.name}: coefficients are {coef.shape}, expected {(N_TARGETS, N_FEATURES)}"
            )

        ridge = Ridge()
        ridge.coef_ = coef
        ridge.intercept_ = np.array(payload["intercept"], dtype=np.float64)
        return cls(standardiser=Standardiser.from_dict(payload["standardiser"]), model=ridge)
