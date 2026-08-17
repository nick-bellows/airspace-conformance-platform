"""Score the conflict detector against simulator ground truth.

    python eval/run_conflict_eval.py --scenarios 60

## Why this number is trustworthy despite synthetic data

The detector consumes only the noisy observation stream, exactly as it does in
production. Ground truth comes from the noiseless simulator state, which no part
of the pipeline ever sees. So while the *traffic* is invented, the measurement
is not circular: it is a real algorithm, running on degraded input, scored
against what actually happened.

What it does **not** measure is how the detector would behave on real traffic,
because the manoeuvre distribution, the sensor error model, and the encounter
geometry are all this project's inventions. See `docs/limitations.md`.

## Definitions

**Detection.** An alert counts as detecting a truth event if it was raised for
the same aircraft pair and was still active at, or raised before, the moment
separation was actually lost. Raising an alert *after* the violation has already
begun does not count as a detection -- a warning that arrives once the aircraft
are already too close has not done its job.

**Lead time.** Seconds between the alert being raised and the violation
starting. This is the number an operations person cares about most.

**False alert.** An alert raised for a pair that never lost separation in that
scenario. Counted per hour of simulated airspace time so it can be compared
across runs of different sizes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acp.common.contracts import AlertKind, AlertState  # noqa: E402
from acp.services.conformance.alerts import AlertManager  # noqa: E402
from acp.services.conformance.runner import ConformanceRunner  # noqa: E402
from acp.services.conformance.separation import (  # noqa: E402
    DETECTOR_VERSION,
    SeparationMonitor,
)
from acp.services.track.estimator import TrackEstimator  # noqa: E402
from acp.services.track.kalman import FILTER_VERSION  # noqa: E402
from acp.sim.engine import Simulation  # noqa: E402
from acp.sim.generator import (  # noqa: E402
    GENERATOR_VERSION,
    NOMINAL,
    FamilyParameters,
    generate_family,
)
from acp.sim.scenario import SIM_VERSION, Scenario, load_scenario  # noqa: E402
from acp.sim.scenario import fingerprint as scenario_fingerprint  # noqa: E402
from eval.conflict_truth import ConflictEvent, TruthConflictFinder  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"
SCENARIO_DIR = REPO_ROOT / "scenarios"

#: Wall-clock seconds of simulation per step.
STEP_S = 1.0


@dataclass(slots=True)
class RaisedAlert:
    """One alert raised during a scenario replay."""

    key: str
    pair: tuple[str, str]
    raised_at: datetime
    lead_time_s: float | None = None
    matched: bool = False


@dataclass(slots=True)
class ScenarioOutcome:
    """What happened in one scenario."""

    scenario_id: str
    seed: int
    duration_s: float
    truth_events: list[ConflictEvent] = field(default_factory=list)
    alerts: list[RaisedAlert] = field(default_factory=list)

    @property
    def detected(self) -> int:
        return sum(1 for e in self.truth_events if _matched(e, self.alerts))

    @property
    def false_alerts(self) -> int:
        return sum(1 for a in self.alerts if not a.matched)


def _matched(event: ConflictEvent, alerts: list[RaisedAlert]) -> bool:
    return any(a.pair == event.pair and a.raised_at <= event.started_at for a in alerts)


class _CollectingPublisher:
    """Captures alerts instead of sending them to Kafka.

    The evaluation runs the real detector and the real alert lifecycle in
    process. Only the transport is replaced -- if this substituted a
    simplified detector, the number would be measuring the wrong thing.
    """

    def __init__(self) -> None:
        self.alerts: list[object] = []

    async def publish(self, topic: str, *, key: str, message: object) -> None:
        self.alerts.append(message)


async def replay(
    scenario: Scenario,
    *,
    horizontal_nm: float,
    vertical_ft: float,
    lookahead_s: float,
    probability_threshold: float | None = None,
) -> ScenarioOutcome:
    """Run one scenario through the real pipeline and score it."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    simulation = Simulation(scenario, start)
    estimator = TrackEstimator()
    publisher = _CollectingPublisher()
    runner = ConformanceRunner(
        # No subscriber: the evaluation drives absorb() and scan_now() directly
        # against the simulation clock rather than consuming Kafka. The detector
        # and the alert lifecycle are the real ones; only the transport differs.
        subscriber=None,
        publisher=publisher,  # type: ignore[arg-type]
        monitor=SeparationMonitor(
            horizontal_nm=horizontal_nm,
            vertical_ft=vertical_ft,
            lookahead_s=lookahead_s,
            probability_threshold=probability_threshold,
        ),
        manager=AlertManager(),
    )
    truth_finder = TruthConflictFinder(horizontal_nm=horizontal_nm, vertical_ft=vertical_ft)

    # Track ids are derived from the aircraft address, so this maps an alert
    # back to the pair of aircraft it is about.
    address_of = {f"trk-{spec.icao24}": spec.icao24 for spec in scenario.aircraft}
    raised: dict[str, RaisedAlert] = {}

    while not simulation.finished:
        simulation.advance(STEP_S)
        now = simulation.now

        truth_finder.observe(simulation.truth(), now)

        for report in simulation.observe():
            runner.absorb(estimator.on_report(report))

        before = len(publisher.alerts)
        await runner.scan_now(now)
        for alert in publisher.alerts[before:]:
            _record(alert, raised, address_of)  # type: ignore[arg-type]

    outcome = ScenarioOutcome(
        scenario_id=scenario.scenario_id,
        seed=scenario.seed,
        duration_s=scenario.duration_s,
        truth_events=truth_finder.events(),
        alerts=list(raised.values()),
    )
    _score(outcome)
    return outcome


