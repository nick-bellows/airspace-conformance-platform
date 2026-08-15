"""Tests for the evaluation half of the training script.

Not the training loop -- that is exercised by running it -- but the scoring and
stratification, which is where a plausible-looking mistake turns into a
plausible-looking number that nobody questions.

The two pinned here are the ones that already went wrong once:

* **Skill is computed against the same stratum.** Comparing a stratum's error to
  the whole dataset's baseline produces something that looks like skill and is
  not.
* **Turning and climbing are separate strata.** Merging them buried the hard
  samples under easy ones and made the model look useless.
"""

from __future__ import annotations

import numpy as np
import pytest

from acp.ml.dataset import Dataset
from acp.ml.features import N_FEATURES
from acp.ml.models import N_TARGETS
from acp.ml.train import (
    SELECTION_STRATUM,
    _physics_residuals,
    _stratum_table,
    render_report,
    score,
    strata,
)


def a_dataset(
    phases: tuple[str, ...],
    *,
    targets: np.ndarray | None = None,
    horizon_s: float = 60.0,
) -> Dataset:
    count = len(phases)
    resolved = targets if targets is not None else np.zeros((count, N_TARGETS))
    features = np.zeros((count, N_FEATURES))
    features[:, 0] = 450.0  # ground speed
    return Dataset(
        features=features,
        targets=resolved,
        baseline_error_nm=np.hypot(resolved[:, 0], resolved[:, 1]),
        scenario_ids=tuple(f"s-{i}" for i in range(count)),
        phases=phases,
        horizon_s=horizon_s,
    )


# --------------------------------------------------------------------------
# Stratification
# --------------------------------------------------------------------------


def test_strata_split_on_true_phase() -> None:
    dataset = a_dataset(("cruise", "turn", "climb", "descent", "cruise"))
    masks = strata(dataset)
    assert masks["all"].sum() == 5
    assert masks["cruise"].sum() == 2
    assert masks["turning"].sum() == 1
    assert masks["climbing_or_descending"].sum() == 2


def test_turning_and_climbing_are_not_merged() -> None:
    """The mistake that made the model look useless.

    A climbing aircraft barely moves sideways, so climbing samples are as easy
    to predict horizontally as cruise. Merging them with turns buried 907 hard
    samples under 3,121 easy ones.
    """
    masks = strata(a_dataset(("turn", "climb")))
    assert not (masks["turning"] & masks["climbing_or_descending"]).any()


def test_the_winner_is_selected_on_the_turning_stratum() -> None:
    """Horizontal prediction is only hard in a turn; selecting anywhere else
    picks whichever model extrapolates a straight line marginally better."""
    assert SELECTION_STRATUM == "turning"
    assert SELECTION_STRATUM in strata(a_dataset(("turn",)))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_a_perfect_prediction_scores_zero_error_and_full_skill() -> None:
    targets = np.array([[1.0, 0.0, 100.0], [2.0, 0.0, 200.0]])
    dataset = a_dataset(("turn", "turn"), targets=targets)
    result = score("perfect", dataset, targets)
    assert result.median_nm == pytest.approx(0.0)
    assert result.skill_vs_dead_reckoning == pytest.approx(1.0)


def test_dead_reckoning_scores_exactly_zero_skill_against_itself() -> None:
    """The baseline must be the origin of the skill scale, not near it."""
    targets = np.array([[1.0, 0.0, 0.0], [3.0, 0.0, 0.0]])
    dataset = a_dataset(("turn", "turn"), targets=targets)
    result = score("dead_reckoning", dataset, np.zeros_like(targets))
    assert result.skill_vs_dead_reckoning == pytest.approx(0.0)


