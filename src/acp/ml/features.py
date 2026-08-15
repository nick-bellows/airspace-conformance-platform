"""Feature extraction for trajectory prediction.

## The rule this module exists to enforce

Features are computed **only from filtered track updates** -- the same messages
the conformance service receives on `tracks.updates.v1`. Nothing here can reach
the simulator's intent, its flight plans, or its noiseless state.

That is not a stylistic preference. If the predictor could see the simulator's
plan it would be inverting the generator rather than learning anything, and
every number in the model card would be worthless. The constraint is structural:
:func:`extract` takes a sequence of `TrackUpdate` and nothing else, and a test
asserts that a manoeuvre is not predictable from the frames before it begins.

## What is in the window

Twenty seconds of history at 1 Hz. Long enough to see a turn developing and to
average out sensor noise; short enough that a track is eligible for prediction
within half a minute of appearing.

The feature that earns its place is `innovation` -- how far the aircraft was
from where constant-velocity physics said it would be, straight off the Kalman
filter. It is the filter's surprise, and a rising innovation is the earliest
observable sign that an aircraft is doing something the physics baseline will
get wrong. The model is being handed the residual signal explicitly rather than
being asked to rediscover it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from acp.common.contracts import TrackUpdate
from acp.common.geodesy import bearing_difference_deg
from acp.ml.baselines import KinematicState

# Bump when the feature set changes in any way. A bump invalidates every cached
# dataset and every trained model, because the input vector no longer means the
# same thing position by position.
FEATURE_VERSION = "acp-features-v1"

#: History used per sample, in updates (1 Hz, so also seconds).
WINDOW = 20

#: Named in order. The order is part of the contract between the dataset
#: builder, the trained model, and the inference path -- a model trained on one
#: ordering and served another produces confident nonsense with no error.
FEATURE_NAMES: tuple[str, ...] = (
    # -- current state --
    "ground_speed_kt",
    "vertical_rate_fpm",
    "turn_rate_deg_s",
    "altitude_kft",
    "position_uncertainty_m",
    "innovation_nm",
    # -- recent dynamics, over the window --
    "speed_change_kt",
    "heading_change_deg",
    "altitude_change_ft",
    "turn_rate_mean",
    "turn_rate_std",
    "turn_rate_abs_max",
    "vertical_rate_mean",
    "vertical_rate_std",
    "innovation_mean",
    "innovation_max",
    # -- coarse regime indicators --
    "is_climbing",
    "is_descending",
    "is_turning",
    "is_high_altitude",
)

N_FEATURES = len(FEATURE_NAMES)

#: A vertical rate below this reads as level flight.
LEVEL_FPM = 300.0
#: A turn rate below this reads as straight flight.
STRAIGHT_DEG_S = 0.15


def state_from(update: TrackUpdate) -> KinematicState:
    """The kinematic state a baseline predictor is given."""
    return KinematicState(
        lat=update.lat,
        lon=update.lon,
        altitude_ft=update.altitude_ft,
        ground_speed_kt=update.ground_speed_kt,
        track_deg=update.track_deg,
        vertical_rate_fpm=update.vertical_rate_fpm,
        turn_rate_deg_s=update.turn_rate_deg_s,
    )


def extract(window: Sequence[TrackUpdate]) -> npt.NDArray[np.float64]:
    """Build the feature vector from a window of track updates.

    The last element is the current state; earlier elements are history, oldest
    first. Raises if the window is too short rather than padding, because a
    padded window silently changes what the recent-dynamics features mean.
    """
    if len(window) < WINDOW:
        raise ValueError(f"need {WINDOW} updates, got {len(window)}")

    recent = list(window[-WINDOW:])
    current = recent[-1]
    first = recent[0]

    turn_rates = np.array([u.turn_rate_deg_s for u in recent], dtype=np.float64)
    vertical_rates = np.array([u.vertical_rate_fpm for u in recent], dtype=np.float64)
    # `innovation_nm` is optional on the wire -- a track that has only ever
    # coasted has none -- so absence is encoded as zero, meaning "no surprise".
    innovations = np.array([u.innovation_nm or 0.0 for u in recent], dtype=np.float64)

    heading_change = bearing_difference_deg(first.track_deg, current.track_deg)

    return np.array(
        [
            current.ground_speed_kt,
            current.vertical_rate_fpm,
            current.turn_rate_deg_s,
            current.altitude_ft / 1000.0,
            current.position_uncertainty_m,
            current.innovation_nm or 0.0,
            current.ground_speed_kt - first.ground_speed_kt,
            heading_change,
            current.altitude_ft - first.altitude_ft,
            float(turn_rates.mean()),
            float(turn_rates.std()),
            float(np.abs(turn_rates).max()),
            float(vertical_rates.mean()),
            float(vertical_rates.std()),
            float(innovations.mean()),
            float(innovations.max()),
            float(current.vertical_rate_fpm > LEVEL_FPM),
            float(current.vertical_rate_fpm < -LEVEL_FPM),
            float(abs(current.turn_rate_deg_s) > STRAIGHT_DEG_S),
            float(current.altitude_ft > 29000.0),
        ],
        dtype=np.float64,
    )


class Standardiser:
    """Zero-mean, unit-variance scaling, fitted on the training split only.

    Fitting on everything would leak test statistics into training. It is a
    small leak -- means and variances, not labels -- and it is exactly the kind
    that makes a held-out number quietly optimistic, so the fit is confined to
    the training split and the fitted values travel with the model.
    """

    def __init__(self, mean: npt.NDArray[np.float64], scale: npt.NDArray[np.float64]) -> None:
        self.mean = mean
        self.scale = scale

    @classmethod
    def fit(cls, features: npt.NDArray[np.float64]) -> Standardiser:
        mean = features.mean(axis=0)
        scale = features.std(axis=0)
        # A constant feature has zero variance; dividing by it produces NaN and
        # poisons every downstream weight. Leave it alone instead.
        scale[scale < 1e-9] = 1.0
        return cls(mean=mean, scale=scale)

    def transform(self, features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        return (features - self.mean) / self.scale

    def to_dict(self) -> dict[str, list[float]]:
        return {"mean": self.mean.tolist(), "scale": self.scale.tolist()}

    @classmethod
    def from_dict(cls, payload: dict[str, list[float]]) -> Standardiser:
        return cls(
            mean=np.array(payload["mean"], dtype=np.float64),
            scale=np.array(payload["scale"], dtype=np.float64),
        )


def sanity_check(features: npt.NDArray[np.float64]) -> None:
    """Fail loudly on a feature matrix that would silently ruin training.

    NaN and infinity propagate through gradient descent and turn every weight
    into NaN, at which point the model still trains, still saves, and still
    predicts -- it just predicts nothing. Worth one pass over the array.
    """
    if features.ndim != 2 or features.shape[1] != N_FEATURES:
        raise ValueError(f"expected (n, {N_FEATURES}) features, got {features.shape}")
    if not np.isfinite(features).all():
        bad = np.argwhere(~np.isfinite(features))
        column = FEATURE_NAMES[int(bad[0][1])]
        raise ValueError(f"non-finite value in feature {column!r} at row {int(bad[0][0])}")


def describe(features: npt.NDArray[np.float64]) -> str:
    """Human-readable feature summary, for the model card."""
    lines = [f"{'feature':<24} {'mean':>10} {'std':>10} {'min':>10} {'max':>10}"]
    for index, name in enumerate(FEATURE_NAMES):
        column = features[:, index]
        lines.append(
            f"{name:<24} {column.mean():>10.3f} {column.std():>10.3f} "
            f"{column.min():>10.3f} {column.max():>10.3f}"
        )
    return "\n".join(lines)


def horizon_seconds_valid(horizon_s: float) -> bool:
    """Horizons this feature set is meant for.

    Beyond a few minutes a constant-velocity baseline is so wrong that the
    residual stops being a small correction, which is the assumption the whole
    residual formulation rests on.
    """
    return 0.0 < horizon_s <= 300.0 and not math.isnan(horizon_s)