def _record(alert: object, raised: dict[str, RaisedAlert], address_of: dict[str, str]) -> None:
    """Remember the first NEW for each alert key."""
    kind = getattr(alert, "kind", None)
    if kind is not AlertKind.PREDICTED_CONFLICT:
        return
    if getattr(alert, "state", None) is not AlertState.NEW:
        return
    key = str(alert.alert_key)  # type: ignore[attr-defined]
    if key in raised:
        return
    track_ids = tuple(alert.track_ids)  # type: ignore[attr-defined]
    addresses = tuple(sorted(address_of.get(t, t) for t in track_ids))
    raised[key] = RaisedAlert(
        key=key,
        pair=addresses,  # type: ignore[arg-type]
        raised_at=alert.raised_at,  # type: ignore[attr-defined]
    )


def _score(outcome: ScenarioOutcome) -> None:
    """Attach lead times and mark which alerts corresponded to a real event."""
    for event in outcome.truth_events:
        candidates = [
            a for a in outcome.alerts if a.pair == event.pair and a.raised_at <= event.started_at
        ]
        if not candidates:
            continue
        earliest = min(candidates, key=lambda a: a.raised_at)
        earliest.lead_time_s = (event.started_at - earliest.raised_at).total_seconds()
        for alert in candidates:
            alert.matched = True

    # An alert for a pair that did conflict, but raised too late to help, is
    # still not a false alarm -- the geometry was real. Mark it matched so it
    # does not inflate the false-alert rate, while leaving it uncounted as a
    # detection.
    conflicting_pairs = {e.pair for e in outcome.truth_events}
    for alert in outcome.alerts:
        if alert.pair in conflicting_pairs:
            alert.matched = True


