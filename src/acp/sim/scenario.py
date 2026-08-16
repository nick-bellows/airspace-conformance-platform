"""Scenario definitions: the input to the simulator.

A scenario is a committed YAML file describing an airspace situation and the
sensor observing it. Everything the simulator does is derived from the scenario
plus its seed, so a scenario file plus a seed is a complete, reproducible
description of a run.

Scenarios are *deliberately authored*, not random. The point of synthetic data
is being able to say "produce a head-on conflict at 35,000 ft in nine minutes"
and get exactly that, every time, so a detector can be scored against it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from acp.common.contracts import Bearing, Icao24, Latitude, Longitude, Squawk

# Bump when the flight model or the noise model changes in a way that alters the
# output for an unchanged scenario and seed. A bump invalidates every cached
# dataset and requires regenerating each committed evaluation report.
#
# v2: manoeuvring flight model - scripted turns, altitude and speed changes.
# v1 flew constant velocity only, so any result recorded against it describes a
# different problem and must not be compared with a v2 number.
SIM_VERSION = "acp-sim-v2"


class Frozen(BaseModel):
    """Scenario models are immutable and reject unknown keys.

    Rejecting unknown keys matters more here than elsewhere: a typo in a
    hand-written YAML file would otherwise be silently ignored and the run would
    quietly use the default instead of the value the author intended.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class GeoPoint(Frozen):
    """A latitude/longitude pair."""

    lat: Latitude
    lon: Longitude


class SensorModel(Frozen):
    """How the simulated surveillance layer degrades the truth.

    Defaults are loosely representative of ADS-B rather than measured from it:
    position error of a few tens of metres, occasional dropped messages. They
    are a plausible operating point, not a calibration.
    """

    report_interval_s: Annotated[float, Field(gt=0.0)] = 1.0
    position_sigma_m: Annotated[float, Field(ge=0.0)] = 30.0
    altitude_sigma_ft: Annotated[float, Field(ge=0.0)] = 25.0
    speed_sigma_kt: Annotated[float, Field(ge=0.0)] = 2.0
    track_sigma_deg: Annotated[float, Field(ge=0.0)] = 0.5
    #: Probability that any single report is lost, independently per report.
    dropout_probability: Annotated[float, Field(ge=0.0, le=1.0)] = 0.02


class InitialState(Frozen):
    """Where an aircraft starts and how it is moving."""

    lat: Latitude
    lon: Longitude
    altitude_ft: Annotated[float, Field(ge=0.0, le=60000.0)]
    ground_speed_kt: Annotated[float, Field(ge=0.0, le=700.0)]
    track_deg: Bearing
    vertical_rate_fpm: Annotated[float, Field(ge=-8000.0, le=8000.0)] = 0.0


class TurnTo(Frozen):
    """Roll onto a new heading at a constant rate.

    3 deg/s is a *standard rate turn* -- a full circle in two minutes -- and is
    what airliners are normally vectored at below about 250 kt. At cruise speed
    the bank angle needed for 3 deg/s exceeds what passengers tolerate, so real
    high-altitude turns are slower; scenarios that care use `rate_deg_s`.
    """

    kind: Literal["turn_to"] = "turn_to"
    at_s: Annotated[float, Field(ge=0.0)]
    heading_deg: Bearing
    rate_deg_s: Annotated[float, Field(gt=0.0, le=10.0)] = 3.0


class ClimbTo(Frozen):
    """Change level and hold the new one."""

    kind: Literal["climb_to"] = "climb_to"
    at_s: Annotated[float, Field(ge=0.0)]
    altitude_ft: Annotated[float, Field(ge=0.0, le=60000.0)]
    rate_fpm: Annotated[float, Field(gt=0.0, le=8000.0)] = 1800.0


class ChangeSpeed(Frozen):
    """Accelerate or decelerate to a new ground speed."""

    kind: Literal["change_speed"] = "change_speed"
    at_s: Annotated[float, Field(ge=0.0)]
    ground_speed_kt: Annotated[float, Field(ge=0.0, le=700.0)]
    acceleration_kt_s: Annotated[float, Field(gt=0.0, le=10.0)] = 1.0


class SetSquawk(Frozen):
    """Change transponder code mid-flight, e.g. to declare an emergency."""

    kind: Literal["set_squawk"] = "set_squawk"
    at_s: Annotated[float, Field(ge=0.0)]
    squawk: Squawk


Command = Annotated[TurnTo | ClimbTo | ChangeSpeed | SetSquawk, Field(discriminator="kind")]


