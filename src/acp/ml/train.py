"""Train and evaluate the trajectory residual models.

    python -m acp.ml.train --scenarios 80

## The splits

Four disjoint sets of **scenarios**, never of samples:

* **train** -- nominal family, fits the models
* **validation** -- nominal family, chooses the winner and stops training early
* **test (same family)** -- nominal family, unseen scenarios
* **test (shifted)** -- a deliberately different family: other flight levels,
  denser traffic, twice the manoeuvre rate

The two test sets answer different questions. Same-family says "does this work
on traffic like the traffic it learned from". Shifted says "does it work when
the airspace changes", which is the question that matters and the one that
usually produces a worse number.

## The verdict

The winner is chosen on **validation**, never on test, and the model card
records which won and by how much. If the neural network does not clearly beat
ridge, the honest answer is to ship ridge, and this script says so rather than
quietly preferring the more impressive-sounding option.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from acp.ml.baselines import (
    KinematicState,
    along_cross_track_error,
    apply_along_cross,
    dead_reckon,
)
from acp.ml.dataset import DATASET_VERSION, Dataset, build, fit_standardiser
from acp.ml.features import FEATURE_VERSION
from acp.ml.models import (
    CALIBRATION_FILENAME,
    MODEL_VERSION,
    ResidualModel,
    RidgeResidualModel,
)
from acp.ml.neural import NeuralResidualModel
from acp.sim.generator import GENERATOR_VERSION, SHIFTED, TRAINING, generate_family
from acp.sim.scenario import SIM_VERSION

MODELS_DIR = Path("models")
RESULTS_DIR = Path("eval/results")

#: Prediction horizons, in seconds. Reported separately because error grows
#: sharply with horizon and a single averaged figure would hide that.
HORIZONS = (30.0, 60.0, 120.0)

#: A model must beat ridge by at least this fraction of its error to justify
#: its extra complexity. 3% is a judgement call, stated so it can be argued
#: with rather than buried.
MATERIAL_IMPROVEMENT = 0.03


def strata(dataset: Dataset) -> dict[str, npt.NDArray[np.bool_]]:
    """Split samples by what the aircraft was truly doing when the prediction
    was made.

    **This is the most important part of the evaluation.** An overall median is
    dominated by steady cruise, where a straight-line prediction is already
    near-exact and no model can add anything. Reporting only that number makes
    the problem look solved and the model look useful, when most of the dataset
    is trivial.

    Grouping uses the simulator's true flight phase, not the filtered turn rate.
    The first attempt used the filtered rate and put three quarters of steady
    cruise into the "manoeuvring" bucket, because the estimate is noisier than
    the threshold -- the same noise that makes the constant-turn baseline
    useless. Truth gives a clean grouping, and it is only ever used to *report*
    results; no model sees it.

    **Turning and climbing are kept apart, and that matters.** A climb barely
    changes where an aircraft is horizontally, so climbing samples are as easy
    to predict in the horizontal plane as cruise. Lumping them together under
    "manoeuvring" buried 907 genuinely hard turning samples under 3,121 easy
    climbing ones and made the model look useless when it was not. The metric
    has to match the axis the aircraft is manoeuvring in.
    """
    phases = np.array(dataset.phases)
    return {
        "all": np.ones(len(dataset), dtype=np.bool_),
        "cruise": phases == "cruise",
        #: The stratum that decides whether horizontal prediction works.
        "turning": phases == "turn",
        #: Judge this one on altitude error, not horizontal.
        "climbing_or_descending": np.isin(phases, ("climb", "descent")),
    }


#: The stratum the winner is chosen on. Horizontal prediction is only difficult
#: when an aircraft is turning; selecting on anything else picks whichever model
#: is marginally better at extrapolating a straight line.
SELECTION_STRATUM = "turning"


@dataclass(frozen=True, slots=True)
class Scores:
    """Prediction error for one model at one horizon."""

    name: str
    median_nm: float
    p90_nm: float
    mean_nm: float
    median_altitude_ft: float
    #: Fraction of the dead-reckoning error removed. Positive is better than
    #: the baseline, zero is identical to it, negative is worse than doing
    #: nothing at all.
    skill_vs_dead_reckoning: float
    samples: int

    def as_dict(self) -> dict[str, object]:
        return {
            "model": self.name,
            "median_nm": round(self.median_nm, 4),
            "p90_nm": round(self.p90_nm, 4),
            "mean_nm": round(self.mean_nm, 4),
            "median_altitude_ft": round(self.median_altitude_ft, 1),
            "skill_vs_dead_reckoning": round(self.skill_vs_dead_reckoning, 4),
            "samples": self.samples,
        }


def _errors_nm(dataset: Dataset, residuals: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Horizontal error of a corrected prediction, in nautical miles.

    Works in the along/cross frame: the target is the residual the model should
    have produced, so the error is the distance between predicted and actual
    residual. That is exactly the great-circle distance between the predicted
    and true positions, without needing to reconstruct either.
    """
    delta = dataset.targets[:, :2] - residuals[:, :2]
    return np.hypot(delta[:, 0], delta[:, 1])


