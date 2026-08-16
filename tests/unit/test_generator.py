"""Tests for the evaluation scenario generator.

Every committed metric is derived from scenarios this module produces, so a
change here silently changes the numbers. Two properties matter: the output is
**reproducible from a seed**, and the encounter set contains genuine positives
*and* genuine negatives.

That second property is the one worth defending. A generator that only produced
conflicts would make recall trivially perfect and precision meaningless, because
the detector would never be shown a close pass it is supposed to ignore.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from acp.common.geodesy import haversine_nm
from acp.sim.engine import Simulation
from acp.sim.generator import (
    NOMINAL,
    SHIFTED,
    TRAINING,
    generate_encounter,
    generate_family,
)
from acp.sim.scenario import fingerprint, load_scenario

START = datetime(2026, 1, 1, tzinfo=UTC)


def closest_approach(scenario: object) -> tuple[float, float]:
    """Minimum horizontal separation of the staged pair, and the vertical gap there."""
    sim = Simulation(scenario, START)  # type: ignore[arg-type]
    first, second = scenario.aircraft[0].icao24, scenario.aircraft[1].icao24  # type: ignore[attr-defined]
    best_nm = float("inf")
    vertical_at_best = 0.0
    while not sim.finished:
        sim.advance(1.0)
        states = {t.icao24: t for t in sim.truth()}
        if first not in states or second not in states:
            continue
        a, b = states[first], states[second]
        horizontal = haversine_nm(a.lat, a.lon, b.lat, b.lon)
        if horizontal < best_nm:
            best_nm = horizontal
            vertical_at_best = abs(a.altitude_ft - b.altitude_ft)
    return best_nm, vertical_at_best


# --------------------------------------------------------------------------
# Reproducibility
# --------------------------------------------------------------------------


def test_the_same_seed_and_index_produce_the_same_scenario() -> None:
    """Every committed metric claims reproducibility. This is that claim."""
    first = generate_encounter(3, 20260815, NOMINAL)
    second = generate_encounter(3, 20260815, NOMINAL)
    assert first.model_dump_json() == second.model_dump_json()


def test_a_different_seed_produces_a_different_scenario() -> None:
    assert generate_encounter(3, 1, NOMINAL) != generate_encounter(3, 2, NOMINAL)


def test_a_different_index_produces_a_different_scenario() -> None:
    assert generate_encounter(1, 7, NOMINAL) != generate_encounter(2, 7, NOMINAL)


def test_a_family_is_reproducible() -> None:
    left = generate_family(5, 42, NOMINAL)
    right = generate_family(5, 42, NOMINAL)
    assert [s.model_dump_json() for s in left] == [s.model_dump_json() for s in right]


def test_scenario_ids_are_unique_within_a_family() -> None:
    scenarios = generate_family(20, 99, NOMINAL)
    assert len({s.scenario_id for s in scenarios}) == 20


# --------------------------------------------------------------------------
# The scenarios are valid and runnable
# --------------------------------------------------------------------------


def test_every_generated_scenario_runs() -> None:
    for scenario in generate_family(10, 5, NOMINAL):
        sim = Simulation(scenario, START)
        for _ in range(30):
            sim.advance(1.0)
        assert sim.truth()


def test_a_generated_scenario_stages_a_pair_plus_background_traffic() -> None:
    scenario = generate_encounter(0, 20260815, NOMINAL)
    assert len(scenario.aircraft) >= 4
    assert scenario.aircraft[0].callsign.endswith("A")
    assert scenario.aircraft[1].callsign.endswith("B")


def test_aircraft_addresses_are_unique() -> None:
    """Duplicates would merge two flight paths into one nonsensical track."""
    for scenario in generate_family(20, 11, NOMINAL):
        addresses = [a.icao24 for a in scenario.aircraft]
        assert len(set(addresses)) == len(addresses)


# --------------------------------------------------------------------------
# The set contains both positives and negatives
# --------------------------------------------------------------------------


def test_the_encounter_set_contains_real_conflicts() -> None:
    """Without positives there is nothing for recall to measure."""
    outcomes = [closest_approach(s) for s in generate_family(25, 20260815, NOMINAL)]
    conflicts = [h for h, v in outcomes if h < 5.0 and v < 1000.0]
    assert conflicts, "no generated encounter actually violated separation"


def test_the_encounter_set_contains_close_passes_that_are_not_conflicts() -> None:
    """Without negatives, precision is meaningless: a detector that always
    fires would score perfectly."""
    outcomes = [closest_approach(s) for s in generate_family(25, 20260815, NOMINAL)]
    near_misses = [h for h, v in outcomes if not (h < 5.0 and v < 1000.0)]
    assert near_misses, "every generated encounter was a conflict"


def test_the_staged_pair_actually_converges() -> None:
    """The geometry must bring them together, or the encounter is not one."""
    scenario = generate_encounter(0, 20260815, NOMINAL)
    sim = Simulation(scenario, START)
    first, second = scenario.aircraft[0].icao24, scenario.aircraft[1].icao24
    states = {t.icao24: t for t in sim.truth()}
    initial = haversine_nm(
        states[first].lat, states[first].lon, states[second].lat, states[second].lon
    )
    closest, _ = closest_approach(scenario)
    assert closest < initial / 2.0


# --------------------------------------------------------------------------
# Families differ, which is what makes the M3 shift test meaningful
# --------------------------------------------------------------------------


def test_the_shifted_family_is_genuinely_different() -> None:
    """Train on one, evaluate on the other. If they overlap, the test is empty."""
    assert set(SHIFTED.flight_levels).isdisjoint(NOMINAL.flight_levels)
    assert SHIFTED.manoeuvre_probability > NOMINAL.manoeuvre_probability
    assert SHIFTED.background_aircraft[1] > NOMINAL.background_aircraft[1]


def test_shifted_scenarios_use_the_shifted_flight_levels() -> None:
    """Checked on the lead aircraft and the background traffic.

    The second aircraft of the pair is deliberately offset off the flight-level
    grid, because the vertical offset is what decides whether the encounter is a
    conflict, so it is excluded here.
    """
    scenario = generate_encounter(0, 20260815, SHIFTED)
    on_grid = [scenario.aircraft[0], *scenario.aircraft[2:]]
    levels = {int(a.initial.altitude_ft / 100) for a in on_grid}
    assert levels.issubset(set(SHIFTED.flight_levels))
    assert levels.isdisjoint(set(NOMINAL.flight_levels))


def test_shifted_scenarios_carry_the_family_name() -> None:
    assert "shifted" in generate_encounter(0, 1, SHIFTED).scenario_id
    assert "nominal" in generate_encounter(0, 1, NOMINAL).scenario_id


def test_the_nominal_family_still_produces_the_scenarios_the_report_was_measured_on() -> None:
    """Guards the committed conflict-detection result.

    `eval/results/conflict_detection.md` records the SHA-256 of the scenario set
    it scored. If a generator change alters what NOMINAL produces for that seed,
    the committed numbers describe traffic that no longer exists and the report
    is silently stale. This recomputes the fingerprint the same way the
    evaluation does and compares it against the committed one.

    If this fails, regenerate the report -- never edit the committed
    fingerprint to match. Whether to also bump `GENERATOR_VERSION` depends on
    *which* input moved, and the fingerprint covers two:

    - **The generator changed.** Bump it. The constant seeds the per-scenario
      RNG, so a bump is itself a generation change and invalidates every
      committed report, which is the point.
    - **A committed scenario in `scenarios/` changed.** Do not bump it.
      Generation is untouched and 120 unrelated scenarios would churn for
      nothing. Regenerating updates the fingerprint on its own.

    The second case is not hypothetical: `head-on-conflict.yaml` was edited to
    move its vertically-separated aircraft into a real 0.2 NM near-pair, and
    this test caught it. Every published metric came back identical -- the
    detector was not fooled -- so only the fingerprint moved.
    """
    committed = json.loads(
        (Path(__file__).resolve().parents[2] / "eval/results/conflict_detection.json").read_text(
            encoding="utf-8"
        )
    )
    scenarios = generate_family(
        committed["summary"]["generated_scenarios"], committed["seed"], NOMINAL
    )
    if committed["summary"]["included_committed"]:
        scenario_dir = Path(__file__).resolve().parents[2] / "scenarios"
        scenarios.extend(load_scenario(p) for p in sorted(scenario_dir.glob("*.yaml")))

    assert fingerprint(scenarios) == committed["scenario_set_sha256_16"], (
        "NOMINAL scenario generation has changed; eval/results/conflict_detection.md "
        "no longer describes the traffic it claims to. Regenerate it and bump "
        "GENERATOR_VERSION."
    )


def test_the_training_family_contains_enough_manoeuvres_to_learn_from() -> None:
    """The reason TRAINING exists at all.

    NOMINAL gives one manoeuvre to at most two aircraft per scenario, which left
    a held-out set with seventeen manoeuvring samples. A model cannot be
    evaluated on seventeen samples and neither can a baseline.
    """
    scenarios = generate_family(5, 20260815, TRAINING)
    with_plans = [a for s in scenarios for a in s.aircraft if a.plan]
    total_commands = sum(len(a.plan) for s in scenarios for a in s.aircraft)

    assert len(with_plans) >= 20, "too few manoeuvring aircraft to train on"
    assert total_commands >= 50


def test_the_conflict_families_leave_background_traffic_alone() -> None:
    """NOMINAL's background aircraft must stay unplanned, or its fingerprint
    changes and the committed conflict report goes stale."""
    for scenario in generate_family(5, 20260815, NOMINAL):
        for aircraft in scenario.aircraft:
            if aircraft.callsign.startswith("BKG"):
                assert aircraft.plan == ()


def test_a_family_returns_exactly_the_requested_count() -> None:
    """Duplicate addresses cause a resample; silently returning fewer would
    quietly shrink an evaluation set and change every metric derived from it."""
    assert len(generate_family(17, 3, NOMINAL)) == 17
