"""Generate randomised scenarios for evaluation.

Two committed scenarios are enough to demo and to smoke-test, and nowhere near
enough to measure precision and recall. This module produces families of
scenarios from a seed so the evaluation runs over dozens of encounters rather
than one.

## The design decision that matters

Encounters are generated with a **randomised miss distance**, and no attempt is
made to force a violation. Some pairs breach the standards, some pass at 6 NM,
some at 15. Ground truth then decides which is which by inspecting the actual
trajectories.

The alternative -- generating only genuine conflicts -- would make recall easy
and precision meaningless, because the detector would never be shown a close
pass it is supposed to stay quiet about. A detector that alerts on everything
scores perfectly against a set containing only positives. Mixing in near misses
is what makes the false-positive rate a real number.

Aircraft also manoeuvre during the encounter, so a straight-line prediction is
sometimes wrong. That is intentional: the detector assumes constant velocity,
and the evaluation should measure what that assumption costs.
"""

from __future__ import annotations

import contextlib
import random
from dataclasses import dataclass

from acp.common.geodesy import destination_point, normalize_bearing
from acp.sim.scenario import (
    AircraftSpec,
    ChangeSpeed,
    ClimbTo,
    Command,
    GeoPoint,
    InitialState,
    Scenario,
    SensorModel,
    TurnTo,
)

#: Bump when generation changes in a way that alters the scenarios a seed
#: produces. A bump invalidates every committed evaluation report.
GENERATOR_VERSION = "acp-gen-v1"

_CENTRE_LAT = 40.60
_CENTRE_LON = -75.50


@dataclass(frozen=True, slots=True)
class FamilyParameters:
    """The knobs that define a scenario family.

    Two families with different values are the basis of the M3 distribution
    shift test: train on one, evaluate on the other, and report the gap.
    """

    name: str
    #: Aircraft not involved in the staged encounter.
    background_aircraft: tuple[int, int] = (2, 5)
    speed_kt: tuple[float, float] = (380.0, 500.0)
    #: Flight levels, in hundreds of feet, restricted to the 1000 ft grid.
    flight_levels: tuple[int, ...] = (300, 310, 320, 330, 340, 350, 360, 370, 380, 390)
    #: How close the staged pair is aimed at passing, in nautical miles.
    #:
    #: The range straddles the 5 NM standard deliberately. Roughly 40% of
    #: encounters end up inside it and the rest are close passes the detector is
    #: supposed to stay quiet about. Generating only violations would make
    #: recall trivial and precision meaningless; generating mostly near misses
    #: would need hundreds of scenarios to accumulate enough positive events to
    #: say anything about recall. This is the compromise, and it is a
    #: measurement decision rather than a modelling one -- real traffic has
    #: vastly fewer conflicts than this per flying hour.
    miss_distance_nm: tuple[float, float] = (0.0, 12.0)
    #: Angle between the two tracks. 180 is head-on, 90 is a crossing.
    crossing_angle_deg: tuple[float, float] = (30.0, 180.0)
    #: Probability that each aircraft is given a manoeuvre part-way through.
    manoeuvre_probability: float = 0.35
    #: Vertical offset between the pair. Straddles the 1000 ft standard for the
    #: same reason the lateral offset straddles 5 NM.
    vertical_offset_ft: tuple[float, float] = (0.0, 1400.0)
    duration_s: float = 600.0


#: The default family: mid-altitude en-route traffic with occasional manoeuvres.
NOMINAL = FamilyParameters(name="nominal")

#: A deliberately different family for distribution-shift testing at M3: denser,
#: faster, more manoeuvring, and using a different band of flight levels.
SHIFTED = FamilyParameters(
    name="shifted",
    background_aircraft=(4, 8),
    speed_kt=(300.0, 560.0),
    flight_levels=(240, 250, 260, 270, 280, 290, 400, 410),
    crossing_angle_deg=(15.0, 180.0),
    manoeuvre_probability=0.7,
    vertical_offset_ft=(0.0, 1800.0),
)


def _address(rng: random.Random) -> str:
    """A random 24-bit ICAO address as lowercase hex."""
    return f"{rng.getrandbits(24):06x}"