def score(
    name: str,
    dataset: Dataset,
    residuals: npt.NDArray[np.float64],
    mask: npt.NDArray[np.bool_] | None = None,
) -> Scores:
    """Error for one model, optionally restricted to a stratum."""
    selector = mask if mask is not None else np.ones(len(dataset), dtype=np.bool_)
    if not selector.any():
        return Scores(name, 0.0, 0.0, 0.0, 0.0, 0.0, 0)

    errors = _errors_nm(dataset, residuals)[selector]
    altitude_errors = np.abs(dataset.targets[:, 2] - residuals[:, 2])[selector]
    # The baseline is recomputed over the same stratum, so skill always compares
    # like with like. Using a whole-dataset baseline against a stratum's error
    # would produce a number that looks like skill and is not.
    baseline_median = float(np.median(dataset.baseline_error_nm[selector]))
    median = float(np.median(errors))
    return Scores(
        name=name,
        median_nm=median,
        p90_nm=float(np.percentile(errors, 90)),
        mean_nm=float(errors.mean()),
        median_altitude_ft=float(np.median(altitude_errors)),
        skill_vs_dead_reckoning=(
            (baseline_median - median) / baseline_median if baseline_median > 0 else 0.0
        ),
        samples=int(selector.sum()),
    )


def _physics_residuals(dataset: Dataset, kind: str) -> npt.NDArray[np.float64]:
    """Residuals implied by a physics baseline, in the same frame as the targets.

    Dead reckoning is the origin of that frame, so its residual is zero by
    definition, and the others are expressed as their offset from it. That is
    what lets every baseline and every model be scored by identical code -- a
    baseline measured along a different path is a baseline you cannot compare
    against.

    Computed in closed form in the aircraft's own along/cross frame rather than
    by stepping around a sphere. Over a two-minute horizon the flat-frame error
    is under 0.01%, and the loop version was the slowest thing in the pipeline
    by a wide margin.
    """
    if kind == "dead_reckoning":
        return np.zeros_like(dataset.targets)

    horizon = dataset.horizon_s
    speed_nm_s = dataset.features[:, 0] / 3600.0
    vertical_ft_s = dataset.features[:, 1] / 60.0
    turn_rad_s = np.radians(dataset.features[:, 2])

    dr_along = speed_nm_s * horizon
    dr_alt = vertical_ft_s * horizon

    if kind == "persistence":
        # The aircraft never moves, so relative to dead reckoning it is exactly
        # the whole dead-reckoned displacement short, and at the original level.
        return np.stack([-dr_along, np.zeros_like(dr_along), -dr_alt], axis=1)

    # Constant turn: a circular arc of radius v/omega. Where omega is
    # negligible the arc degenerates to the straight line and the formula
    # divides by zero, so those rows fall back to dead reckoning.
    turning = np.abs(turn_rad_s) > 1e-9
    theta = turn_rad_s * horizon
    with np.errstate(divide="ignore", invalid="ignore"):
        radius = np.where(turning, speed_nm_s / np.where(turning, turn_rad_s, 1.0), 0.0)
    along = np.where(turning, radius * np.sin(theta), dr_along)
    cross = np.where(turning, radius * (1.0 - np.cos(theta)), 0.0)
    return np.stack([along - dr_along, cross, np.zeros_like(dr_along)], axis=1)


