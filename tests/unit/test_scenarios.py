"""The three demo scenarios do what the README says they do.

`scripts/demo.ps1` is the first thing a reader runs and the README makes a
specific promise about each scenario -- which aircraft alert, and roughly when.
Nothing checked those promises: `head-on-conflict` was covered end to end,
`quiet-cruise` was only asserted to contain no true loss of separation, and
`unannounced-turn` -- the scenario that demonstrates the conformance monitoring
this project is named after -- appeared in no test at all.

These run the real components in-process, without Kafka or storage, which is
enough to check *what fires and when*. `tests/e2e` covers the wiring.

The timings below are the observed ones, held to a tolerance wide enough to
survive ordinary drift and narrow enough that a detector which stopped firing,
or started firing on the controls, fails here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from acp.common.contracts import TrackUpdate
from acp.services.conformance.monitor import ConformanceMonitor, NonConformance
from acp.services.conformance.separation import Conflict, SeparationMonitor
from acp.services.track.estimator import TrackEstimator
from acp.sim.engine import Simulation
from acp.sim.scenario import load_scenario

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"
START = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


class Outcome:
    """What a scenario produced, and when."""

    def __init__(self) -> None:
        self.nonconformance: list[tuple[float, str]] = []
        self.conflicts: list[tuple[float, str]] = []

    @property
    def nonconforming_aircraft(self) -> set[str]:
        return {who for _, who in self.nonconformance}

    @property
    def conflicting_pairs(self) -> set[str]:
        return {pair for _, pair in self.conflicts}

    def first_nonconformance_s(self) -> float:
        return self.nonconformance[0][0]

    def first_conflict_s(self) -> float:
        return self.conflicts[0][0]


def _name(conflict: Conflict) -> str:
    return "/".join(
        sorted(
            [
                conflict.first_callsign or conflict.first_track_id,
                conflict.second_callsign or conflict.second_track_id,
            ]
        )
    )


def run_scenario(name: str) -> Outcome:
    """Drive one scenario through the real filter, monitor, and detector."""
    scenario = load_scenario(SCENARIO_DIR / f"{name}.yaml")
    sim = Simulation(scenario, START)
    estimator = TrackEstimator()
    conformance = ConformanceMonitor()
    separation = SeparationMonitor(horizontal_nm=5.0, vertical_ft=1000.0, lookahead_s=300.0)

    outcome = Outcome()
    live: dict[str, TrackUpdate] = {}

    while not sim.finished:
        sim.advance(scenario.sensor.report_interval_s)
        for report in sim.observe():
            update = estimator.on_report(report)
            if update is None:
                continue
            live[update.track_id] = update
            finding: NonConformance | None = conformance.observe(update)
            if finding is not None:
                outcome.nonconformance.append((sim.elapsed_s, finding.callsign or finding.track_id))
        for conflict in separation.scan(list(live.values())):
            outcome.conflicts.append((sim.elapsed_s, _name(conflict)))

    return outcome


@pytest.fixture(scope="module")
def head_on() -> Outcome:
    return run_scenario("head-on-conflict")


@pytest.fixture(scope="module")
def unannounced_turn() -> Outcome:
    return run_scenario("unannounced-turn")


@pytest.fixture(scope="module")
def quiet_cruise() -> Outcome:
    return run_scenario("quiet-cruise")


def test_head_on_conflict_alerts_on_exactly_one_pair(head_on: Outcome) -> None:
    """The README promises ACP101 and ACP202 ring, and nobody else."""
    assert head_on.conflicting_pairs == {"ACP101/ACP202"}


def test_head_on_conflict_alerts_around_two_and_a_half_minutes(head_on: Outcome) -> None:
    """ "~2½ min" in the README's scenario table."""
    assert 120.0 <= head_on.first_conflict_s() <= 180.0


def test_head_on_conflict_ignores_the_vertically_separated_aircraft(head_on: Outcome) -> None:
    """ACP303 passes 0.2 NM from the pair as they meet, 4,000 ft above.

    Both standards must breach at the same moment, and treating either alone as
    sufficient is the most common way to get this wrong.

    This assertion was worth nothing until the scenario was fixed. ACP303 used
    to start 30 NM south, crossing that point 223 s before the pair arrived, so
    its closest lateral approach was 18.8 NM -- a detector with *both* vertical
    guards deleted still passed. Two guards enforce the standard independently
    (an early rejection in `_test_pair`, and the check at closest approach), so
    only removing both makes this fail, which was confirmed by doing it.
    """
    involved = {callsign for pair in head_on.conflicting_pairs for callsign in pair.split("/")}
    assert "ACP303" not in involved


def test_the_turn_raises_a_conformance_advisory(unannounced_turn: Outcome) -> None:
    """Without this the project's namesake feature is silently dead."""
    assert unannounced_turn.nonconformance, "no non-conformance finding for an 80 degree turn"


def test_the_turn_advisory_lands_shortly_after_the_turn(unannounced_turn: Outcome) -> None:
    """The turn starts at t=240s; the prediction made just before it matures next.

    The README says "~4½ min". Observed: 4:34. The advisory cannot precede the
    turn, and waiting a full horizon past it would mean the check had stopped
    working and something else was firing.
    """
    assert 240.0 < unannounced_turn.first_nonconformance_s() <= 330.0


def test_the_turn_advisory_names_only_the_turning_aircraft(unannounced_turn: Outcome) -> None:
    """ACP502 and ACP503 fly straight and are the controls for this scenario."""
    assert unannounced_turn.nonconforming_aircraft == {"ACP501"}


def test_the_turn_scenario_never_predicts_a_conflict(unannounced_turn: Outcome) -> None:
    """It is about prediction; an unrelated conflict alert would muddy the demo."""
    assert unannounced_turn.conflicts == []


def test_the_quiet_scenario_raises_nothing_at_all(quiet_cruise: Outcome) -> None:
    """The false-alarm control: six aircraft, fifteen minutes, silence.

    `test_sim.py` proves the scenario contains no true loss of separation. This
    proves the detectors agree, which is the claim the README actually makes.
    """
    assert quiet_cruise.conflicts == []
    assert quiet_cruise.nonconformance == []