def test_a_prediction_worse_than_the_baseline_scores_negative_skill() -> None:
    """Negative skill must be reportable, not floored at zero."""
    targets = np.array([[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    dataset = a_dataset(("turn", "turn"), targets=targets)
    result = score("bad", dataset, np.array([[-3.0, 0.0, 0.0], [-3.0, 0.0, 0.0]]))
    assert result.skill_vs_dead_reckoning < 0.0


def test_skill_is_measured_against_the_same_stratum() -> None:
    """Comparing a stratum's error against the whole dataset's baseline gives a
    number that looks like skill and is not.

    Here the turning samples have a large baseline error and the cruise samples
    almost none. Scoring the cruise stratum against a whole-dataset baseline
    would report enormous skill for predicting nothing.
    """
    targets = np.array([[5.0, 0.0, 0.0], [5.0, 0.0, 0.0], [0.01, 0.0, 0.0], [0.01, 0.0, 0.0]])
    dataset = a_dataset(("turn", "turn", "cruise", "cruise"), targets=targets)
    masks = strata(dataset)

    cruise = score("dead_reckoning", dataset, np.zeros_like(targets), masks["cruise"])
    assert cruise.skill_vs_dead_reckoning == pytest.approx(0.0)
    assert cruise.samples == 2


def test_an_empty_stratum_scores_zero_samples_rather_than_crashing() -> None:
    dataset = a_dataset(("cruise", "cruise"))
    result = score("x", dataset, np.zeros((2, N_TARGETS)), strata(dataset)["turning"])
    assert result.samples == 0


def test_altitude_error_is_reported_separately_from_horizontal() -> None:
    targets = np.array([[0.0, 0.0, 500.0], [0.0, 0.0, 700.0]])
    dataset = a_dataset(("climb", "climb"), targets=targets)
    result = score("dead_reckoning", dataset, np.zeros_like(targets))
    assert result.median_nm == pytest.approx(0.0)
    assert result.median_altitude_ft == pytest.approx(600.0)


# --------------------------------------------------------------------------
# Physics baselines, computed in closed form
# --------------------------------------------------------------------------


def test_dead_reckoning_has_no_residual_by_definition() -> None:
    """It is the origin of the target frame, which is what lets every baseline
    and model be scored by identical code."""
    dataset = a_dataset(("cruise",) * 3)
    assert not _physics_residuals(dataset, "dead_reckoning").any()


def test_persistence_is_the_whole_displacement_short() -> None:
    dataset = a_dataset(("cruise",) * 2)  # 450 kt in the speed column
    residuals = _physics_residuals(dataset, "persistence")
    assert residuals[0, 0] == pytest.approx(-7.5)  # 450 kt for 60 s
    assert residuals[0, 1] == pytest.approx(0.0)


def test_constant_turn_matches_dead_reckoning_when_not_turning() -> None:
    dataset = a_dataset(("cruise",) * 2)  # turn rate column is zero
    assert not _physics_residuals(dataset, "constant_turn").any()


def test_constant_turn_curves_when_turning() -> None:
    dataset = a_dataset(("turn",) * 2)
    dataset.features[:, 2] = 3.0  # standard rate turn
    residuals = _physics_residuals(dataset, "constant_turn")
    # A 180 degree turn ends up well left or right of the straight-line point.
    assert abs(residuals[0, 1]) > 1.0


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------


def test_the_stratum_table_reports_skill_as_a_percentage() -> None:
    rows = [
        {
            "model": "neural",
            "median_nm": 1.22,
            "p90_nm": 3.4,
            "skill_vs_dead_reckoning": 0.494,
            "samples": 4495,
            "median_altitude_ft": 305.0,
        }
    ]
    table = _stratum_table(rows)
    assert "`neural`" in table
    assert "+49.4%" in table
    assert "4495" in table


def test_the_altitude_table_reports_feet() -> None:
    rows = [{"model": "neural", "median_altitude_ft": 305.0, "samples": 12318}]
    assert "305.0 ft" in _stratum_table(rows, altitude=True)


def test_the_report_states_what_it_does_not_measure() -> None:
    """The caveats are part of the artifact, not an optional extra."""
    report = {
        "generated_at": "2026-08-15T00:00:00+00:00",
        "seed": 1,
        "versions": {"sim": "acp-sim-v2"},
        "split_scenarios": {"train": 72},
        "horizons": {
            "60s": {
                "winner": "neural",
                "neural_improvement_over_ridge": 0.235,
                "material_improvement_threshold": 0.03,
                "shipped_artifact": "residual_neural_60s.pt",
                "results": {
                    "test_same_family": {
                        "turning": [],
                        "climbing_or_descending": [],
                        "cruise": [],
                    },
                    "test_shifted": {"turning": []},
                },
            }
        },
    }
    rendered = render_report(report)
    assert "What this does not measure" in rendered
    assert "turning stratum is the result" in rendered
    assert "acp-sim-v2" in rendered
