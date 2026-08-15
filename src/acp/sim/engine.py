"""The simulation engine: propagate truth, then degrade it into observations.

The separation between those two steps is the most important thing in this
module and the reason the project's headline metric is defensible.

`truth()` returns exactly where each aircraft is. `observe()` returns what the
sensor layer saw: noisy, sometimes missing entirely. Only `observe()` output
ever reaches the pipeline. The evaluation harness reads `truth()` on a separate
topic that no production code path consumes.

M1 propagates constant velocity only. M2 replaces `_advance_aircraft` with a
flight model that flies waypoints, climbs, turns, and holds; nothing outside
that function should need to change.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from acp.common.contracts import DataSource, SurveillanceReport, TruthState
from acp.common.geodesy import (
    fpm_to_ft_per_second,
    normalize_bearing,
    project_forward,
)
from acp.sim.scenario import SIM_VERSION, AircraftSpec, Scenario

#: One metre expressed in degrees of latitude, for converting position noise.
_DEGREES_PER_METRE = 1.0 / 111_320.0


@dataclass(frozen=True, slots=True)
class AircraftTruth:
    """Exact state of one aircraft. Never published to a production topic."""

    icao24: str
    callsign: str
    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    squawk: str | None
    phase: str
    airborne: bool


def _seed_for(scenario_seed: int, icao24: str) -> int:
    """Derive a stable per-aircraft seed.

    Each aircraft draws noise from its own generator, seeded from the scenario
    seed and its own address. That means adding or removing an aircraft does not
    shift the noise sequence of any other aircraft -- so a scenario can be
    extended without invalidating a previously recorded result for the aircraft
    that were already in it.
    """
    digest = hashlib.sha256(f"{scenario_seed}:{icao24}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class Simulation:
    """A running scenario.

    Advance it with :meth:`advance`, then read :meth:`truth` and
    :meth:`observe`. Given the same scenario the sequence of outputs is
    identical on every run and every machine.
    """

    def __init__(self, scenario: Scenario, start_time: datetime) -> None:
        self._scenario = scenario
        self._start_time = start_time
        self._elapsed_s = 0.0
        self._report_sequence = 0
        self._noise = {
            spec.icao24: random.Random(_seed_for(scenario.seed, spec.icao24))  # noqa: S311
            for spec in scenario.aircraft
        }
        self._state = {spec.icao24: self._initial_truth(spec) for spec in scenario.aircraft}

    @property
    def scenario(self) -> Scenario:
        return self._scenario

    @property
    def elapsed_s(self) -> float:
        return self._elapsed_s

    @property
    def now(self) -> datetime:
        """Simulated wall-clock time at the current step."""
        return self._start_time + timedelta(seconds=self._elapsed_s)

    @property
    def finished(self) -> bool:
        return self._elapsed_s >= self._scenario.duration_s

    @staticmethod
    def _initial_truth(spec: AircraftSpec) -> AircraftTruth:
        return AircraftTruth(
            icao24=spec.icao24,
            callsign=spec.callsign,
            lat=spec.initial.lat,
            lon=spec.initial.lon,
            altitude_ft=spec.initial.altitude_ft,
            ground_speed_kt=spec.initial.ground_speed_kt,
            track_deg=spec.initial.track_deg,
            vertical_rate_fpm=spec.initial.vertical_rate_fpm,
            squawk=spec.squawk,
            phase=_phase_for(spec.initial.vertical_rate_fpm),
            airborne=spec.entry_time_s <= 0.0,
        )

    def advance(self, dt_s: float) -> None:
        """Move the simulation forward by `dt_s` seconds."""
        if dt_s <= 0.0:
            raise ValueError("dt_s must be positive")
        self._elapsed_s += dt_s
        entry = {spec.icao24: spec.entry_time_s for spec in self._scenario.aircraft}
        self._state = {
            icao24: self._advance_aircraft(state, dt_s, self._elapsed_s >= entry[icao24])
            for icao24, state in self._state.items()
        }

    @staticmethod
    def _advance_aircraft(state: AircraftTruth, dt_s: float, airborne: bool) -> AircraftTruth:
        """Propagate one aircraft by one step.

        M1: constant ground speed along a constant track, with a constant
        vertical rate. This is the same motion model the dead-reckoning baseline
        assumes, which is exactly why M1 cannot say anything interesting about
        prediction quality -- the predictor would be exact. M2 introduces
        maneuvers, and with them the residual the model is meant to learn.
        """
        if not airborne:
            return replace(state, airborne=False)

        lat, lon = project_forward(
            state.lat, state.lon, state.track_deg, state.ground_speed_kt, dt_s
        )
        altitude_ft = max(
            0.0, state.altitude_ft + fpm_to_ft_per_second(state.vertical_rate_fpm) * dt_s
        )
        return replace(state, lat=lat, lon=lon, altitude_ft=altitude_ft, airborne=True)

    def truth(self) -> tuple[TruthState, ...]:
        """Exact state of every airborne aircraft. Evaluation only."""
        return tuple(
            TruthState(
                icao24=s.icao24,
                scenario_id=self._scenario.scenario_id,
                sim_version=SIM_VERSION,
                valid_at=self.now,
                lat=s.lat,
                lon=s.lon,
                altitude_ft=s.altitude_ft,
                ground_speed_kt=s.ground_speed_kt,
                track_deg=normalize_bearing(s.track_deg),
                vertical_rate_fpm=s.vertical_rate_fpm,
                phase=s.phase,
            )
            for s in self._state.values()
            if s.airborne
        )

    def observe(self) -> tuple[SurveillanceReport, ...]:
        """What the sensor layer saw this step.

        Reports are dropped independently per aircraft, and the survivors carry
        Gaussian error. This is the only view the pipeline is ever given.
        """
        reports = []
        sensor = self._scenario.sensor
        for state in self._state.values():
            if not state.airborne:
                continue
            rng = self._noise[state.icao24]
            if rng.random() < sensor.dropout_probability:
                continue
            reports.append(self._observe_one(state, rng))
        return tuple(reports)

    def _observe_one(self, state: AircraftTruth, rng: random.Random) -> SurveillanceReport:
        sensor = self._scenario.sensor
        # Independent east/north error, converted from metres to degrees. The
        # longitude conversion shrinks with latitude, so a fixed metre error is
        # a larger angle near the poles.
        north_deg = rng.gauss(0.0, sensor.position_sigma_m) * _DEGREES_PER_METRE
        east_m = rng.gauss(0.0, sensor.position_sigma_m)
        cos_lat = max(0.01, abs(math.cos(math.radians(state.lat))))
        east_deg = east_m * _DEGREES_PER_METRE / cos_lat

        self._report_sequence += 1
        return SurveillanceReport(
            report_id=f"{self._scenario.scenario_id}-{self._report_sequence:09d}",
            icao24=state.icao24,
            callsign=state.callsign,
            observed_at=self.now,
            lat=_clamp(state.lat + north_deg, -90.0, 90.0),
            lon=_wrap_longitude(state.lon + east_deg),
            altitude_baro_ft=state.altitude_ft + rng.gauss(0.0, sensor.altitude_sigma_ft),
            ground_speed_kt=max(0.0, state.ground_speed_kt + rng.gauss(0.0, sensor.speed_sigma_kt)),
            track_deg=normalize_bearing(state.track_deg + rng.gauss(0.0, sensor.track_sigma_deg)),
            vertical_rate_fpm=state.vertical_rate_fpm,
            squawk=state.squawk,
            on_ground=False,
            source=DataSource.SIMULATOR,
            scenario_id=self._scenario.scenario_id,
        )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wrap_longitude(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _phase_for(vertical_rate_fpm: float) -> str:
    """Coarse flight phase from vertical rate. Refined in M2."""
    if vertical_rate_fpm > 300.0:
        return "climb"
    if vertical_rate_fpm < -300.0:
        return "descent"
    return "cruise"
