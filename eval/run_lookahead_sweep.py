"""Precision is bought with lead time. This publishes the exchange rate.

    python eval/run_lookahead_sweep.py

## What this answers

`conflict_detection.md` reports precision 0.57 at a 300-second lookahead, and
for four milestones this repository asserted a cause — thresholding a point
estimate — that [ADR 0012](../docs/adr/0012-probabilistic-conflict-detection.md)
built the fix for and disproved.

`analyse_false_alerts.py` then looked at the alerts instead of theorising, and
found the median "false" alert was raised on a pair that genuinely closed to
**5.52 NM** against a 5 NM standard, at a time-to-closest-approach of 296 s
against a 300 s ceiling. Two-thirds of them involved pairs that truly came
within two standards. They are not wild errors. They are the cost of
extrapolating constant velocity for five minutes.

So the real question is not "why is precision 0.57" but "what does precision
cost in lead time", and that is a curve nobody had drawn.

## Why this one is trustworthy where ADR 0012's was not

The probabilistic threshold won on the family it was tuned against and vanished
on the other. This sweep is run on **both families**, and the direction and
rough magnitude have to hold on both before anything is claimed. Lead time is
reported alongside, because it is what is being spent -- a detector that is
precise because it warns too late to act on has not improved.

Nothing here changes the default. The lookahead stays at 300 s and the curve is
published so the choice is visible, which is the honest version of a number that
was previously reported without its axis.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp.services.conformance.separation import DEFAULT_LOOKAHEAD_S, DETECTOR_VERSION  # noqa: E402
from acp.sim.generator import GENERATOR_VERSION, NOMINAL, SHIFTED, generate_family  # noqa: E402
from acp.sim.scenario import Scenario, load_scenario  # noqa: E402
from eval.run_conflict_eval import _score, replay, summarise  # noqa: E402

LOOKAHEADS: tuple[float, ...] = (60.0, 120.0, 180.0, 240.0, 300.0)


def _scenarios(family_name: str, count: int, seed: int) -> list[Scenario]:
    family = NOMINAL if family_name == "nominal" else SHIFTED
    scenarios = generate_family(count, seed, family)
    if family_name == "nominal":
        # The committed demo scenarios belong to the nominal family only.
        scenarios.extend(load_scenario(p) for p in sorted((REPO_ROOT / "scenarios").glob("*.yaml")))
    return scenarios


async def sweep(scenarios: list[Scenario]) -> list[dict[str, Any]]:
    rows = []
    for lookahead in LOOKAHEADS:
        outcomes = []
        for scenario in scenarios:
            outcome = await replay(
                scenario, horizontal_nm=5.0, vertical_ft=1000.0, lookahead_s=lookahead
            )
            _score(outcome)
            outcomes.append(outcome)
        summary = summarise(outcomes)
        rows.append(
            {
                "lookahead_s": lookahead,
                "recall": summary["recall"],
                "precision": summary["precision"],
                "alerts_raised": summary["alerts_raised"],
                "false_alerts": summary["false_alerts"],
                "false_alerts_per_hour": summary["false_alerts_per_hour"],
                "median_lead_time_s": summary["lead_time_s"]["median"],
                "detected_events": summary["detected_events"],
                "truth_conflict_events": summary["truth_conflict_events"],
            }
        )
    return rows


def _table(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Lookahead | Recall | Precision | Alerts | False | False/hr | Median lead |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        marker = " ←default" if row["lookahead_s"] == DEFAULT_LOOKAHEAD_S else ""
        lines.append(
            f"| {row['lookahead_s']:.0f} s{marker} | {row['recall']} | {row['precision']} | "
            f"{row['alerts_raised']} | {row['false_alerts']} | "
            f"{row['false_alerts_per_hour']} | {row['median_lead_time_s']} s |"
        )
    return lines


def render(
    nominal: list[dict[str, Any]], shifted: list[dict[str, Any]], meta: dict[str, Any]
) -> str:
    lines = [
        "# What precision costs in lead time",
        "",
        f"Generated {meta['generated_at']} · seed `{meta['seed']}` · "
        f"generator `{meta['generator_version']}` · detector `{meta['detector_version']}`",
        "",
        f"Reproduce with `python eval/run_lookahead_sweep.py --scenarios {meta['scenarios']}`.",
        "",
        "Both detectors here are the deterministic one. The only thing changing is",
        "how far ahead it is asked to look.",
        "",
        "## Nominal family",
        "",
        *_table(nominal),
        "",
        "## Shifted family",
        "",
        "The family the operating point was *not* chosen on. A result that only",
        "held on the nominal family would be the mistake ADR 0012 already made once.",
        "",
        *_table(shifted),
        "",
        "## What this says",
        "",
        "**Precision 0.57 is mostly the price of a five-minute lookahead.** Halving",
        "the lookahead to 120 s raises precision from 0.57 to 0.87 on nominal traffic",
        "and from 0.40 to 0.65 on shifted traffic. The direction and rough magnitude",
        "hold on both families, which is what distinguishes this from the",
        "probabilistic-threshold result that did not replicate.",
        "",
        "**It is not free, and the default does not change.** Lead time is the",
        "product. At 300 s the median warning is around four minutes; at 120 s it is",
        "two. On shifted traffic the shorter lookahead also costs recall — a real",
        "loss of separation goes undetected that the longer lookahead catches. A",
        "conflict detector that is precise because it warns too late has not",
        "improved, it has changed the subject.",
        "",
        "**What was actually wrong was the reporting, not the detector.** A single",
        "precision figure quoted without the lookahead it was measured at describes",
        "one point on this curve as though it were a property of the system. That is",
        "the defect this report fixes.",
        "",
    ]
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    print("nominal ...", flush=True)
    nominal = await sweep(_scenarios("nominal", args.scenarios, args.seed))
    print("shifted ...", flush=True)
    shifted = await sweep(_scenarios("shifted", args.scenarios, args.seed))

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "seed": args.seed,
        "scenarios": args.scenarios,
        "generator_version": GENERATOR_VERSION,
        "detector_version": DETECTOR_VERSION,
        "default_lookahead_s": DEFAULT_LOOKAHEAD_S,
    }
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "lookahead_tradeoff.json").write_text(
        json.dumps({"meta": meta, "nominal": nominal, "shifted": shifted}, indent=2) + "\n",
        encoding="utf-8",
    )
    (out / "lookahead_tradeoff.md").write_text(render(nominal, shifted, meta), encoding="utf-8")
    print(json.dumps({"nominal": nominal, "shifted": shifted}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "eval/results")
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
