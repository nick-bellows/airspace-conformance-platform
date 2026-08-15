"""The simulation engine: propagate truth, then degrade it into observations.

The separation between those two steps is the most important thing in this
module and the reason the project's headline metric is defensible.

`truth()` returns exactly where each aircraft is. `observe()` returns what the
sensor layer saw: noisy, sometimes missing entirely. Only `observe()` output
ever reaches the pipeline. The evaluation harness reads `truth()` on a separate
topic that no production code path consumes.

The flight model is *kinematic*, not aerodynamic: aircraft turn, climb, and
accelerate at commanded rates, and nothing checks whether the resulting
manoeuvre is within an airframe's performance envelope. That is a deliberate
limitation -- the point is to produce plausible tracks to detect conflicts in,
not to model flight.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from acp.common.contracts import DataSource, SurveillanceReport, TruthState
from acp.common.geodesy import (
    bearing_difference_deg,
    fpm_to_ft_per_second,
    normalize_bearing,
    project_forward,
)
from acp.sim.scenario import (
    SIM_VERSION,
    AircraftSpec,
    ChangeSpeed,
    ClimbTo,
    Scenario,
    SetSquawk,
    TurnTo,
)

#: One metre expressed in degrees of latitude, for converting position noise.
_DEGREES_PER_METRE = 1.0 / 111_320.0

#: A vertical rate below this reads as level flight rather than a climb.
LEVEL_THRESHOLD_FPM = 300.0


@dataclass(frozen=True, slots=True)
class AircraftTruth:
    """Exact state of one aircraft, plus the autopilot targets driving it.

    The target fields are the aircraft's intent. They are never published on any
    topic and never reach the pipeline -- see `AircraftSpec.plan`.
    """

    icao24: str
    callsign: str
    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    squawk: str | None
    airborne: bool

    # Autopilot targets and the rates at which they are chased.
    target_track_deg: float
    turn_rate_deg_s: float
    target_altitude_ft: float
    climb_rate_fpm: float
    target_speed_kt: float
    acceleration_kt_s: float

    @property
    def phase(self) -> str:
        """Coarse flight phase, derived from what the aircraft is doing now."""
        if self.vertical_rate_fpm > LEVEL_THRESHOLD_FPM:
            return "climb"
        if self.vertical_rate_fpm < -LEVEL_THRESHOLD_FPM:
            return "descent"
        if abs(bearing_difference_deg(self.track_deg, self.target_track_deg)) > 1.0:
            return "turn"
        return "cruise"


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
        self._specs = {spec.icao24: spec for spec in scenario.aircraft}
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
            airborne=spec.entry_time_s <= 0.0,
            target_track_deg=spec.initial.track_deg,
            turn_rate_deg_s=3.0,
            # A non-zero initial vertical rate means "keep climbing"; the target
            # is set far enough away that it is never reached unless commanded.
            target_altitude_ft=(
                spec.initial.altitude_ft
                if abs(spec.initial.vertical_rate_fpm) < 1e-9
                else (60000.0 if spec.initial.vertical_rate_fpm > 0 else 0.0)
            ),
            climb_rate_fpm=abs(spec.initial.vertical_rate_fpm) or 1800.0,
            target_speed_kt=spec.initial.ground_speed_kt,
            acceleration_kt_s=1.0,
        )

    def advance(self, dt_s: float) -> None:
        """Move the simulation forward by `dt_s` seconds."""
        if dt_s <= 0.0:
            raise ValueError("dt_s must be positive")
        previous_s = self._elapsed_s
        self._elapsed_s += dt_s

        for icao24, state in self._state.items():
            spec = self._specs[icao24]
            commanded = self._apply_commands(state, spec, previous_s, self._elapsed_s)
            self._state[icao24] = self._advance_aircraft(
                commanded, dt_s, airborne=self._elapsed_s >= spec.entry_time_s
            )

    @staticmethod
    def _apply_commands(
        state: AircraftTruth, spec: AircraftSpec, from_s: float, to_s: float
    ) -> AircraftTruth:
        """Fire any plan commands whose time fell inside this step.

        Commands are edge-triggered on the interval `(from_s, to_s]` so a step
        larger than the gap between two commands still applies both, in order.
        """
        for command in spec.plan:
            if not from_s < command.at_s <= to_s:
                continue
            if isinstance(command, TurnTo):
                state = replace(
                    state,
                    target_track_deg=command.heading_deg,
                    turn_rate_deg_s=command.rate_deg_s,
                )
            elif isinstance(command, ClimbTo):
                state = replace(
                    state,
                    target_altitude_ft=command.altitude_ft,
                    climb_rate_fpm=command.rate_fpm,
                )
            elif isinstance(command, ChangeSpeed):
                state = replace(
                    state,
                    target_speed_kt=command.ground_speed_kt,
                    acceleration_kt_s=command.acceleration_kt_s,
                )
            elif isinstance(command, SetSquawk):
                state = replace(state, squawk=command.squawk)
        return state

    @staticmethod
    def _advance_aircraft(state: AircraftTruth, dt_s: float, *, airborne: bool) -> AircraftTruth:
        """Propagate one aircraft by one step.

        Each of the three axes chases its target at a bounded rate and stops
        exactly on it rather than overshooting and oscillating -- which is what
        an autopilot does, and what keeps the resulting track free of artefacts
        the detectors would otherwise have to cope with.
        """
        if not airborne:
            return replace(state, airborne=False)

        # --- heading: turn the short way, at the commanded rate ---
        to_turn = bearing_difference_deg(state.track_deg, state.target_track_deg)
        max_turn = state.turn_rate_deg_s * dt_s
        turn = math.copysign(min(abs(to_turn), max_turn), to_turn)
        track_deg = normalize_bearing(state.track_deg + turn)

        # --- speed: accelerate toward the target, never past it ---
        to_accelerate = state.target_speed_kt - state.ground_speed_kt
        max_accelerate = state.acceleration_kt_s * dt_s
        speed_kt = state.ground_speed_kt + math.copysign(
            min(abs(to_accelerate), max_accelerate), to_accelerate
        )

        # --- altitude: climb or descend toward the target and level off ---
        to_climb_ft = state.target_altitude_ft - state.altitude_ft
        max_climb_ft = fpm_to_ft_per_second(state.climb_rate_fpm) * dt_s
        climb_ft = math.copysign(min(abs(to_climb_ft), max_climb_ft), to_climb_ft)
        altitude_ft = max(0.0, state.altitude_ft + climb_ft)
        # Reported vertical rate is the rate actually achieved this step, so it
        # goes to zero the moment the aircraft levels off rather than lingering.
        vertical_rate_fpm = (climb_ft / dt_s) * 60.0 if dt_s > 0 else 0.0

        # Move along the average of the entry and exit headings. Using either
        # endpoint alone biases a turning aircraft consistently to one side,
        # which accumulates into a visible position error over a long turn.
        mid_track = normalize_bearing(state.track_deg + turn / 2.0)
        mid_speed = (state.ground_speed_kt + speed_kt) / 2.0
        lat, lon = project_forward(state.lat, state.lon, mid_track, mid_speed, dt_s)

        return replace(
            state,
            lat=lat,
            lon=lon,
            altitude_ft=altitude_ft,
            ground_speed_kt=speed_kt,
            track_deg=track_deg,
            vertical_rate_fpm=vertical_rate_fpm,
            airborne=True,
        )

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
