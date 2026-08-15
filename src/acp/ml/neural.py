"""The PyTorch residual model, isolated so that torch is an optional dependency.

## Why this is a separate module

`docs/cards/model-trajectory-predictor.md` and ADR 0007 both claim the system
degrades to dead reckoning when the model is unavailable. That claim was only
true for a *missing artifact*. With torch imported at the top of `models.py`, an
install without the `ml` extra could not even import the conformance service --
it crashed at startup rather than degrading.

Keeping every torch reference behind this module makes the claim true: the
serving path imports it lazily, and an ImportError is handled by the same
fallback that handles a corrupt file. The conformance service now starts, runs
on physics, and says so.

Training legitimately requires torch and imports this directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import torch
from torch import nn

from acp.ml.features import N_FEATURES, Standardiser
from acp.ml.models import MODEL_VERSION, N_TARGETS, version_mismatch


class _MLP(nn.Module):
    """Two hidden layers. Small on purpose.

    Roughly 3,500 parameters against hundreds of thousands of samples. A larger
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
        problem = version_mismatch(payload.get("model_version"), path)
        if problem is not None:
            raise ValueError(problem)

        net = _MLP()
        net.load_state_dict(payload["state_dict"])
        net.eval()
        return cls(
            standardiser=Standardiser.from_dict(payload["standardiser"]),
            net=net,
            target_scale=np.array(payload["target_scale"], dtype=np.float64),
            epochs_trained=int(payload.get("epochs_trained", 0)),
        )
