"""Conformance monitoring: did the aircraft go where we predicted?

This is the part of the system the project is named after. Every track gets a
prediction of where it will be one horizon from now; when that moment arrives,
the prediction is compared against where the aircraft actually turned out to be.
A large gap means the aircraft did something the model did not anticipate.

## What a non-conformance alert does and does not mean

It means **the aircraft did not do what our model of it predicted**. It does not
mean the aircraft did anything wrong. Without flight plans or clearances this
system has no idea what an aircraft was *supposed* to do, so a perfectly normal
turn onto a cleared heading looks exactly like an unexplained one. That
limitation is in the alert's own summary text, not just in the documentation,
because an operator reading the alert is the person most likely to over-read it.

## How the threshold is set, and one way of setting it that does not work

The first attempt scaled the threshold by the dead-reckoning error *of the same
sample*: alert when the model beat physics by less than some factor. It has a
fatal property, and a test caught it. When the predictor falls back to physics --
no model artifact, a corrupt one, a NaN output -- the model's error *is* the
baseline's error, so the comparison reduces to `error > 3 * error` and no alert
can ever fire. Conformance monitoring would silently stop working at exactly the
moment the model became unavailable, which is the failure this codebase spends
most of its effort avoiding elsewhere.

The threshold is therefore calibrated against **measured typical error**, with
separate figures for the model and for the physics fallback. An alert then means
"this aircraft deviated much more than a predictor of this kind usually does at
this horizon", which stays meaningful whichever predictor is running.

Those figures come from `models/calibration.json`, written by the training run
beside the model artifacts. They used to be constants copied into this file by
hand, which meant retraining a more accurate model left the thresholds behind
and made alerting quietly less sensitive. Loading them keeps the calibration and
the model in step; the constants remain only as a fallback for a deployment that
has no artifact.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from acp.common.contracts import AlertKind, ConformanceEvidence, Severity, TrackUpdate
from acp.common.geodesy import haversine_nm
from acp.common.logging import get_logger
from acp.ml.baselines import dead_reckon
from acp.ml.features import WINDOW, state_from
from acp.ml.models import CALIBRATION_FILENAME
from acp.ml.predictor import DEFAULT_MODELS_DIR, TrajectoryPredictor

_log = get_logger(__name__)

MONITOR_VERSION = "acp-conformance-monitor-v1"

#: How far ahead predictions are made and later checked.
DEFAULT_HORIZON_S = 60.0

#: Last-resort typical error, in nautical miles, used only when no calibration
#: artifact is available. Training writes `models/calibration.json` and the
#: monitor prefers it; these exist so a deployment without one still has
#: defensible thresholds rather than none.
#:
#: They are the same figures the training run produced, on the turning stratum
#: of the held-out split. Turning rather than the overall median because it is
#: the hard case: calibrating on cruise, where error is tens of metres, would
#: make every turn an alert.
FALLBACK_MODEL_ERROR_NM = {30.0: 0.412, 60.0: 1.220, 120.0: 2.732}

#: The same for dead reckoning, used when the predictor has fallen back to
#: physics. Larger, so the fallback alerts less readily -- which is correct:
#: physics is expected to be wrong more often, and treating that as a
#: non-conformance would flood on every turn.
FALLBACK_PHYSICS_ERROR_NM = {30.0: 1.007, 60.0: 2.412, 120.0: 5.085}

#: How many times the typical error a deviation must exceed to be reported.
#: Not swept -- it trades sensitivity against false alerts and no calibration
#: run exists for it. See docs/limitations.md.
DEFAULT_THRESHOLD_FACTOR = 3.0

#: Absolute floor, in nautical miles, for horizons with no calibration figure.
MINIMUM_THRESHOLD_NM = 1.5

#: Predictions older than this are discarded unmatched. Slightly wider than the
#: horizon so a dropped report does not lose the comparison entirely.
MATCH_TOLERANCE_S = 3.0


@dataclass(frozen=True, slots=True)
class Calibration:
    """Typical prediction error per horizon, for the model and for physics.

    Loaded from the artifact the training run writes. Falling back to compiled
    constants is deliberate: a deployment with no artifact should have
    defensible thresholds rather than none, and it logs which it is using so
    the situation is visible rather than assumed.
    """

    model_nm: dict[float, float]
    physics_nm: dict[float, float]
    source: str

    @classmethod
    def load(cls, models_dir: Path = DEFAULT_MODELS_DIR) -> Calibration:
        path = models_dir / CALIBRATION_FILENAME
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            horizons = payload["horizons"]
            calibration = cls(
                model_nm={float(k): float(v["model_median_nm"]) for k, v in horizons.items()},
                physics_nm={float(k): float(v["physics_median_nm"]) for k, v in horizons.items()},
                source=str(path),
            )
        except (OSError, KeyError, ValueError, TypeError):
            _log.warning(
                "no usable calibration artifact; using compiled fallback thresholds",
                extra={"path": str(path)},
            )
            return cls(
                model_nm=dict(FALLBACK_MODEL_ERROR_NM),
                physics_nm=dict(FALLBACK_PHYSICS_ERROR_NM),
                source="compiled fallback",
            )
        _log.info(
            "loaded conformance calibration",
            extra={"source": calibration.source, "horizons": sorted(calibration.model_nm)},
        )
        return calibration


@dataclass(frozen=True, slots=True)
class _PendingPrediction:
    """A prediction waiting for the moment it can be checked."""

    valid_at: datetime
    predicted_at: datetime
    lat: float
    lon: float
    altitude_ft: float
    baseline_lat: float
    baseline_lon: float
    source: str
    predictor_version: str


@dataclass(frozen=True, slots=True)
class NonConformance:
    """An aircraft that diverged from where it was predicted to be."""

    track_id: str
    callsign: str | None
    predicted_at: datetime
    horizon_s: float
    error_nm: float
    baseline_error_nm: float
    threshold_nm: float
    predictor_version: str
    used_model: bool

    @property
    def key(self) -> str:
        return f"{AlertKind.NON_CONFORMANCE}:{self.track_id}"

    @property
    def severity(self) -> Severity:
        # Advisory, always. A non-conformance says only that the aircraft
        # surprised our model; escalating that to a warning would imply a
        # judgement about the flight that this system is in no position to make.
        return Severity.ADVISORY

    @property
    def reason_codes(self) -> tuple[str, ...]:
        codes = ["prediction_error_above_threshold"]
        codes.append("model_prediction" if self.used_model else "physics_prediction_only")
        if self.error_nm > self.threshold_nm * 2.0:
            codes.append("large_deviation")
        return tuple(codes)

    @property
    def summary(self) -> str:
        label = self.callsign or self.track_id
        return (
            f"{label} diverged {self.error_nm:.1f} NM from its "
            f"{self.horizon_s:.0f} s prediction "
            f"(threshold {self.threshold_nm:.1f} NM). "
            "Advisory only: no flight plan is available, so this may be an "
            "entirely normal manoeuvre."
        )

    def as_evidence(self) -> ConformanceEvidence:
        return ConformanceEvidence(
            predicted_at=self.predicted_at,
            horizon_s=self.horizon_s,
            error_nm=self.error_nm,
            baseline_error_nm=self.baseline_error_nm,
            threshold_nm=self.threshold_nm,
            predictor_version=self.predictor_version,
        )


class ConformanceMonitor:
    """Predicts each track forward and checks the prediction when it matures."""

    def __init__(
        self,
        predictor: TrajectoryPredictor | None = None,
        *,
        horizon_s: float = DEFAULT_HORIZON_S,
        threshold_factor: float = DEFAULT_THRESHOLD_FACTOR,
        minimum_threshold_nm: float = MINIMUM_THRESHOLD_NM,
        calibration: Calibration | None = None,
    ) -> None:
        self._predictor = predictor or TrajectoryPredictor(horizon_s)
        self._horizon_s = horizon_s
        self._threshold_factor = threshold_factor
        self._minimum_threshold_nm = minimum_threshold_nm
        self._calibration = calibration if calibration is not None else Calibration.load()
        self._windows: dict[str, deque[TrackUpdate]] = {}
        self._pending: dict[str, deque[_PendingPrediction]] = {}

    @property
    def tracked(self) -> int:
        return len(self._windows)

    def observe(self, update: TrackUpdate) -> NonConformance | None:
        """Fold in an update, check any matured prediction, and make a new one.

        Returns a finding if this update resolved a prediction that turned out
        to be badly wrong.
        """
        window = self._windows.setdefault(update.track_id, deque(maxlen=WINDOW))
        window.append(update)

        finding = self._check_matured(update)
        self._make_prediction(update)
        return finding

    def forget(self, track_ids: list[str]) -> None:
        """Drop state for tracks that no longer exist."""
        for track_id in track_ids:
            self._windows.pop(track_id, None)
            self._pending.pop(track_id, None)

    def _threshold_for(self, used_model: bool) -> float:
        """Deviation, in NM, beyond which this horizon counts as non-conforming."""
        table = self._calibration.model_nm if used_model else self._calibration.physics_nm
        typical = table.get(self._horizon_s)
        if typical is None:
            # An uncalibrated horizon. Fall back to the floor and say so, rather
            # than inventing a figure by interpolating between measured ones.
            _log.warning(
                "no calibrated typical error for this horizon; using the minimum threshold",
                extra={"horizon_s": self._horizon_s, "threshold_nm": self._minimum_threshold_nm},
            )
            return self._minimum_threshold_nm
        return max(self._minimum_threshold_nm, self._threshold_factor * typical)

    def _check_matured(self, update: TrackUpdate) -> NonConformance | None:
        queue = self._pending.get(update.track_id)
        if not queue:
            return None

        tolerance = timedelta(seconds=MATCH_TOLERANCE_S)
        # Several predictions can mature against one update, after a dropout.
        # Keeping the *last* one silently discarded worse deviations that had
        # matured in the same burst; keeping the worst is the conservative
        # choice for a safety advisory. Only one survives either way, because
        # alerts are keyed per track and the lifecycle collapses them -- so the
        # question is which one, not how many.
        finding: NonConformance | None = None

        while queue:
            candidate = queue[0]
            if update.updated_at < candidate.valid_at - tolerance:
                break  # not due yet; everything behind it is even less due
            queue.popleft()
            if update.updated_at > candidate.valid_at + tolerance:
                continue  # missed its window, probably a dropout

            error_nm = haversine_nm(candidate.lat, candidate.lon, update.lat, update.lon)
            # Kept for the alert evidence rather than used for the decision: it
            # tells a reviewer how hard the situation was, which is useful
            # context but a bad threshold (see the module docstring).
            baseline_error_nm = haversine_nm(
                candidate.baseline_lat, candidate.baseline_lon, update.lat, update.lon
            )
            threshold = self._threshold_for(candidate.source == "model")
            if error_nm > threshold and (finding is None or error_nm > finding.error_nm):
                finding = NonConformance(
                    track_id=update.track_id,
                    callsign=update.callsign,
                    predicted_at=candidate.predicted_at,
                    horizon_s=self._horizon_s,
                    error_nm=error_nm,
                    baseline_error_nm=baseline_error_nm,
                    threshold_nm=threshold,
                    predictor_version=candidate.predictor_version,
                    used_model=candidate.source == "model",
                )
        return finding

    def _make_prediction(self, update: TrackUpdate) -> None:
        window = self._windows[update.track_id]
        prediction = self._predictor.predict(list(window))
        baseline = dead_reckon(state_from(update), self._horizon_s)

        queue = self._pending.setdefault(update.track_id, deque(maxlen=256))
        queue.append(
            _PendingPrediction(
                valid_at=update.updated_at + timedelta(seconds=self._horizon_s),
                predicted_at=update.updated_at,
                lat=prediction.lat,
                lon=prediction.lon,
                altitude_ft=prediction.altitude_ft,
                baseline_lat=baseline.lat,
                baseline_lon=baseline.lon,
                source=prediction.source,
                predictor_version=prediction.predictor_version,
            )
        )
