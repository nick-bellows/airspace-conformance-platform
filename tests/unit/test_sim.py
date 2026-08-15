"""Tests for the simulator.

Two properties carry the project. **Determinism**, because every committed
evaluation number claims to be reproducible from a seed. And the **separation
of truth from observation**, because the conflict-detection metric is only
meaningful if the detector genuinely never sees the truth it is scored against.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from acp.common.geodesy import haversine_nm
from acp.sim.engine import Simulation
from acp.sim.scenario import (
    AircraftSpec,
    GeoPoint,
    InitialState,
    Scenario,
    SensorModel,
    load_scenario,
)

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "scenarios"
START = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_scenario(**overrides: object) -> Scenario:
    fields: dict[str, object] = {
        "scenario_id": "unit-test",
        "seed": 1234,
        "duration_s": 60.0,
        "reference": GeoPoint(lat=40.0, lon=-75.0),
        "aircraft": (
            AircraftSpec(
                icao24="a1b2c3",
                callsign="TEST01",
                initial=InitialState(
                    lat=40.0, lon=-75.0, altitude_ft=35000.0, ground_speed_kt=450.0, track_deg=90.0
                ),
            ),
        ),
    }
    fields.update(overrides)
    return Scenario(**fields)  # type: ignore[arg-type]


def run(scenario: Scenario, steps: int, dt: float = 1.0) -> list[tuple[str, ...]]:
    """Collect the report ids produced over `steps` steps."""
    sim = Simulation(scenario, START)
    collected = []
    for _ in range(steps):
        sim.advance(dt)
        collected.append(tuple(r.model_dump_json() for r in sim.observe()))
    return collected


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_the_same_seed_produces_byte_identical_output() -> None:
    """Every committed metric claims reproducibility from a seed. This is that claim."""
    scenario = a_scenario()
    assert run(scenario, steps=30) == run(scenario, steps=30)


def test_a_different_seed_produces_different_noise() -> None:
    """Otherwise the seed is decorative and the 'reproducible' claim is empty."""
    assert run(a_scenario(seed=1), steps=30) != run(a_scenario(seed=2), steps=30)


def test_adding_an_aircraft_does_not_disturb_the_others() -> None:
    """Per-aircraft seeds mean a scenario can be extended without invalidating
    a result already recorded for the aircraft that were in it."""
    one = a_scenario()
    two = a_scenario(
        aircraft=(
            *one.aircraft,
            AircraftSpec(
                icao24="ffffff",
                callsign="TEST02",
                initial=InitialState(
                    lat=41.0, lon=-75.0, altitude_ft=30000.0, ground_speed_kt=400.0, track_deg=270.0
                ),
            ),
        )
    )
    sim_one, sim_two = Simulation(one, START), Simulation(two, START)
    for _ in range(30):
        sim_one.advance(1.0)
        sim_two.advance(1.0)
    original = [r for r in sim_two.observe() if r.icao24 == "a1b2c3"]
    assert [r.model_dump() for r in sim_one.observe()] == [r.model_dump() for r in original]


# --------------------------------------------------------------------------
# Truth is not observation
# --------------------------------------------------------------------------


def test_observations_differ_from_truth() -> None:
    """If they were equal the sensor model would be doing nothing."""
    sim = Simulation(a_scenario(), START)
    sim.advance(1.0)
    truth = sim.truth()[0]
    report = sim.observe()[0]
    assert (report.lat, report.lon) != (truth.lat, truth.lon)


def test_observation_error_stays_within_a_plausible_envelope() -> None:
    """Noise must be small enough to be realistic and large enough to matter.

    A 30 m sigma in two axes puts essentially every sample inside 200 m; five
    sigma on the diagonal is about 212 m.
    """
    scenario = a_scenario(duration_s=400.0)
    sim = Simulation(scenario, START)
    errors_nm = []
    for _ in range(300):
        sim.advance(1.0)
        truth = {t.icao24: t for t in sim.truth()}
        for report in sim.observe():
            t = truth[report.icao24]
            errors_nm.append(haversine_nm(t.lat, t.lon, report.lat, report.lon))
    assert errors_nm, "no reports were produced"
    assert max(errors_nm) < 0.15  # ~278 m
    assert sum(errors_nm) / len(errors_nm) > 0.005  # not silently noiseless


def test_reports_are_dropped_at_roughly_the_configured_rate() -> None:
    scenario = a_scenario(duration_s=2000.0, sensor=SensorModel(dropout_probability=0.2), seed=99)
    sim = Simulation(scenario, START)
    seen = 0
    steps = 2000
    for _ in range(steps):
        sim.advance(1.0)
        seen += len(sim.observe())
    assert 0.75 < seen / steps < 0.85  # 0.8 expected, generous band for sampling


def test_no_reports_are_dropped_when_the_sensor_is_perfect() -> None:
    scenario = a_scenario(sensor=SensorModel(dropout_probability=0.0, position_sigma_m=0.0))
    sim = Simulation(scenario, START)
    for _ in range(50):
        sim.advance(1.0)
        assert len(sim.observe()) == 1


# --------------------------------------------------------------------------
# Kinematics
# --------------------------------------------------------------------------


def test_an_aircraft_covers_ground_speed_times_time() -> None:
    scenario = a_scenario(sensor=SensorModel(position_sigma_m=0.0, dropout_probability=0.0))
    sim = Simulation(scenario, START)
    start = sim.truth()[0]
    sim.advance(60.0)
    end = sim.truth()[0]
    assert haversine_nm(start.lat, start.lon, end.lat, end.lon) == pytest.approx(7.5, rel=1e-6)


def test_vertical_rate_changes_altitude() -> None:
    scenario = a_scenario(
        aircraft=(
            AircraftSpec(
                icao24="a1b2c3",
                callsign="CLIMB1",
                initial=InitialState(
                    lat=40.0,
                    lon=-75.0,
                    altitude_ft=10000.0,
                    ground_speed_kt=300.0,
                    track_deg=0.0,
                    vertical_rate_fpm=1800.0,
                ),
            ),
        )
    )
    sim = Simulation(scenario, START)
    sim.advance(60.0)
    assert sim.truth()[0].altitude_ft == pytest.approx(11800.0)
    assert sim.truth()[0].phase == "climb"


def test_altitude_never_goes_below_the_ground() -> None:
    scenario = a_scenario(
        duration_s=600.0,
        aircraft=(
            AircraftSpec(
                icao24="a1b2c3",
                callsign="DESC01",
                initial=InitialState(
                    lat=40.0,
                    lon=-75.0,
                    altitude_ft=1000.0,
                    ground_speed_kt=200.0,
                    track_deg=0.0,
                    vertical_rate_fpm=-3000.0,
                ),
            ),
        ),
    )
    sim = Simulation(scenario, START)
    for _ in range(300):
        sim.advance(1.0)
        assert sim.truth()[0].altitude_ft >= 0.0


def test_aircraft_do_not_appear_before_their_entry_time() -> None:
    scenario = a_scenario(
        duration_s=300.0,
        aircraft=(
            AircraftSpec(
                icao24="a1b2c3",
                callsign="LATE01",
                entry_time_s=100.0,
                initial=InitialState(
                    lat=40.0, lon=-75.0, altitude_ft=30000.0, ground_speed_kt=400.0, track_deg=90.0
                ),
            ),
        ),
    )
    sim = Simulation(scenario, START)
    sim.advance(50.0)
    assert sim.truth() == ()
    assert sim.observe() == ()
    sim.advance(60.0)  # now at t=110
    assert len(sim.truth()) == 1


def test_advance_rejects_a_non_positive_step() -> None:
    sim = Simulation(a_scenario(), START)
    with pytest.raises(ValueError, match="must be positive"):
        sim.advance(0.0)


def test_simulated_time_tracks_elapsed_time() -> None:
    sim = Simulation(a_scenario(), START)
    sim.advance(30.0)
    assert sim.elapsed_s == 30.0
    assert (sim.now - START).total_seconds() == 30.0
    assert not sim.finished
    sim.advance(31.0)
    assert sim.finished


# --------------------------------------------------------------------------
# Scenario loading and validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(SCENARIO_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_every_committed_scenario_loads_and_runs(path: Path) -> None:
    """A committed scenario that does not parse is a broken demo and a broken test."""
    scenario = load_scenario(path)
    sim = Simulation(scenario, START)
    for _ in range(30):
        sim.advance(1.0)
    assert sim.truth()


def test_the_head_on_scenario_actually_produces_a_conflict() -> None:
    """The scenario is only useful if the geometry does what its comment claims.

    Asserted against truth, at the crossing point: the pair must come within the
    5 NM standard while co-altitude.
    """
    scenario = load_scenario(SCENARIO_DIR / "head-on-conflict.yaml")
    sim = Simulation(scenario, START)
    closest_nm = math.inf
    while not sim.finished:
        sim.advance(1.0)
        states = {t.icao24: t for t in sim.truth()}
        a, b = states["a1b2c3"], states["d4e5f6"]
        separation = haversine_nm(a.lat, a.lon, b.lat, b.lon)
        if separation < closest_nm:
            closest_nm = separation
            vertical_at_closest = abs(a.altitude_ft - b.altitude_ft)
    assert closest_nm < 5.0
    assert vertical_at_closest < 1000.0


def test_the_quiet_scenario_never_produces_a_conflict() -> None:
    """The false-alarm control must genuinely contain no conflict."""
    scenario = load_scenario(SCENARIO_DIR / "quiet-cruise.yaml")
    sim = Simulation(scenario, START)
    while not sim.finished:
        sim.advance(1.0)
        states = list(sim.truth())
        for i, a in enumerate(states):
            for b in states[i + 1 :]:
                horizontal = haversine_nm(a.lat, a.lon, b.lat, b.lon)
                vertical = abs(a.altitude_ft - b.altitude_ft)
                assert horizontal >= 5.0 or vertical >= 1000.0, (
                    f"{a.icao24}/{b.icao24} lost separation at t={sim.elapsed_s}s"
                )


def test_duplicate_aircraft_addresses_are_rejected() -> None:
    """Two aircraft on one address silently merge into one nonsensical track."""
    spec = AircraftSpec(
        icao24="a1b2c3",
        callsign="DUP001",
        initial=InitialState(
            lat=40.0, lon=-75.0, altitude_ft=30000.0, ground_speed_kt=400.0, track_deg=90.0
        ),
    )
    other = AircraftSpec(
        icao24="a1b2c3",
        callsign="DUP002",
        initial=InitialState(
            lat=41.0, lon=-75.0, altitude_ft=30000.0, ground_speed_kt=400.0, track_deg=90.0
        ),
    )
    with pytest.raises(ValidationError, match="duplicate icao24"):
        a_scenario(aircraft=(spec, other))


def test_duplicate_callsigns_are_rejected() -> None:
    def spec(icao24: str) -> AircraftSpec:
        return AircraftSpec(
            icao24=icao24,
            callsign="SAME01",
            initial=InitialState(
                lat=40.0, lon=-75.0, altitude_ft=30000.0, ground_speed_kt=400.0, track_deg=90.0
            ),
        )

    with pytest.raises(ValidationError, match="duplicate callsign"):
        a_scenario(aircraft=(spec("a1b2c3"), spec("ffffff")))


def test_an_aircraft_that_never_enters_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at or after the end"):
        a_scenario(
            duration_s=100.0,
            aircraft=(
                AircraftSpec(
                    icao24="a1b2c3",
                    callsign="NEVER1",
                    entry_time_s=100.0,
                    initial=InitialState(
                        lat=40.0,
                        lon=-75.0,
                        altitude_ft=30000.0,
                        ground_speed_kt=400.0,
                        track_deg=90.0,
                    ),
                ),
            ),
        )


def test_a_typo_in_a_scenario_file_is_an_error_not_a_silent_default(tmp_path: Path) -> None:
    """`extra="forbid"` is what turns a misspelled key into a loud failure."""
    path = tmp_path / "typo.yaml"
    path.write_text(
        "scenario_id: typo\nseed: 1\nduration_s: 60\n"
        "reference: {lat: 40.0, lon: -75.0}\n"
        "sensor: {dropout_probabilty: 0.5}\n"  # misspelled
        "aircraft:\n"
        "  - icao24: a1b2c3\n    callsign: T1\n"
        "    initial: {lat: 40.0, lon: -75.0, altitude_ft: 30000,"
        " ground_speed_kt: 400, track_deg: 90}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="dropout_probabilty"):
        load_scenario(path)


def test_a_yaml_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- not\n- a\n- scenario\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_scenario(path)