class AircraftSpec(Frozen):
    """One aircraft in a scenario."""

    icao24: Icao24
    callsign: str = Field(min_length=1, max_length=8)
    initial: InitialState
    squawk: Squawk | None = None
    #: Seconds after scenario start at which this aircraft appears. Lets a
    #: scenario stage arrivals rather than starting everything at once.
    entry_time_s: Annotated[float, Field(ge=0.0)] = 0.0
    #: Scripted manoeuvres, applied when the clock reaches each `at_s`.
    #:
    #: This is the aircraft's *intent*, and it is never observable. The pipeline
    #: sees only the positions that result, so a predictor cannot recover the
    #: plan -- it can only learn how aircraft in this airspace tend to behave.
    #: That is what stops the M3 model being a trivial inversion of the
    #: simulator.
    plan: tuple[Command, ...] = ()


class Scenario(Frozen):
    """A complete, reproducible airspace situation."""

    scenario_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    seed: int
    duration_s: Annotated[float, Field(gt=0.0)]
    #: Centre of the display and origin of the local tangent plane.
    reference: GeoPoint
    sensor: SensorModel = SensorModel()
    aircraft: tuple[AircraftSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicate_aircraft(self) -> Scenario:
        """Two aircraft sharing an address would collapse into one track.

        The tracker keys on `icao24`, so a duplicate is not a colourful edge
        case -- it silently merges two flight paths into a single nonsensical
        track. Cheaper to reject at load time.
        """
        addresses = [a.icao24 for a in self.aircraft]
        duplicates = {a for a in addresses if addresses.count(a) > 1}
        if duplicates:
            raise ValueError(f"duplicate icao24 in scenario: {sorted(duplicates)}")
        callsigns = [a.callsign for a in self.aircraft]
        duplicate_calls = {c for c in callsigns if callsigns.count(c) > 1}
        if duplicate_calls:
            raise ValueError(f"duplicate callsign in scenario: {sorted(duplicate_calls)}")
        return self

    @model_validator(mode="after")
    def _reject_aircraft_that_never_appear(self) -> Scenario:
        """An entry time past the end of the run is almost certainly a mistake."""
        late = [a.callsign for a in self.aircraft if a.entry_time_s >= self.duration_s]
        if late:
            raise ValueError(
                f"aircraft enter at or after the end of the scenario: {sorted(late)}; "
                f"duration_s is {self.duration_s}"
            )
        return self


def load_scenario(path: str | Path) -> Scenario:
    """Read and validate a scenario YAML file.

    Raises `pydantic.ValidationError` with the offending field named if the file
    does not describe a runnable scenario.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return Scenario.model_validate(raw)


#: Decimal places every float is rounded to before a scenario set is hashed.
#:
#: 9 places on a coordinate is about 0.1 mm, which is nine orders of magnitude
#: finer than anything that could change a 5 NM separation verdict -- and many
#: orders of magnitude coarser than the platform noise this exists to absorb.
FINGERPRINT_PLACES = 9


def _canonical(value: object) -> object:
    """Recursively round floats so a hash does not depend on the last ULP."""
    if isinstance(value, float):
        # `+ 0.0` normalises -0.0, which would otherwise serialise differently
        # from 0.0 for a value that is arithmetically identical.
        return round(value, FINGERPRINT_PLACES) + 0.0
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def fingerprint(scenarios: Sequence[Scenario]) -> str:
    """Identify a scenario set, so a committed report names its own inputs.

    ## Why this rounds before hashing

    The first version hashed `model_dump_json()` directly, and the fingerprint
    then differed between Windows and Linux. The generator itself is
    deterministic -- it draws from `random.Random`, which is platform-stable --
    but aircraft positions are computed through `math.sin`, `cos`, `asin`, and
    `atan2`, and those are libm calls whose last unit in the last place differs
    between the MSVC and glibc implementations. A difference around the
    fifteenth significant digit changed the SHA and failed the guard, on the
    first CI run this project ever had.

    That difference cannot affect any published number: 1e-15 degrees is
    sub-nanometre, and the evaluation thresholds at 5 NM. So the honest fix is
    to hash at a tolerance rather than bit-exactly. The guard still catches what
    it was built for -- a real change to what the generator produces -- and no
    longer fails on which operating system ran it.

    The limitation this leaves is recorded in `docs/limitations.md`:
    reproducibility here means "the same scenarios to within 0.1 mm", not
    "byte-identical files".
    """
    digest = hashlib.sha256()
    for scenario in scenarios:
        canonical = _canonical(scenario.model_dump(mode="json"))
        digest.update(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode())
    return digest.hexdigest()[:16]
