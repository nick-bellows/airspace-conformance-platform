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

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from acp.common.contracts import Bearing, Icao24, Latitude, Longitude, Squawk

# Bump when the flight model or the noise model changes in a way that alters the
# output for an unchanged scenario and seed. A bump invalidates every cached
# dataset and requires regenerating each committed evaluation report.
SIM_VERSION = "acp-sim-v1"


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


class AircraftSpec(Frozen):
    """One aircraft in a scenario."""

    icao24: Icao24
    callsign: str = Field(min_length=1, max_length=8)
    initial: InitialState
    squawk: Squawk | None = None
    #: Seconds after scenario start at which this aircraft appears. Lets a
    #: scenario stage arrivals rather than starting everything at once.
    entry_time_s: Annotated[float, Field(ge=0.0)] = 0.0


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