def generate_encounter(index: int, seed: int, params: FamilyParameters = NOMINAL) -> Scenario:
    """One staged two-aircraft encounter plus background traffic.

    The pair is placed so that, flying straight, they arrive at the same point
    at the same time, offset laterally by the sampled miss distance. Whether
    that constitutes a loss of separation depends on the miss distance and the
    vertical offset, and is decided by ground truth rather than asserted here.
    """
    # Seeded from the version, run seed, and index so a given (seed, index)
    # always yields the same encounter, and a generator change is a visible
    # version bump rather than a silent shift in the evaluation set.
    rng = random.Random(f"{GENERATOR_VERSION}:{seed}:{index}")  # noqa: S311 - not cryptographic

    speed_a = rng.uniform(*params.speed_kt)
    speed_b = rng.uniform(*params.speed_kt)
    level = rng.choice(params.flight_levels) * 100.0
    vertical_offset = rng.uniform(*params.vertical_offset_ft)
    miss_nm = rng.uniform(*params.miss_distance_nm)
    crossing = rng.uniform(*params.crossing_angle_deg)
    # Time until the pair reaches the meeting point. Long enough that the
    # detector gets a real look-ahead window, short enough to stay in the run.
    time_to_meet_s = rng.uniform(240.0, 420.0)

    # Both aircraft are aimed at the same point. Aircraft A tracks due east;
    # aircraft B approaches on a bearing `crossing` degrees away from it.
    track_a = rng.uniform(0.0, 359.0)
    track_b = normalize_bearing(track_a + crossing)

    lat_a, lon_a = destination_point(
        _CENTRE_LAT,
        _CENTRE_LON,
        normalize_bearing(track_a + 180.0),
        speed_a * time_to_meet_s / 3600.0,
    )
    lat_b, lon_b = destination_point(
        _CENTRE_LAT,
        _CENTRE_LON,
        normalize_bearing(track_b + 180.0),
        speed_b * time_to_meet_s / 3600.0,
    )
    # Displace B perpendicular to its own track by the sampled miss distance,
    # which is what makes some encounters conflicts and others close passes.
    lat_b, lon_b = destination_point(lat_b, lon_b, normalize_bearing(track_b + 90.0), miss_nm)

    aircraft = [
        AircraftSpec(
            icao24=_address(rng),
            callsign=f"ENC{index:03d}A",
            initial=InitialState(
                lat=lat_a,
                lon=lon_a,
                altitude_ft=level,
                ground_speed_kt=speed_a,
                track_deg=normalize_bearing(track_a),
            ),
            plan=_maybe_manoeuvre(rng, params, track_a, level, speed_a),
        ),
        AircraftSpec(
            icao24=_address(rng),
            callsign=f"ENC{index:03d}B",
            initial=InitialState(
                lat=lat_b,
                lon=lon_b,
                altitude_ft=level + vertical_offset,
                ground_speed_kt=speed_b,
                track_deg=normalize_bearing(track_b),
            ),
            plan=_maybe_manoeuvre(rng, params, track_b, level + vertical_offset, speed_b),
        ),
    ]
    aircraft.extend(_background(rng, params, len(aircraft)))

    return Scenario(
        scenario_id=f"gen-{params.name}-enc-{index:04d}",
        description=(
            f"Generated encounter: {crossing:.0f} deg crossing, "
            f"{miss_nm:.1f} NM lateral offset, {vertical_offset:.0f} ft vertical offset."
        ),
        seed=seed * 1000 + index,
        duration_s=params.duration_s,
        reference=GeoPoint(lat=_CENTRE_LAT, lon=_CENTRE_LON),
        sensor=SensorModel(),
        aircraft=tuple(aircraft),
    )


def _maybe_manoeuvre(
    rng: random.Random, params: FamilyParameters, track_deg: float, level_ft: float, speed_kt: float
) -> tuple[Command, ...]:
    """Occasionally give an aircraft something to do mid-flight.

    These are the events the predictor cannot see coming: the command list is
    intent, and intent is never published. From the pipeline's point of view the
    aircraft simply starts turning.
    """
    if rng.random() >= params.manoeuvre_probability:
        return ()

    at_s = rng.uniform(60.0, 240.0)
    choice = rng.random()
    if choice < 0.45:
        return (
            TurnTo(
                at_s=at_s,
                heading_deg=normalize_bearing(track_deg + rng.uniform(-60.0, 60.0)),
                rate_deg_s=rng.uniform(1.0, 3.0),
            ),
        )
    if choice < 0.8:
        step = rng.choice((-2000.0, -1000.0, 1000.0, 2000.0))
        return (
            ClimbTo(
                at_s=at_s,
                altitude_ft=max(1000.0, min(45000.0, level_ft + step)),
                rate_fpm=rng.uniform(800.0, 2200.0),
            ),
        )
    return (
        ChangeSpeed(
            at_s=at_s,
            ground_speed_kt=max(200.0, speed_kt + rng.uniform(-60.0, 60.0)),
            acceleration_kt_s=rng.uniform(0.5, 2.0),
        ),
    )


def _background(rng: random.Random, params: FamilyParameters, existing: int) -> list[AircraftSpec]:
    """Traffic that fills the airspace without being part of the encounter.

    Placed on random bearings 40-120 NM out and pointed outward, so they are
    unlikely to conflict with anything. Any that happen to converge are handled
    correctly anyway, because ground truth is computed rather than assumed.
    """
    count = rng.randint(*params.background_aircraft)
    specs = []
    for i in range(count):
        bearing = rng.uniform(0.0, 359.0)
        lat, lon = destination_point(_CENTRE_LAT, _CENTRE_LON, bearing, rng.uniform(40.0, 120.0))
        specs.append(
            AircraftSpec(
                icao24=_address(rng),
                callsign=f"BKG{existing + i:03d}",
                initial=InitialState(
                    lat=lat,
                    lon=lon,
                    altitude_ft=rng.choice(params.flight_levels) * 100.0,
                    ground_speed_kt=rng.uniform(*params.speed_kt),
                    # Pointing away from the centre.
                    track_deg=normalize_bearing(bearing + rng.uniform(-45.0, 45.0)),
                ),
            )
        )
    return specs


def generate_family(count: int, seed: int, params: FamilyParameters = NOMINAL) -> list[Scenario]:
    """A whole family of encounters from one seed."""
    scenarios: list[Scenario] = []
    index = 0
    attempts = 0
    # Duplicate ICAO addresses are possible by chance and rejected by the
    # scenario validator, so skip and resample rather than crashing an eval run
    # a hundred scenarios in.
    while len(scenarios) < count and attempts < count * 10:
        attempts += 1
        with contextlib.suppress(ValueError):
            scenarios.append(generate_encounter(index, seed, params))
        index += 1
    if len(scenarios) < count:
        raise RuntimeError(f"only generated {len(scenarios)} of {count} scenarios")
    return scenarios
