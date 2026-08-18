"""What are the 29 false alerts actually?

    python eval/analyse_false_alerts.py --scenarios 120

## Why this exists

Precision is 0.57 and, until now, the repository asserted a cause: the detector
thresholds a point estimate, so marginal geometries fall on the wrong side of a
line. [ADR 0012](../docs/adr/0012-probabilistic-conflict-detection.md) tested
that by building the principled fix, and it did not survive distribution shift.
The cause is therefore unknown.

This stops theorising and looks at the alerts. For every alert the detector
raises it records the geometry it predicted, and then — from the noiseless
simulator state the detector never sees — **what the pair actually did**. The
question that matters is not how many alerts were false, it is how false they
were:

* A pair predicted to pass at 4 NM that truly passed at 5.2 NM is a detector
  working correctly against a standard that happens to be a cliff edge. Calling
  that a false alert flatters the standard, not the detector.
* A pair predicted to pass at 4 NM that truly passed at 30 NM is a real failure
  and something is wrong upstream.

Those two populations need completely different fixes, and no published number
in this repository currently distinguishes them.

## What it does not do

It does not propose a fix, and it deliberately does not tune anything. The
output is a description of a population. Any change motivated by it has to be
validated on a family this analysis did not look at, which is the lesson ADR
0012 paid for.

The replay loop below duplicates the one in `run_conflict_eval.py` rather than
importing it, because that runner returns only scored outcomes and threading
diagnostics through it would mean changing the code path that produces the
committed headline numbers. A published metric's runner should change when the
metric changes, not when someone wants a different report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp.common.contracts import AlertKind, AlertState  # noqa: E402
from acp.common.geodesy import haversine_nm  # noqa: E402
from acp.services.conformance.alerts import AlertManager  # noqa: E402
from acp.services.conformance.runner import ConformanceRunner  # noqa: E402
from acp.services.conformance.separation import SeparationMonitor  # noqa: E402
from acp.services.track.estimator import TrackEstimator  # noqa: E402
from acp.sim.engine import Simulation  # noqa: E402
from acp.sim.generator import NOMINAL, SHIFTED, generate_family  # noqa: E402
from acp.sim.scenario import Scenario, load_scenario  # noqa: E402
from eval.conflict_truth import TruthConflictFinder  # noqa: E402
from eval.run_conflict_eval import STEP_S, _CollectingPublisher  # noqa: E402

HORIZONTAL_NM = 5.0
VERTICAL_FT = 1000.0
LOOKAHEAD_S = 300.0


@dataclass(slots=True)
class AlertRecord:
    """One alert, with what was predicted and what actually happened."""

    scenario_id: str
    pair: tuple[str, str]
    predicted_horizontal_nm: float
    predicted_vertical_ft: float
    time_to_cpa_s: float
    true_min_horizontal_nm: float
    true_vertical_at_closest_ft: float
    truly_conflicted: bool

    @property
    def horizontal_error_nm(self) -> float:
        """How wrong the prediction was, signed: positive means over-optimistic."""
        return self.true_min_horizontal_nm - self.predicted_horizontal_nm


@dataclass(slots=True)
class PairTruth:
    """Closest the two aircraft genuinely came, over the whole scenario."""

    min_horizontal_nm: float = float("inf")
    vertical_at_closest_ft: float = float("inf")
    ever_conflicted: bool = False
    records: list[str] = field(default_factory=list)


async def analyse(scenario: Scenario) -> list[AlertRecord]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    simulation = Simulation(scenario, start)
    estimator = TrackEstimator()
    publisher = _CollectingPublisher()
    runner = ConformanceRunner(
        subscriber=None,
        publisher=publisher,  # type: ignore[arg-type]
        monitor=SeparationMonitor(
            horizontal_nm=HORIZONTAL_NM, vertical_ft=VERTICAL_FT, lookahead_s=LOOKAHEAD_S
        ),
        manager=AlertManager(),
    )
    truth_finder = TruthConflictFinder(horizontal_nm=HORIZONTAL_NM, vertical_ft=VERTICAL_FT)
    address_of = {f"trk-{spec.icao24}": spec.icao24 for spec in scenario.aircraft}

    truth: dict[tuple[str, str], PairTruth] = {}
    seen_keys: set[str] = set()
    raised: list[tuple[tuple[str, str], float, float, float]] = []

    while not simulation.finished:
        simulation.advance(STEP_S)
        now = simulation.now
        states = tuple(simulation.truth())
        truth_finder.observe(states, now)

        # Closest approach for every pair, from truth the detector never sees.
        for i, a in enumerate(states):
            for b in states[i + 1 :]:
                key = (a.icao24, b.icao24) if a.icao24 < b.icao24 else (b.icao24, a.icao24)
                entry = truth.setdefault(key, PairTruth())
                horizontal = haversine_nm(a.lat, a.lon, b.lat, b.lon)
                vertical = abs(a.altitude_ft - b.altitude_ft)
                if horizontal < entry.min_horizontal_nm:
                    entry.min_horizontal_nm = horizontal
                    entry.vertical_at_closest_ft = vertical
                if horizontal < HORIZONTAL_NM and vertical < VERTICAL_FT:
                    entry.ever_conflicted = True

        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))

        before = len(publisher.alerts)
        await runner.scan_now(now)
        for alert in publisher.alerts[before:]:
            if getattr(alert, "kind", None) is not AlertKind.PREDICTED_CONFLICT:
                continue
            if getattr(alert, "state", None) is not AlertState.NEW:
                continue
            key = str(alert.alert_key)  # type: ignore[attr-defined]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            evidence = alert.conflict  # type: ignore[attr-defined]
            addresses = tuple(
                sorted(address_of.get(t, t) for t in alert.track_ids)  # type: ignore[attr-defined]
            )
            raised.append(
                (
                    addresses,  # type: ignore[arg-type]
                    evidence.min_horizontal_sep_nm,
                    evidence.min_vertical_sep_ft,
                    evidence.time_to_cpa_s,
                )
            )

    records = []
    for pair, predicted_h, predicted_v, t_cpa in raised:
        entry = truth.get(pair, PairTruth())
        records.append(
            AlertRecord(
                scenario_id=scenario.scenario_id,
                pair=pair,
                predicted_horizontal_nm=predicted_h,
                predicted_vertical_ft=predicted_v,
                time_to_cpa_s=t_cpa,
                true_min_horizontal_nm=entry.min_horizontal_nm,
                true_vertical_at_closest_ft=entry.vertical_at_closest_ft,
                truly_conflicted=entry.ever_conflicted,
            )
        )
    return records


def _quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "median": None, "p10": None, "p90": None, "min": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "median": round(statistics.median(ordered), 2),
        "p10": round(ordered[len(ordered) // 10], 2),
        "p90": round(ordered[min(len(ordered) - 1, 9 * len(ordered) // 10)], 2),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
    }


def summarise(records: list[AlertRecord]) -> dict[str, Any]:
    false_alerts = [r for r in records if not r.truly_conflicted]
    true_alerts = [r for r in records if r.truly_conflicted]

    # How near the pair genuinely came, for the alerts scored as false. This is
    # the number the whole analysis exists to produce.
    near = [r for r in false_alerts if r.true_min_horizontal_nm < 2 * HORIZONTAL_NM]
    vertical_only = [
        r
        for r in false_alerts
        if r.true_min_horizontal_nm < HORIZONTAL_NM and r.true_vertical_at_closest_ft >= VERTICAL_FT
    ]
    return {
        "alerts": len(records),
        "true_alerts": len(true_alerts),
        "false_alerts": len(false_alerts),
        "false_alert_true_min_horizontal_nm": _quantiles(
            [r.true_min_horizontal_nm for r in false_alerts]
        ),
        "false_alert_predicted_horizontal_nm": _quantiles(
            [r.predicted_horizontal_nm for r in false_alerts]
        ),
        "false_alert_horizontal_error_nm": _quantiles(
            [r.horizontal_error_nm for r in false_alerts]
        ),
        "false_alert_time_to_cpa_s": _quantiles([r.time_to_cpa_s for r in false_alerts]),
        "true_alert_horizontal_error_nm": _quantiles([r.horizontal_error_nm for r in true_alerts]),
        "false_alerts_within_two_standards": len(near),
        "false_alerts_breaching_laterally_but_not_vertically": len(vertical_only),
    }


async def main_async(args: argparse.Namespace) -> int:
    family = NOMINAL if args.family == "nominal" else SHIFTED
    scenarios = generate_family(args.scenarios, args.seed, family)
    if args.family == "nominal":
        scenarios.extend(load_scenario(p) for p in sorted((REPO_ROOT / "scenarios").glob("*.yaml")))

    records: list[AlertRecord] = []
    for index, scenario in enumerate(scenarios, start=1):
        if index % 20 == 0:
            print(f"  {index}/{len(scenarios)} scenarios", flush=True)
        records.extend(await analyse(scenario))

    summary = summarise(records)
    summary["family"] = args.family
    summary["seed"] = args.seed
    summary["generated_at"] = datetime.now(UTC).isoformat()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.name}.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "false_alerts": [
                    {
                        "scenario_id": r.scenario_id,
                        "pair": list(r.pair),
                        "predicted_horizontal_nm": round(r.predicted_horizontal_nm, 3),
                        "predicted_vertical_ft": round(r.predicted_vertical_ft, 1),
                        "time_to_cpa_s": round(r.time_to_cpa_s, 1),
                        "true_min_horizontal_nm": round(r.true_min_horizontal_nm, 3),
                        "true_vertical_at_closest_ft": round(r.true_vertical_at_closest_ft, 1),
                    }
                    for r in records
                    if not r.truly_conflicted
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--family", choices=("nominal", "shifted"), default="nominal")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "eval/results")
    parser.add_argument("--name", default="false_alert_analysis")
    return parser


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