def summarise(outcomes: list[ScenarioOutcome]) -> dict[str, object]:
    """Aggregate per-scenario outcomes into the reported metrics."""
    total_events = sum(len(o.truth_events) for o in outcomes)
    detected = sum(o.detected for o in outcomes)
    total_alerts = sum(len(o.alerts) for o in outcomes)
    false_alerts = sum(o.false_alerts for o in outcomes)
    airspace_hours = sum(o.duration_s for o in outcomes) / 3600.0

    lead_times = [a.lead_time_s for o in outcomes for a in o.alerts if a.lead_time_s is not None]

    return {
        "scenarios": len(outcomes),
        "simulated_hours": round(airspace_hours, 2),
        "truth_conflict_events": total_events,
        "detected_events": detected,
        "recall": round(detected / total_events, 4) if total_events else None,
        "alerts_raised": total_alerts,
        "false_alerts": false_alerts,
        "precision": (
            round((total_alerts - false_alerts) / total_alerts, 4) if total_alerts else None
        ),
        "false_alerts_per_hour": (
            round(false_alerts / airspace_hours, 2) if airspace_hours else None
        ),
        "lead_time_s": {
            "count": len(lead_times),
            "median": round(statistics.median(lead_times), 1) if lead_times else None,
            "p10": round(sorted(lead_times)[len(lead_times) // 10], 1)
            if len(lead_times) >= 10
            else None,
            "min": round(min(lead_times), 1) if lead_times else None,
            "max": round(max(lead_times), 1) if lead_times else None,
        },
    }


def _fingerprint(scenarios: list[Scenario]) -> str:
    """Hash of the evaluated scenario set, so a report names its own inputs.

    Delegates to `acp.sim.scenario.fingerprint` so the runner and the guarding
    test cannot compute it differently -- they used to share only a convention.
    """
    return scenario_fingerprint(scenarios)


def render_report(
    summary: dict[str, object],
    fingerprint: str,
    params: FamilyParameters,
    settings: dict[str, float],
    generated_at: datetime,
) -> str:
    lead = summary["lead_time_s"]
    assert isinstance(lead, dict)
    reproduce = (
        f"python eval/run_conflict_eval.py"
        f" --scenarios {summary['generated_scenarios']}"
        f" --family {params.name}"
        f" --seed {settings['seed']:.0f}"
        + (" --include-committed" if summary["included_committed"] else "")
    )
    return f"""# Conflict detection evaluation

Generated: {generated_at.isoformat()}

| Component | Version |
| --- | --- |
| Simulator | `{SIM_VERSION}` |
| Scenario generator | `{GENERATOR_VERSION}` |
| Track filter | `{FILTER_VERSION}` |
| Detector | `{DETECTOR_VERSION}` |

| Input | Value |
| --- | --- |
| Scenario family | `{params.name}` |
| Scenarios | {summary["scenarios"]} |
| Simulated airspace time | {summary["simulated_hours"]} hours |
| Scenario set SHA-256 (first 16) | `{fingerprint}` |
| Horizontal standard | {settings["horizontal_nm"]} NM |
| Vertical standard | {settings["vertical_ft"]} ft |
| Lookahead | {settings["lookahead_s"]} s |

## Results

| Metric | Value |
| --- | --- |
| Truth conflict events | {summary["truth_conflict_events"]} |
| Detected before violation | {summary["detected_events"]} |
| **Recall** | **{summary["recall"]}** |
| Alerts raised | {summary["alerts_raised"]} |
| False alerts | {summary["false_alerts"]} |
| **Precision** | **{summary["precision"]}** |
| False alerts per airspace hour | {summary["false_alerts_per_hour"]} |
| Median warning lead time | {lead["median"]} s |
| 10th percentile lead time | {lead["p10"]} s |
| Lead time range | {lead["min"]} - {lead["max"]} s |

## How to read this

The detector consumed **only** noisy surveillance reports. Ground truth was
computed from noiseless simulator state that no part of the pipeline observes.
The measurement is therefore not circular: a real algorithm, on degraded input,
scored against what actually happened.

**Recall** is the fraction of real losses of separation for which an alert was
raised *before* separation was lost. An alert raised after the fact is not
counted as a detection.

**Precision** is the fraction of raised alerts that concerned a pair which
really did lose separation. Encounters are generated with randomised miss
distances straddling the 5 NM standard, so the set contains close passes the
detector is supposed to stay quiet about. Without those, precision would be
meaningless.

**Lead time** is how long before the violation the alert appeared. The 10th
percentile matters more than the median: it is the bad case.

## Interpretation

**Recall of {summary["recall"]} is weaker evidence than it looks.** The generated
encounters are mostly constant-velocity approaches developing over four to seven
minutes, and the detector extrapolates at constant velocity over a
{settings["lookahead_s"]:.0f} s window. It is being tested largely on the case its
model fits exactly. The hard case -- a conflict created by a late, unforecast
turn -- is under-represented, because the generator's manoeuvres fire between 60
and 240 seconds, usually before the encounter geometry matures. Read this number
as "the geometry is implemented correctly", not as "this detector never misses".

**Precision of {summary["precision"]} is the real result, and it is not good.**
{summary["false_alerts"]} of {summary["alerts_raised"]} alerts were raised for pairs
that never actually lost separation. The cause is structural rather than a
defect: the detector applies a hard threshold to a point estimate. A pair
predicted to miss by 4.9 NM alerts and a pair predicted at 5.1 NM does not,
while the velocity estimate driving that prediction carries a couple of knots of
noise -- which over {settings["lookahead_s"]:.0f} s of extrapolation is more than a
nautical mile of position uncertainty. Encounters engineered to pass at 5-6 NM
therefore fall either side of the line close to arbitrarily.

Three changes would improve it, in descending order of value:

1. **Probabilistic detection.** The Kalman filter already maintains a position
   covariance. Propagating it through the extrapolation and alerting on the
   *probability* of violation, rather than thresholding a point estimate, is the
   principled fix and would let the threshold be set by an acceptable false
   alert rate instead of by geometry.
2. **Persistence.** Require the same pair to be detected on several consecutive
   scans before raising. Cheap, and removes alerts caused by one noisy estimate.
   The alert lifecycle already has the hysteresis machinery for it, applied on
   clearing but not on raising.
3. **Intent.** Flight plans would remove the constant-velocity assumption
   entirely. Unavailable here by construction -- the simulator's plans are never
   published -- and the largest single source of remaining error.

**{summary["false_alerts_per_hour"]} false alerts per airspace hour** is the number
to quote operationally: roughly one spurious alert per hour of traffic. That is
too many for anyone to use, and improving it is the obvious next piece of work
on this detector.

## What this does not measure

Real-world performance. The traffic, the manoeuvre distribution, the encounter
rate, and the sensor error model are all inventions of this project -- and real
airspace contains vastly fewer conflicts per flying hour than this scenario set
does, by design, so the false-alert rate here is not comparable to an
operational one. These numbers characterise the algorithm and the pipeline, not
the airspace. See `docs/limitations.md`.

## Reproduce

```
{reproduce}
```
"""


async def main_async(args: argparse.Namespace) -> int:
    from acp.sim.generator import SHIFTED

    params = {"nominal": NOMINAL, "shifted": SHIFTED}[args.family]
    scenarios = generate_family(args.scenarios, args.seed, params)
    if args.include_committed:
        scenarios.extend(load_scenario(p) for p in sorted(SCENARIO_DIR.glob("*.yaml")))

    settings = {
        "horizontal_nm": args.horizontal_nm,
        "vertical_ft": args.vertical_ft,
        "lookahead_s": args.lookahead_s,
        "seed": float(args.seed),
    }

    outcomes = []
    for index, scenario in enumerate(scenarios, start=1):
        outcomes.append(
            await replay(
                scenario,
                horizontal_nm=args.horizontal_nm,
                vertical_ft=args.vertical_ft,
                lookahead_s=args.lookahead_s,
            )
        )
        if index % 10 == 0:
            print(f"  {index}/{len(scenarios)} scenarios", file=sys.stderr)

    summary = summarise(outcomes)
    summary["generated_scenarios"] = args.scenarios
    summary["included_committed"] = args.include_committed
    generated_at = datetime.now(UTC)
    fingerprint = _fingerprint(scenarios)

    output_dir = Path(args.output_dir) if args.output_dir else RESULTS_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    report = render_report(summary, fingerprint, params, settings, generated_at)
    (output_dir / "conflict_detection.md").write_text(report, encoding="utf-8")
    (output_dir / "conflict_detection.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "sim_version": SIM_VERSION,
                "generator_version": GENERATOR_VERSION,
                "filter_version": FILTER_VERSION,
                "detector_version": DETECTOR_VERSION,
                "family": params.name,
                "seed": args.seed,
                "scenario_set_sha256_16": fingerprint,
                "settings": settings,
                "summary": summary,
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
    parser.add_argument("--scenarios", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--family", choices=("nominal", "shifted"), default="nominal")
    parser.add_argument("--horizontal-nm", type=float, default=5.0)
    parser.add_argument("--vertical-ft", type=float, default=1000.0)
    parser.add_argument("--lookahead-s", type=float, default=300.0)
    parser.add_argument(
        "--include-committed",
        action="store_true",
        help="also evaluate the hand-written scenarios in scenarios/",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "where to write the report; defaults to eval/results/. "
            "Tests point this at a temporary directory so a smoke run cannot "
            "overwrite the committed result with a four-scenario one."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import asyncio

    return asyncio.run(main_async(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
