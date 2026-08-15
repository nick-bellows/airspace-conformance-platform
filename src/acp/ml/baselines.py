"""Physics baselines for trajectory prediction.

Every model number in this project is reported next to these. That is not
politeness -- it is the only way to tell whether a model is doing anything. A
learned predictor that beats persistence but loses to dead reckoning has learned
less than a straight line, and without the baseline printed beside it the error
figure would look respectable.

Three baselines, in increasing order of how much physics they use:

* **Persistence** -- the aircraft does not move. A floor, not a contender. It
  exists so that a model which somehow scores worse than *nothing* is visible.
* **Dead reckoning** -- constant ground speed along the current track, constant
  vertical rate. This is the one that matters: it is the assumption the Kalman
  filter and the conflict detector both make, and it is what the learned model
  predicts a correction to.
* **Constant turn** -- as dead reckoning, but continues the current turn rate.
  Better through a sustained turn, worse when a turn ends, which is most of the
  time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from acp.common.geodesy import (
    destination_point,
    fpm_to_ft_per_second,
    haversine_nm,
    initial_bearing_deg,
    knots_to_nm_per_second,
    normalize_bearing,
)


@dataclass(frozen=True, slots=True)
class Prediction:
    """Where an aircraft is expected to be, and when."""

    lat: float
    lon: float
    altitude_ft: float
    horizon_s: float


@dataclass(frozen=True, slots=True)
class KinematicState:
    """The observable state a predictor is allowed to use.

    Deliberately narrow: this is what a filtered track update carries. Anything
    absent from here -- flight plans, clearances, the simulator's intent -- is
    unavailable to every predictor in this module and to the learned model, by
    construction rather than by discipline.
    """

    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    turn_rate_deg_s: float


def persistence(state: KinematicState, horizon_s: float) -> Prediction:
    """The aircraft stays exactly where it is. A floor to measure against."""
    return Prediction(
        lat=state.lat, lon=state.lon, altitude_ft=state.altitude_ft, horizon_s=horizon_s
    )


def dead_reckon(state: KinematicState, horizon_s: float) -> Prediction:
    """Constant velocity: hold this track, speed, and vertical rate.

    The reference prediction for the whole project. The learned model outputs a
    correction to this, so a model that outputs zero degrades exactly to here.
    """
    lat, lon = destination_point(
        state.lat,
        state.lon,
        state.track_deg,
        knots_to_nm_per_second(state.ground_speed_kt) * horizon_s,
    )
    return Prediction(
        lat=lat,
        lon=lon,
        altitude_ft=max(
            0.0, state.altitude_ft + fpm_to_ft_per_second(state.vertical_rate_fpm) * horizon_s
        ),
        horizon_s=horizon_s,
    )


def constant_turn(state: KinematicState, horizon_s: float, *, steps: int = 12) -> Prediction:
    """Continue the current turn rate as well as the current speed.

    Integrated in steps rather than in closed form: the closed-form arc is
    exact only on a flat earth, and stepping keeps it consistent with the
    spherical geometry the rest of the system uses. Twelve steps over a minute
    is well inside the error of everything else here.
    """
    if abs(state.turn_rate_deg_s) < 1e-6:
        return dead_reckon(state, horizon_s)

    dt = horizon_s / steps
    lat, lon = state.lat, state.lon
    track = state.track_deg
    leg_nm = knots_to_nm_per_second(state.ground_speed_kt) * dt

    for _ in range(steps):
        # Advance along the average heading over the step, for the same reason
        # the simulator does: using either endpoint biases the arc to one side.
        mid_track = normalize_bearing(track + state.turn_rate_deg_s * dt / 2.0)
        lat, lon = destination_point(lat, lon, mid_track, leg_nm)
        track = normalize_bearing(track + state.turn_rate_deg_s * dt)

    return Prediction(
        lat=lat,
        lon=lon,
        altitude_ft=max(
            0.0, state.altitude_ft + fpm_to_ft_per_second(state.vertical_rate_fpm) * horizon_s
        ),
        horizon_s=horizon_s,
    )


def along_cross_track_error(
    reference: Prediction, actual_lat: float, actual_lon: float, track_deg: float
) -> tuple[float, float]:
    """Decompose a prediction error into along-track and cross-track components.

    Both in nautical miles, relative to the aircraft's heading. Positive
    along-track means the aircraft got further than predicted; positive
    cross-track means it ended up to the right of the predicted point.

    This decomposition is what the model learns, rather than raw latitude and
    longitude offsets. Two reasons: the components mean the same thing wherever
    on earth the aircraft is, and they separate two physically different errors
    -- being wrong about *speed* from being wrong about *heading* -- which a
    lat/lon target would smear together.
    """
    distance = haversine_nm(reference.lat, reference.lon, actual_lat, actual_lon)
    if distance < 1e-9:
        return 0.0, 0.0
    bearing = initial_bearing_deg(reference.lat, reference.lon, actual_lat, actual_lon)
    relative = math.radians(bearing - track_deg)
    return distance * math.cos(relative), distance * math.sin(relative)


def apply_along_cross(
    reference: Prediction, along_nm: float, cross_nm: float, track_deg: float
) -> tuple[float, float]:
    """Inverse of :func:`along_cross_track_error`: offset a point by a residual."""
    distance = math.hypot(along_nm, cross_nm)
    if distance < 1e-9:
        return reference.lat, reference.lon
    bearing = normalize_bearing(track_deg + math.degrees(math.atan2(cross_nm, along_nm)))
    return destination_point(reference.lat, reference.lon, bearing, distance)
