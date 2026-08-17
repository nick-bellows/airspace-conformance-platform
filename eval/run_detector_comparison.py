"""Deterministic vs probabilistic conflict detection, on identical traffic.

    python eval/run_detector_comparison.py --scenarios 120

## What this answers

`conflict_detection.md` reports a recall of 1.00 and a precision of 0.57. The
precision is the weak number, and the diagnosis has always been that the
detector thresholds a point estimate: a predicted 4.9 NM miss alerts and 5.1 NM
does not, while the velocity estimate carries enough noise to move the answer
across that line.

The probabilistic detector uses the covariance the filter already maintains and
reports P(both standards breached at closest approach). This scores both on the
same scenarios, with the same ground truth, sweeping the probability threshold
so the trade is visible rather than argued.

## Why a sweep rather than one number

A threshold is a dial between recall and precision, so quoting one setting
invites the obvious question of whether it was chosen after seeing the answer.
The sweep is reported in full, including the settings that lose, and the
recommendation is made from the curve.

**No threshold is fitted here.** Every row is measured on the same nominal
family the committed report uses. Picking one row as a default and then quoting
its numbers as the system's performance would be selection on the test set; if
a threshold is adopted, `future-work.md` records that validating it needs a
family the choice was not made on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp.services.conformance.separation import DETECTOR_VERSION  # noqa: E402
from acp.sim.generator import GENERATOR_VERSION, NOMINAL, SHIFTED, generate_family  # noqa: E402
from acp.sim.scenario import Scenario, fingerprint, load_scenario  # noqa: E402
from eval.run_conflict_eval import ScenarioOutcome, _score, replay, summarise  # noqa: E402

#: Swept in full and reported in full. `None` is the deterministic detector.
THRESHOLDS: tuple[float | None, ...] = (None, 0.01, 0.05, 0.10, 0.20, 0.35, 0.50, 0.70)


async def _run_one(scenarios: list[Scenario], threshold: float | None) -> list[ScenarioOutcome]:
    outcomes = []
    for scenario in scenarios:
        outcome = await replay(
            scenario,
            horizontal_nm=5.0,
            vertical_ft=1000.0,
            lookahead_s=300.0,
            probability_threshold=threshold,
        )
        _score(outcome)
        outcomes.append(outcome)
    return outcomes


def _precision(outcomes: list[ScenarioOutcome]) -> float | None:
    alerts = sum(len(o.alerts) for o in outcomes)
    if not alerts:
        return None
    return (alerts - sum(o.false_alerts for o in outcomes)) / alerts


def bootstrap_precision_delta(
    baseline: list[ScenarioOutcome],
    candidate: list[ScenarioOutcome],
    *,
    iterations: int = 10_000,
    seed: int = 20260816,
) -> dict[str, Any]:
    """95% CI for the precision difference, resampling *scenarios*.

    Resampling scenarios rather than alerts is the point. Alerts from one
    scenario are not independent observations -- the same encounter geometry
    produces a burst of them as the pair closes -- so an alert-level bootstrap
    would treat one disagreement as many and report a confidence interval far
    tighter than the evidence supports. This is the same clustering argument
    the trajectory-prediction evaluation makes in M3.

    Both arms are resampled with the *same* indices, because they ran on the
    same scenarios; pairing removes the between-scenario variance that both
    share and is what makes a difference of a few alerts measurable at all.
    """
    rng = random.Random(seed)  # noqa: S311 - resampling, not cryptography
    n = len(baseline)
    deltas: list[float] = []
    for _ in range(iterations):
        picks = [rng.randrange(n) for _ in range(n)]
        base = _precision([baseline[i] for i in picks])
        cand = _precision([candidate[i] for i in picks])
        if base is not None and cand is not None:
            deltas.append(cand - base)
    deltas.sort()
    observed_base = _precision(baseline)
    observed_cand = _precision(candidate)
    lower = deltas[int(0.025 * len(deltas))]
    upper = deltas[int(0.975 * len(deltas))]
    return {
        "precision_delta": (
            round(observed_cand - observed_base, 4)
            if observed_base is not None and observed_cand is not None
            else None
        ),
        "ci95_low": round(lower, 4),
        "ci95_high": round(upper, 4),
        "excludes_zero": bool(lower > 0.0 or upper < 0.0),
        "iterations": iterations,
    }


def _row(threshold: float | None, summary: dict[str, Any]) -> dict[str, Any]:
    lead = summary["lead_time_s"]
    return {
        "threshold": threshold,
        "detector": "deterministic" if threshold is None else f"probabilistic p>={threshold}",
        "recall": summary["recall"],
        "precision": summary["precision"],
        "alerts_raised": summary["alerts_raised"],
        "false_alerts": summary["false_alerts"],
        "false_alerts_per_hour": summary["false_alerts_per_hour"],
        "median_lead_time_s": lead["median"],
        "detected_events": summary["detected_events"],
        "truth_conflict_events": summary["truth_conflict_events"],
    }


def render(rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    baseline = rows[0]
    lines = [
        "# Deterministic vs probabilistic conflict detection",
        "",
        f"Generated {meta['generated_at']} · seed `{meta['seed']}` · "
        f"{meta['scenarios']} scenarios · {meta['simulated_hours']} simulated hours",
        "",
        f"Scenario set SHA-256/16 `{meta['scenario_set_sha256_16']}` · "
        f"generator `{meta['generator_version']}` · detector `{meta['detector_version']}`",
        "",
        "Reproduce with `python eval/run_detector_comparison.py --scenarios "
        f"{meta['generated_scenarios']} --seed {meta['seed']}`.",
        "",
        "## The result",
        "",
        "| Detector | Recall | Precision | Alerts | False | False/hr | Median lead |"
        " Precision vs deterministic (95% CI) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        delta = row.get("vs_deterministic")
        if delta is None:
            comparison = "baseline"
        else:
            verdict = "" if delta["excludes_zero"] else " — spans zero"
            comparison = (
                f"{delta['precision_delta']:+.4f} "
                f"[{delta['ci95_low']:+.4f}, {delta['ci95_high']:+.4f}]{verdict}"
            )
        lines.append(
            f"| {row['detector']} | {row['recall']} | {row['precision']} | "
            f"{row['alerts_raised']} | {row['false_alerts']} | "
            f"{row['false_alerts_per_hour']} | {row['median_lead_time_s']} s | {comparison} |"
        )
    lines += [
        "",
        "Ground truth comes from noiseless simulator state that no part of the",
        "pipeline sees, so this comparison is not circular: both detectors consume",
        "the same degraded observation stream and are scored against what actually",
        "happened. Only the traffic is invented.",
        "",
        "## Reading it",
        "",
        f"The deterministic detector is the first row: recall {baseline['recall']}, "
        f"precision {baseline['precision']}, {baseline['false_alerts']} false alerts.",
        "Every probabilistic row below is the *same traffic* judged by probability",
        "instead of by which side of a line the mean landed.",
        "",
        "A row only beats the baseline if it raises precision **without** dropping",
        "recall. Recall is the number that matters most here -- a missed loss of",
        "separation is the failure this system exists to prevent, and trading it for",
        "a tidier precision would be the wrong deal at any exchange rate.",
        "",
    ]
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    family = NOMINAL if args.family == "nominal" else SHIFTED
    scenarios = generate_family(args.scenarios, args.seed, family)
    if args.family == "nominal":
        # The committed demo scenarios belong to the nominal family only.
        committed_dir = REPO_ROOT / "scenarios"
        scenarios.extend(load_scenario(p) for p in sorted(committed_dir.glob("*.yaml")))

    rows: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    baseline_outcomes: list[ScenarioOutcome] = []
    for threshold in THRESHOLDS:
        label = "deterministic" if threshold is None else f"p>={threshold}"
        print(f"  {label} ...", flush=True)
        outcomes = await _run_one(scenarios, threshold)
        summary = summarise(outcomes)
        row = _row(threshold, summary)
        if threshold is None:
            baseline_outcomes = outcomes
        else:
            row["vs_deterministic"] = bootstrap_precision_delta(baseline_outcomes, outcomes)
        rows.append(row)
        if not meta:
            meta = {
                "generated_at": datetime.now(UTC).isoformat(),
                "seed": args.seed,
                "scenarios": summary["scenarios"],
                "generated_scenarios": args.scenarios,
                "simulated_hours": summary["simulated_hours"],
                "scenario_set_sha256_16": fingerprint(scenarios),
                "generator_version": GENERATOR_VERSION,
                "detector_version": DETECTOR_VERSION,
                "family": args.family,
            }

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{args.name}.json").write_text(
        json.dumps({"meta": meta, "rows": rows}, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / f"{args.name}.md").write_text(render(rows, meta), encoding="utf-8")
    print(json.dumps(rows, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--family", choices=("nominal", "shifted"), default="nominal")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "eval/results")
    parser.add_argument("--name", default="detector_comparison")
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