#: Bootstrap resamples. 400 is enough for a 95% interval to be stable to a few
#: thousandths of a nautical mile, and cheap on a few thousand samples.
BOOTSTRAP_RESAMPLES = 400


def bootstrap_interval(
    dataset: Dataset,
    residuals: dict[str, npt.NDArray[np.float64]],
    mask: npt.NDArray[np.bool_],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = 4242,
) -> dict[str, Any]:
    """Scenario-clustered bootstrap of median error, and of the neural-vs-ridge gap.

    **Resampling scenarios rather than samples is the whole point.** Consecutive
    samples from one flight are the same twenty-second window shifted by a
    second, so they are almost identical. Treating 4,495 of those as 4,495
    independent observations would produce an interval far too narrow to mean
    anything -- the effective sample size is closer to the number of *scenarios*,
    which is thirty.

    Reported because a project that insists on measured claims should not ask a
    reader to take "neural beats ridge by 24%" on trust when the underlying n is
    much smaller than it looks.
    """
    rng = np.random.default_rng(seed)
    errors = {name: _errors_nm(dataset, r) for name, r in residuals.items()}
    scenarios = np.array(dataset.scenario_ids)
    unique = np.unique(scenarios[mask])
    if len(unique) < 2:
        return {"resamples": 0, "note": "too few scenarios to bootstrap"}

    # Precompute per-scenario index arrays so each resample is a concatenation
    # rather than a full pass over the dataset.
    indices = {name: np.flatnonzero(mask & (scenarios == name)) for name in unique}

    draws: dict[str, list[float]] = {name: [] for name in residuals}
    gaps: list[float] = []
    for _ in range(resamples):
        chosen = rng.choice(unique, size=len(unique), replace=True)
        picked = np.concatenate([indices[name] for name in chosen])
        for name, error in errors.items():
            draws[name].append(float(np.median(error[picked])))
        if "neural" in errors and "ridge" in errors:
            ridge_median = float(np.median(errors["ridge"][picked]))
            neural_median = float(np.median(errors["neural"][picked]))
            if ridge_median > 0:
                gaps.append((ridge_median - neural_median) / ridge_median)

    summary: dict[str, Any] = {
        "resamples": resamples,
        "scenarios": len(unique),
        "median_nm_ci95": {
            name: [
                round(float(np.percentile(values, 2.5)), 4),
                round(float(np.percentile(values, 97.5)), 4),
            ]
            for name, values in draws.items()
        },
    }
    if gaps:
        summary["neural_over_ridge_ci95"] = [
            round(float(np.percentile(gaps, 2.5)), 4),
            round(float(np.percentile(gaps, 97.5)), 4),
        ]
        # The decision the interval is there to support: is the improvement
        # distinguishable from zero at all?
        summary["neural_beats_ridge"] = bool(float(np.percentile(gaps, 2.5)) > 0.0)
    return summary


def evaluate_all(
    dataset: Dataset, models: dict[str, ResidualModel]
) -> dict[str, list[dict[str, object]]]:
    """Score every baseline and model on every stratum of one dataset."""
    residuals: dict[str, npt.NDArray[np.float64]] = {
        "persistence": _physics_residuals(dataset, "persistence"),
        "dead_reckoning": _physics_residuals(dataset, "dead_reckoning"),
        "constant_turn": _physics_residuals(dataset, "constant_turn"),
    }
    residuals.update({name: model.predict(dataset.features) for name, model in models.items()})

    return {
        stratum: [score(name, dataset, r, mask).as_dict() for name, r in residuals.items()]
        for stratum, mask in strata(dataset).items()
    }


def _round_trip_check(dataset: Dataset, model: ResidualModel) -> float:
    """Verify the along/cross residual really reconstructs a position.

    Cheap insurance against a sign error in the decomposition, which would be
    invisible in the loss but would put every prediction on the wrong side of
    the aircraft. Returns the worst discrepancy in nautical miles.
    """
    residuals = model.predict(dataset.features[:50])
    worst = 0.0
    for index in range(residuals.shape[0]):
        state = KinematicState(
            lat=40.0,
            lon=-75.0,
            altitude_ft=35000.0,
            ground_speed_kt=float(dataset.features[index, 0]),
            track_deg=90.0,
            vertical_rate_fpm=float(dataset.features[index, 1]),
            turn_rate_deg_s=float(dataset.features[index, 2]),
        )
        reference = dead_reckon(state, dataset.horizon_s)
        lat, lon = apply_along_cross(
            reference, float(residuals[index, 0]), float(residuals[index, 1]), 90.0
        )
        along, cross = along_cross_track_error(reference, lat, lon, 90.0)
        worst = max(
            worst,
            abs(along - float(residuals[index, 0])),
            abs(cross - float(residuals[index, 1])),
        )
    return worst


