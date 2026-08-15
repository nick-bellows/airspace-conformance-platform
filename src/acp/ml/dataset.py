"""Build training data by running scenarios through the real pipeline.

Each sample is: twenty seconds of *filtered track updates* as features, and the
aircraft's *true* position some horizon later as the label.

## Why the label is truth and the features are not

Truth appears only as an offline training label. At inference the model sees
filtered track updates and nothing else. That is ordinary supervised learning --
you train against the answer and predict without it -- and it is worth being
explicit about because "the simulator knows the answer" sounds like cheating
until you see where the boundary is.

## Splitting by scenario, never by sample

Consecutive samples from one flight are almost identical: a one-second shift in
a twenty-second window. A random sample-level split would put near-duplicates in
both training and test, and the held-out error would be meaningless -- possibly
by an order of magnitude.

So splits are by **scenario**. Every sample from one flight lands entirely in
training or entirely in test. This is the single most consequential decision in
the evaluation and the easiest one to get silently wrong.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt

from acp.common.contracts import TrackState, TrackUpdate
from acp.ml.baselines import along_cross_track_error, dead_reckon
from acp.ml.features import WINDOW, Standardiser, extract, sanity_check, state_from
from acp.services.track.estimator import TrackEstimator
from acp.sim.engine import Simulation
from acp.sim.scenario import Scenario

#: Bump when sample construction changes. Invalidates cached datasets.
DATASET_VERSION = "acp-dataset-v1"

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Dataset:
    """Features, residual targets, and enough context to audit a sample."""

    features: npt.NDArray[np.float64]
    #: (along_track_nm, cross_track_nm, altitude_ft) residual from dead reckoning.
    targets: npt.NDArray[np.float64]
    #: Dead-reckoning error magnitude per sample, in NM. The number to beat.
    baseline_error_nm: npt.NDArray[np.float64]
    scenario_ids: tuple[str, ...]
    #: True flight phase at the moment of prediction, taken from the simulator.
    #:
    #: Used **only to group results when reporting**, never as a feature. That
    #: distinction matters: the filtered turn rate is too noisy to separate a
    #: straight aircraft from a gently turning one, so stratifying on it puts
    #: three quarters of steady cruise into the "manoeuvring" bucket and the
    #: split says nothing. Truth gives a clean grouping, and grouping results by
    #: something the model cannot see is ordinary analysis rather than leakage.
    phases: tuple[str, ...]
    horizon_s: float

    def __len__(self) -> int:
        return int(self.features.shape[0])

    @property
    def scenario_count(self) -> int:
        return len(set(self.scenario_ids))


@dataclass(frozen=True, slots=True)
class _Sample:
    features: npt.NDArray[np.float64]
    target: npt.NDArray[np.float64]
    baseline_error_nm: float
    scenario_id: str
    phase: str


@dataclass(frozen=True, slots=True)
class _TruthPoint:
    lat: float
    lon: float
    altitude_ft: float
    phase: str


def _run_scenario(
    scenario: Scenario,
) -> tuple[dict[str, list[TrackUpdate]], dict[str, dict[int, _TruthPoint]]]:
    """Replay one scenario, collecting filtered tracks and truth side by side.

    Returns the track update history per aircraft, and truth indexed by whole
    second so a sample at time t can look up the answer at t + horizon.
    """
    simulation = Simulation(scenario, _EPOCH)
    estimator = TrackEstimator()
    history: dict[str, list[TrackUpdate]] = {}
    truth: dict[str, dict[int, _TruthPoint]] = {}

    while not simulation.finished:
        simulation.advance(1.0)
        second = round(simulation.elapsed_s)

        for state in simulation.truth():
            truth.setdefault(state.icao24, {})[second] = _TruthPoint(
                lat=state.lat,
                lon=state.lon,
                altitude_ft=state.altitude_ft,
                phase=state.phase,
            )

        for report in simulation.observe():
            update = estimator.on_report(report)
            # Only confirmed tracks. An initiating track's velocity estimate is
            # still dominated by its initial guess, and training on that teaches
            # the model to correct the filter's warm-up rather than to predict
            # what an aircraft will do.
            if update.state is TrackState.CONFIRMED:
                history.setdefault(update.icao24, []).append(update)

    return history, truth


def _samples_from(scenario: Scenario, horizon_s: float) -> Iterator[_Sample]:
    history, truth = _run_scenario(scenario)
    horizon = round(horizon_s)

    for icao24, updates in history.items():
        aircraft_truth = truth.get(icao24, {})
        for index in range(WINDOW - 1, len(updates)):
            window = updates[index - WINDOW + 1 : index + 1]
            current = window[-1]

            # Elapsed seconds, recovered from the update's own timestamp so the
            # lookup cannot drift out of step with the truth index.
            now_s = round((current.updated_at - _EPOCH).total_seconds())
            answer = aircraft_truth.get(now_s + horizon)
            at_prediction_time = aircraft_truth.get(now_s)
            if answer is None or at_prediction_time is None:
                continue  # aircraft left, or the run ended before the horizon

            baseline = dead_reckon(state_from(current), horizon_s)
            along_nm, cross_nm = along_cross_track_error(
                baseline, answer.lat, answer.lon, current.track_deg
            )

            yield _Sample(
                features=extract(window),
                target=np.array(
                    [along_nm, cross_nm, answer.altitude_ft - baseline.altitude_ft],
                    dtype=np.float64,
                ),
                baseline_error_nm=float(np.hypot(along_nm, cross_nm)),
                scenario_id=scenario.scenario_id,
                # Phase at the moment the prediction is made, not at the
                # horizon: the question is "how hard is this to predict from
                # here", and that is decided by what the aircraft is doing now.
                phase=at_prediction_time.phase,
            )


def build(scenarios: Sequence[Scenario], horizon_s: float) -> Dataset:
    """Build a dataset from a set of scenarios at one prediction horizon."""
    samples = [s for scenario in scenarios for s in _samples_from(scenario, horizon_s)]
    if not samples:
        raise ValueError("no samples produced; scenarios may be shorter than the horizon")

    features = np.vstack([s.features for s in samples])
    sanity_check(features)
    return Dataset(
        features=features,
        targets=np.vstack([s.target for s in samples]),
        baseline_error_nm=np.array([s.baseline_error_nm for s in samples], dtype=np.float64),
        scenario_ids=tuple(s.scenario_id for s in samples),
        phases=tuple(s.phase for s in samples),
        horizon_s=horizon_s,
    )


def fit_standardiser(dataset: Dataset) -> Standardiser:
    """Fit scaling on this dataset. Call it on the training split only."""
    return Standardiser.fit(dataset.features)
