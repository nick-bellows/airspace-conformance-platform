"""Residual models: ridge regression and a small neural network.

Both predict the same three numbers -- the along-track, cross-track, and
altitude error of a dead-reckoning prediction -- and both are used the same way:
dead reckon, then add the correction.

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
from typing import Protocol

import numpy as np
import numpy.typing as npt
import torch
from sklearn.linear_model import Ridge
from torch import nn

from acp.ml.features import FEATURE_NAMES, N_FEATURES, Standardiser

#: Bump when a model's architecture or training procedure changes.
MODEL_VERSION = "acp-residual-v1"

N_TARGETS = 3
TARGET_NAMES = ("along_track_nm", "cross_track_nm", "altitude_ft")


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
    model: Ridge
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
        payload = json.loads(path.read_text(encoding="utf-8"))
        ridge = Ridge()
        ridge.coef_ = np.array(payload["coef"], dtype=np.float64)
        ridge.intercept_ = np.array(payload["intercept"], dtype=np.float64)
        return cls(
            standardiser=Standardiser.from_dict(payload["standardiser"]),
            model=ridge,
        )


class _MLP(nn.Module):
    """Two hidden layers. Small on purpose.

    Roughly 5,000 parameters against tens of thousands of samples. A larger
    network would fit the training scenarios more closely without predicting
    unseen traffic any better, and the distribution-shift split would show it.
    """

    def __init__(self, hidden: int = 64) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(N_FEATURES, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden // 2),
            nn.ReLU(),
            nn.Linear(hidden // 2, N_TARGETS),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)  # type: ignore[no-any-return]


@dataclass
class NeuralResidualModel:
    """A small MLP over the same features the ridge model sees."""

    standardiser: Standardiser
    net: _MLP
    #: Per-target scaling of the targets themselves. Altitude error is in feet
    #: and position error in nautical miles, so an unscaled loss would be
    #: dominated almost entirely by altitude and the position outputs would
    #: barely train.
    target_scale: npt.NDArray[np.float64]
    name: str = "neural"
    epochs_trained: int = 0

    @classmethod
    def train(
        cls,
        features: npt.NDArray[np.float64],
        targets: npt.NDArray[np.float64],
        standardiser: Standardiser,
        *,
        validation: tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]] | None = None,
        epochs: int = 120,
        batch_size: int = 256,
        learning_rate: float = 1e-3,
        patience: int = 15,
        seed: int = 20260815,
    ) -> NeuralResidualModel:
        """Train with early stopping on a validation split.

        Determinism is enforced by seeding torch and numpy: a model card that
        quotes an error figure has to be reproducible, and "roughly this number"
        is not a result.
        """
        torch.manual_seed(seed)
        np.random.seed(seed)

        target_scale = np.abs(targets).mean(axis=0)
        target_scale[target_scale < 1e-9] = 1.0

        x = torch.tensor(standardiser.transform(features), dtype=torch.float32)
        y = torch.tensor(targets / target_scale, dtype=torch.float32)

        net = _MLP()
        optimiser = torch.optim.Adam(net.parameters(), lr=learning_rate)
        # Huber rather than MSE. Trajectory residuals have a long tail -- a
        # go-around or a hard turn produces an error many times the typical
        # one -- and squared error would let a handful of those dominate every
        # gradient and drag the model off the common case.
        criterion = nn.HuberLoss(delta=1.0)

        val_x = val_y = None
        if validation is not None:
            val_features, val_targets = validation
            val_x = torch.tensor(standardiser.transform(val_features), dtype=torch.float32)
            val_y = torch.tensor(val_targets / target_scale, dtype=torch.float32)

        best_loss = float("inf")
        best_state = {k: v.clone() for k, v in net.state_dict().items()}
        since_improvement = 0
        completed = 0

        for epoch in range(epochs):
            net.train()
            permutation = torch.randperm(x.shape[0])
            for start in range(0, x.shape[0], batch_size):
                batch = permutation[start : start + batch_size]
                optimiser.zero_grad()
                loss = criterion(net(x[batch]), y[batch])
                loss.backward()
                optimiser.step()
            completed = epoch + 1

            if val_x is None or val_y is None:
                continue
            net.eval()
            with torch.no_grad():
                validation_loss = float(criterion(net(val_x), val_y))
            if validation_loss < best_loss - 1e-6:
                best_loss = validation_loss
                best_state = {k: v.clone() for k, v in net.state_dict().items()}
                since_improvement = 0
            else:
                since_improvement += 1
                if since_improvement >= patience:
                    break

        net.load_state_dict(best_state)
        net.eval()
        return cls(
            standardiser=standardiser,
            net=net,
            target_scale=target_scale,
            epochs_trained=completed,
        )

    def predict(self, features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        self.net.eval()
        with torch.no_grad():
            x = torch.tensor(self.standardiser.transform(features), dtype=torch.float32)
            scaled = self.net(x).numpy().astype(np.float64)
        return np.asarray(scaled * self.target_scale, dtype=np.float64)

    def parameter_count(self) -> int:
        return sum(p.numel() for p in self.net.parameters())

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "model_version": MODEL_VERSION,
                "kind": "neural",
                "state_dict": self.net.state_dict(),
                "standardiser": self.standardiser.to_dict(),
                "target_scale": self.target_scale.tolist(),
                "epochs_trained": self.epochs_trained,
            },
            path,
        )

    @classmethod
    def load(cls, path: Path) -> NeuralResidualModel:
        # weights_only=True refuses to unpickle arbitrary objects. A checkpoint
        # is data, and loading one should not be able to execute code -- this is
        # the well-known torch.load deserialisation hazard.
        payload = torch.load(path, weights_only=True)
        net = _MLP()
        net.load_state_dict(payload["state_dict"])
        net.eval()
        return cls(
            standardiser=Standardiser.from_dict(payload["standardiser"]),
            net=net,
            target_scale=np.array(payload["target_scale"], dtype=np.float64),
            epochs_trained=int(payload.get("epochs_trained", 0)),
        )