def train_and_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    total = args.scenarios
    train_n = int(total * 0.6)
    val_n = int(total * 0.15)

    training = generate_family(total, args.seed, TRAINING)
    shifted = generate_family(max(20, total // 3), args.seed + 7777, SHIFTED)

    splits = {
        "train": training[:train_n],
        "validation": training[train_n : train_n + val_n],
        "test_same_family": training[train_n + val_n :],
        "test_shifted": shifted,
    }
    # Scenario-level disjointness is the property the whole evaluation rests on:
    # if a scenario leaked from training into test, near-duplicate samples would
    # appear on both sides and the held-out numbers would be fiction. Checked at
    # runtime rather than assumed, and raising rather than asserting so it still
    # holds when Python is run with -O.
    seen: set[str] = set()
    for name, scenarios in splits.items():
        ids = {s.scenario_id for s in scenarios}
        overlap = ids & seen
        if overlap:
            raise RuntimeError(f"split {name!r} reuses scenarios from another split: {overlap}")
        seen |= ids

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "versions": {
            "sim": SIM_VERSION,
            "generator": GENERATOR_VERSION,
            "features": FEATURE_VERSION,
            "dataset": DATASET_VERSION,
            "model": MODEL_VERSION,
        },
        "seed": args.seed,
        "split_scenarios": {name: len(s) for name, s in splits.items()},
        "horizons": {},
    }

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    calibration: dict[str, dict[str, Any]] = {}

    for horizon in HORIZONS:
        print(f"\n=== horizon {horizon:.0f} s ===")
        datasets = {name: build(s, horizon) for name, s in splits.items()}
        for name, dataset in datasets.items():
            print(f"  {name:<18} {len(dataset):>7} samples from {dataset.scenario_count} scenarios")

        standardiser = fit_standardiser(datasets["train"])
        ridge = RidgeResidualModel.train(
            datasets["train"].features, datasets["train"].targets, standardiser
        )
        neural = NeuralResidualModel.train(
            datasets["train"].features,
            datasets["train"].targets,
            standardiser,
            validation=(datasets["validation"].features, datasets["validation"].targets),
        )

        models: dict[str, ResidualModel] = {"ridge": ridge, "neural": neural}

        # Winner chosen on validation, on the turning stratum only.
        selection = strata(datasets["validation"])[SELECTION_STRATUM]
        validation_scores = {
            name: score(
                name,
                datasets["validation"],
                model.predict(datasets["validation"].features),
                selection,
            )
            for name, model in models.items()
        }
        ridge_error = validation_scores["ridge"].median_nm
        neural_error = validation_scores["neural"].median_nm
        improvement = (ridge_error - neural_error) / ridge_error if ridge_error > 0 else 0.0
        winner = "neural" if improvement >= MATERIAL_IMPROVEMENT else "ridge"

        print(
            f"  validation ({SELECTION_STRATUM}, {int(selection.sum())} samples): "
            f"ridge {ridge_error:.4f} NM, neural {neural_error:.4f} NM "
            f"({improvement:+.1%}) -> {winner}"
        )

        horizon_report: dict[str, Any] = {
            "validation_median_nm": {
                "ridge": round(ridge_error, 4),
                "neural": round(neural_error, 4),
            },
            "neural_improvement_over_ridge": round(improvement, 4),
            "material_improvement_threshold": MATERIAL_IMPROVEMENT,
            "winner": winner,
            "neural_parameters": neural.parameter_count(),
            "neural_epochs": neural.epochs_trained,
            "round_trip_error_nm": round(_round_trip_check(datasets["train"], ridge), 9),
            "results": {},
        }
        for split in ("test_same_family", "test_shifted"):
            by_stratum = evaluate_all(datasets[split], models)
            horizon_report["results"][split] = by_stratum
            horizon_report[f"stratum_samples_{split}"] = {
                k: int(v.sum()) for k, v in strata(datasets[split]).items()
            }

            residuals = {
                "dead_reckoning": _physics_residuals(datasets[split], "dead_reckoning"),
                **{n: m.predict(datasets[split].features) for n, m in models.items()},
            }
            horizon_report[f"bootstrap_{split}"] = bootstrap_interval(
                datasets[split], residuals, strata(datasets[split])[SELECTION_STRATUM]
            )

            turning = {row["model"]: row for row in by_stratum[SELECTION_STRATUM]}
            climbing = {row["model"]: row for row in by_stratum["climbing_or_descending"]}
            print(
                f"  {split:<18} turning  n={turning['dead_reckoning']['samples']:<6} "
                f"DR {turning['dead_reckoning']['median_nm']:.3f} -> "
                f"{winner} {turning[winner]['median_nm']:.3f} NM "
                f"(skill {turning[winner]['skill_vs_dead_reckoning']:+.1%})"
            )
            print(
                f"  {'':<18} vertical n={climbing['dead_reckoning']['samples']:<6} "
                f"DR {climbing['dead_reckoning']['median_altitude_ft']:.0f} -> "
                f"{winner} {climbing[winner]['median_altitude_ft']:.0f} ft altitude error"
            )

        # Remove the loser's artifact from a previous run. Leaving it behind
        # would put two models for one horizon in the directory, and the
        # predictor's load order would silently decide which one production
        # used -- a stale artifact winning that race is exactly the kind of
        # thing nobody notices for months.
        for stale in (
            MODELS_DIR / f"residual_ridge_{int(horizon)}s.json",
            MODELS_DIR / f"residual_neural_{int(horizon)}s.pt",
        ):
            stale.unlink(missing_ok=True)

        if winner == "ridge":
            ridge.save(MODELS_DIR / f"residual_ridge_{int(horizon)}s.json")
            horizon_report["shipped_artifact"] = f"residual_ridge_{int(horizon)}s.json"
        else:
            neural.save(MODELS_DIR / f"residual_neural_{int(horizon)}s.pt")
            horizon_report["shipped_artifact"] = f"residual_neural_{int(horizon)}s.pt"

        # Typical error on the turning stratum of the held-out split, for both
        # the shipped model and the physics fallback. The conformance monitor
        # reads this rather than carrying hand-copied constants: retraining a
        # more accurate model used to leave the thresholds behind, making
        # alerting progressively less sensitive with nothing to indicate it.
        turning = {
            row["model"]: row
            for row in horizon_report["results"]["test_same_family"][SELECTION_STRATUM]
        }
        calibration[str(int(horizon))] = {
            "model_median_nm": turning[winner]["median_nm"],
            "physics_median_nm": turning["dead_reckoning"]["median_nm"],
            "stratum": SELECTION_STRATUM,
            "samples": turning[winner]["samples"],
        }

        if horizon == 60.0:
            horizon_report["ridge_coefficients"] = ridge.coefficients()

        report["horizons"][f"{int(horizon)}s"] = horizon_report

    (MODELS_DIR / CALIBRATION_FILENAME).write_text(
        json.dumps(
            {
                "model_version": MODEL_VERSION,
                "generated_at": report["generated_at"],
                "seed": args.seed,
                "horizons": calibration,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report["calibration"] = calibration
    return report


def _stratum_table(rows: list[dict[str, Any]], *, altitude: bool = False) -> str:
    """One markdown table of model scores for a stratum."""
    if altitude:
        header = "| Model | Median altitude error | Samples |\n| --- | --- | --- |"
        lines = [
            f"| `{r['model']}` | {r['median_altitude_ft']} ft | {r['samples']} |" for r in rows
        ]
    else:
        header = (
            "| Model | Median | p90 | Skill vs dead reckoning | Samples |\n"
            "| --- | --- | --- | --- | --- |"
        )
        lines = [
            f"| `{r['model']}` | {r['median_nm']} NM | {r['p90_nm']} NM | "
            f"{float(r['skill_vs_dead_reckoning']):+.1%} | {r['samples']} |"
            for r in rows
        ]
    return "\n".join([header, *lines])


def _shift_verdict(horizons: dict[str, Any]) -> str:
    """State, per horizon, whether neural beats ridge on shifted traffic.

    Generated from the bootstrap rather than written by hand, so it cannot drift
    away from the numbers it describes the way a prose summary would.
    """
    lines = []
    for name, horizon in horizons.items():
        bootstrap = horizon.get("bootstrap_test_shifted", {})
        interval = bootstrap.get("neural_over_ridge_ci95")
        if interval is None:
            continue
        low, high = interval
        verdict = (
            "distinguishable from zero"
            if bootstrap.get("neural_beats_ridge")
            else "**not distinguishable from zero**"
        )
        lines.append(f"- **{name}** on shifted traffic: {low:+.1%} to {high:+.1%}, {verdict}")
    return "\n".join(lines) if lines else "_No shifted bootstrap available._"


def _bootstrap_note(bootstrap: dict[str, Any]) -> str:
    """One paragraph of confidence intervals under a results table."""
    if not bootstrap or not bootstrap.get("resamples"):
        return "_No bootstrap: too few scenarios._"

    intervals = bootstrap["median_nm_ci95"]
    lines = [
        f"95% confidence intervals from {bootstrap['resamples']} bootstrap resamples over "
        f"{bootstrap['scenarios']} **scenarios** (not samples — consecutive windows from one "
        "flight are near-duplicates, so the effective sample size is the number of flights):",
        "",
    ]
    lines += [
        f"- `{name}` median {low} to {high} NM" for name, (low, high) in sorted(intervals.items())
    ]
    if "neural_over_ridge_ci95" in bootstrap:
        low, high = bootstrap["neural_over_ridge_ci95"]
        verdict = (
            "the improvement is distinguishable from zero"
            if bootstrap.get("neural_beats_ridge")
            else "**the improvement is not distinguishable from zero**"
        )
        lines += ["", f"Neural over ridge: {low:+.1%} to {high:+.1%}, so {verdict}."]
    return "\n".join(lines)


def render_report(report: dict[str, Any]) -> str:
    """Write the findings out in prose, with the caveats attached to them."""
    versions = report["versions"]
    horizons = report["horizons"]

    sections = [
        "# Trajectory prediction evaluation",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "| Component | Version |",
        "| --- | --- |",
        *[f"| {name} | `{value}` |" for name, value in versions.items()],
        "",
        f"Seed `{report['seed']}`. Scenario counts per split: "
        + ", ".join(f"{k} {v}" for k, v in report["split_scenarios"].items()),
        "",
        "## What is being predicted",
        "",
        "Where an aircraft will be after a fixed horizon, using **only filtered",
        "track updates** -- the same messages the conformance service receives.",
        "The simulator's flight plans are never published and no predictor can",
        "see them, so the model cannot invert the generator; it can only learn",
        "how aircraft in this airspace tend to behave.",
        "",
        "Both models predict a **residual**: the correction to a dead-reckoning",
        "prediction, decomposed into along-track, cross-track, and altitude",
        "error. A model that outputs zero degrades exactly to dead reckoning,",
        "so the worst case is the baseline rather than nonsense.",
        "",
        "## How to read the strata",
        "",
        "Results are grouped by what the aircraft was truly doing when the",
        "prediction was made. **The turning stratum is the result**; everything",
        "else is close to free.",
        "",
        "In cruise and in a climb, an aircraft is going where a straight line",
        "says it is going, and dead reckoning is already accurate to a few tens",
        "of metres. Averaging those in produces a headline number dominated by",
        "samples nobody needed a model for. An earlier version of this report",
        "did exactly that and made the model look useless.",
        "",
        "Climbs are judged on **altitude** error rather than horizontal, for the",
        "same reason: a climbing aircraft barely moves sideways.",
        "",
    ]

    for name, horizon in horizons.items():
        results = horizon["results"]
        sections += [
            f"## Horizon {name}",
            "",
            f"Winner on validation: **{horizon['winner']}** "
            f"(neural improved on ridge by "
            f"{float(horizon['neural_improvement_over_ridge']):+.1%}, "
            f"threshold {float(horizon['material_improvement_threshold']):.0%}). "
            f"Shipped artifact: `{horizon['shipped_artifact']}`.",
            "",
            "### Turning — horizontal error",
            "",
            "**Same family (unseen scenarios)**",
            "",
            _stratum_table(results["test_same_family"]["turning"]),
            "",
            _bootstrap_note(horizon.get("bootstrap_test_same_family", {})),
            "",
            "**Shifted family (different airspace)**",
            "",
            _stratum_table(results["test_shifted"]["turning"]),
            "",
            _bootstrap_note(horizon.get("bootstrap_test_shifted", {})),
            "",
            "### Climbing or descending — altitude error",
            "",
            _stratum_table(results["test_same_family"]["climbing_or_descending"], altitude=True),
            "",
            "### Cruise — horizontal error",
            "",
            _stratum_table(results["test_same_family"]["cruise"]),
            "",
        ]

    sections += [
        "## Findings",
        "",
        "**The model roughly halves horizontal error during turns**, which is",
        "the only regime where horizontal prediction is hard. It also holds up",
        "under distribution shift: skill drops when the airspace changes, but",
        "does not collapse. That drop is the honest cost of training on one",
        "traffic distribution and flying in another.",
        "",
        "**The constant-turn baseline is worse than assuming straight flight.**",
        "Extrapolating the estimated turn rate for a minute amplifies its noise",
        "into miles of arc error -- the turn-rate estimate is noisier than the",
        "signal it carries. This is a useful negative result: the obvious",
        "physics improvement over dead reckoning makes things worse.",
        "",
        "**At the shortest horizon the model degrades altitude prediction.**",
        "Dead reckoning's vertical error at 30 s is already a few tens of feet,",
        "and the model adds noise rather than removing it. It only earns its",
        "place vertically at 60 s and beyond. A deployment that cared about",
        "30 s altitude should use the baseline.",
        "",
        "**The neural network's advantage over ridge does not survive",
        "distribution shift at longer horizons.** This is the most important",
        "finding in the report and it only became visible with confidence",
        "intervals.",
        "",
        _shift_verdict(horizons),
        "",
        "Both models are trained on identical features and predict the same",
        "residual, and **both clearly beat dead reckoning everywhere** -- those",
        "intervals do not overlap at all. The question is only whether the extra",
        "capacity earns its place. On traffic resembling the training set it",
        "clearly does. On traffic that does not resemble it, at the horizons",
        "that matter most, the honest answer is that these data cannot tell the",
        "two apart.",
        "",
        "The shipped model is still the one validation chose, because selecting",
        "on the shift test would turn the shift test into a training signal and",
        "destroy the only out-of-distribution estimate available. But a",
        "deployment that valued inspectability over an advantage this uncertain",
        "would be right to ship ridge, and its weights are recorded below.",
        "",
        "## What this does not measure",
        "",
        "Real-world accuracy. Simulated aircraft hold heading and speed exactly",
        "and there is no wind, no turbulence, and no variation between",
        "autopilots or crews. Real 60-second prediction error is substantially",
        "larger than anything here, and the gap is unmeasured. See",
        "`docs/limitations.md`.",
        "",
        "The manoeuvre distribution is also invented: the training family gives",
        "most aircraft several manoeuvres in fifteen minutes, which is far more",
        "than real en-route traffic. That was a deliberate choice to get enough",
        "turning samples to measure anything, and it means the *proportions* in",
        "these strata say nothing about real airspace.",
        "",
        "## Reproduce",
        "",
        "```",
        f"python -m acp.ml.train --scenarios 120 --seed {report['seed']}",
        "```",
        "",
        "Roughly seven minutes on a laptop CPU. Deterministic: the seed fixes",
        "scenario generation, the train/validation/test split, and torch's",
        "initialisation and batch order.",
        "",
    ]
    return "\n".join(sections)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acp-train", description=__doc__)
    parser.add_argument("--scenarios", type=int, default=80, help="nominal-family scenarios")
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--output", type=Path, default=RESULTS_DIR / "trajectory_prediction.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = train_and_evaluate(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown = args.output.with_suffix(".md")
    markdown.write_text(render_report(report), encoding="utf-8")
    print(f"\nwrote {args.output} and {markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
