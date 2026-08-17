"""Linear Kalman filter for aircraft state, in a local tangent plane.

## The model

State is six numbers -- position, velocity, altitude, vertical rate::

    x = [east_nm, north_nm, v_east_kt, v_north_kt, altitude_ft, vertical_rate_fpm]

and the transition is **constant velocity**: whatever the aircraft is doing now,
assume it keeps doing it. Turns and level-offs are not in the model; they are
absorbed by process noise.

## Why constant velocity and not a turning model

A constant-turn-rate model tracks a turning aircraft more tightly, at the cost
of a non-linear filter (EKF or UKF) and a model that is *worse* than this one
whenever the aircraft is flying straight, which is most of the time. The usual
production answer is an IMM -- several models running in parallel with a
probability assigned to each -- and that is genuinely the right choice for a
real tracker.

The reason not to reach for it here is more interesting than the cost. A
constant-velocity filter **lags during a turn**, and that lag is measurable: the
innovation, the gap between where the filter expected the aircraft to be and
where it was reported, spikes exactly when the aircraft does something the model
does not predict. That is the signal the conformance monitor is built on. A
filter that tracked manoeuvres perfectly would hide the very thing this system
exists to detect, so the simple model is not only cheaper here -- it is more
useful. See ADR 0006.

## Tuning

`process_noise_accel` is the assumed standard deviation of unmodelled
acceleration. Too small and the filter is over-confident and lags badly through
turns; too large and it chases sensor noise and the smoothing is wasted. The
default is set so that a standard-rate turn at cruise speed produces an
innovation the conformance monitor can see without the filter falling apart.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from acp.common.geodesy import (
    from_local_enu,
    knots_to_nm_per_second,
    normalize_bearing,
    to_local_enu,
)

# Bump when the state model, the noise model, or the defaults change in a way
# that alters filter output. A bump invalidates every committed evaluation
# report that quotes a tracking accuracy number.
FILTER_VERSION = "acp-kf-cv-v1"

Vector = npt.NDArray[np.float64]
Matrix = npt.NDArray[np.float64]

STATE_DIM = 6
_EAST, _NORTH, _VEAST, _VNORTH, _ALT, _VRATE = range(STATE_DIM)


@dataclass(frozen=True, slots=True)
class FilterTuning:
    """Noise assumptions. Every value is a claim about the world, not a knob."""

    #: Unmodelled horizontal acceleration, in knots per second. A standard-rate
    #: turn at 450 kt swings the velocity vector by roughly 23 kt/s, so this is
    #: set well below that: the filter is *meant* to be surprised by a turn.
    process_noise_accel_kt_s: float = 3.0
    #: Unmodelled vertical acceleration, in feet per minute per second.
    process_noise_vertical_fpm_s: float = 60.0
    #: Reported position error, in nautical miles. Matches the simulator's
    #: default 30 m sigma; against a real feed this would be measured.
    measurement_position_nm: float = 30.0 / 1852.0
    measurement_altitude_ft: float = 25.0
    measurement_speed_kt: float = 2.0
    #: Initial uncertainty, before any correction has been applied. Large, so
    #: the first few measurements dominate rather than the initial guess.
    initial_position_nm: float = 1.0
    initial_velocity_kt: float = 50.0
    initial_altitude_ft: float = 200.0
    initial_vertical_rate_fpm: float = 500.0


@dataclass(frozen=True, slots=True)
class FilterEstimate:
    """Filter output in the units the rest of the system speaks."""

    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    #: One-sigma horizontal position uncertainty, in metres, from the covariance.
    #: Unlike M1's constant, this responds to dropouts and to manoeuvres.
    #: This is the RSS over both horizontal axes, not a per-axis sigma.
    position_uncertainty_m: float
    #: Per-axis one-sigma velocity uncertainty, in knots. This is the term that
    #: dominates a *predicted* position: position error grows linearly in the
    #: lookahead while this stays roughly constant, so at five minutes out it
    #: is worth far more than the position uncertainty above.
    velocity_uncertainty_kt: float
    #: One-sigma altitude uncertainty, in feet.
    altitude_uncertainty_ft: float
    #: One-sigma vertical-rate uncertainty, in feet per minute.
    vertical_rate_uncertainty_fpm: float
    #: Distance between the predicted and reported position, in nautical miles,
    #: for the correction that produced this estimate. Zero on initialisation.
    #: This is the manoeuvre signal.
    innovation_nm: float


class TrackFilter:
    """A constant-velocity Kalman filter for one aircraft.

    Coordinates are east/north nautical miles about a reference point fixed at
    track initiation. Re-referencing mid-track would introduce a discontinuity
    in the state, so the reference is chosen once and the track is expected to
    stay within a few hundred miles of it.
    """

    def __init__(
        self,
        *,
        ref_lat: float,
        ref_lon: float,
        lat: float,
        lon: float,
        altitude_ft: float,
        ground_speed_kt: float,
        track_deg: float,
        vertical_rate_fpm: float,
        tuning: FilterTuning | None = None,
    ) -> None:
        self._tuning = tuning or FilterTuning()
        self._ref_lat = ref_lat
        self._ref_lon = ref_lon

        east, north = to_local_enu(ref_lat, ref_lon, lat, lon)
        heading = np.radians(track_deg)

        self._x: Vector = np.array(
            [
                east,
                north,
                # Velocity is carried in knots so the tuning constants read in
                # the units an aviator would use.
                ground_speed_kt * float(np.sin(heading)),
                ground_speed_kt * float(np.cos(heading)),
                altitude_ft,
                vertical_rate_fpm,
            ],
            dtype=np.float64,
        )
        t = self._tuning
        self._p: Matrix = np.diag(
            np.array(
                [
                    t.initial_position_nm**2,
                    t.initial_position_nm**2,
                    t.initial_velocity_kt**2,
                    t.initial_velocity_kt**2,
                    t.initial_altitude_ft**2,
                    t.initial_vertical_rate_fpm**2,
                ],
                dtype=np.float64,
            )
        )
        self._last_innovation_nm = 0.0

    # -- prediction ---------------------------------------------------------

    def _transition(self, dt_s: float) -> Matrix:
        """State transition. Velocity is knots, position nautical miles."""
        f = np.eye(STATE_DIM, dtype=np.float64)
        f[_EAST, _VEAST] = knots_to_nm_per_second(1.0) * dt_s
        f[_NORTH, _VNORTH] = knots_to_nm_per_second(1.0) * dt_s
        f[_ALT, _VRATE] = dt_s / 60.0  # fpm to feet over dt seconds
        return f

    def _process_noise(self, dt_s: float) -> Matrix:
        """Discrete white-noise acceleration.

        An unknown constant acceleration over `dt` contributes `a*dt^2/2` to
        position and `a*dt` to velocity; the block below is the covariance of
        that, which is what makes position and velocity uncertainty correlated
        rather than independent.
        """
        t = self._tuning
        q = np.zeros((STATE_DIM, STATE_DIM), dtype=np.float64)

        var_a = t.process_noise_accel_kt_s**2
        pos_per_kt = knots_to_nm_per_second(1.0)
        # Position gain from an acceleration in kt/s, expressed in NM.
        gpp = (0.5 * dt_s**2) * pos_per_kt
        gvv = dt_s
        for pos, vel in ((_EAST, _VEAST), (_NORTH, _VNORTH)):
            q[pos, pos] = gpp * gpp * var_a
            q[pos, vel] = gpp * gvv * var_a
            q[vel, pos] = gpp * gvv * var_a
            q[vel, vel] = gvv * gvv * var_a

        var_va = t.process_noise_vertical_fpm_s**2
        alt_gain = 0.5 * dt_s**2 / 60.0
        q[_ALT, _ALT] = alt_gain * alt_gain * var_va
        q[_ALT, _VRATE] = alt_gain * dt_s * var_va
        q[_VRATE, _ALT] = alt_gain * dt_s * var_va
        q[_VRATE, _VRATE] = dt_s * dt_s * var_va
        return q

    def predict(self, dt_s: float) -> None:
        """Propagate the state forward without a measurement.

        Called on every update, and called *alone* while coasting -- which is
        why the covariance grows during a dropout and the reported uncertainty
        with it.
        """
        if dt_s <= 0.0:
            return
        f = self._transition(dt_s)
        self._x = f @ self._x
        self._p = f @ self._p @ f.T + self._process_noise(dt_s)

    # -- correction ---------------------------------------------------------

    def update(
        self,
        *,
        dt_s: float,
        lat: float,
        lon: float,
        altitude_ft: float | None,
        ground_speed_kt: float | None,
        track_deg: float | None,
        vertical_rate_fpm: float | None,
    ) -> None:
        """Predict forward then fold in whatever the report actually contained.

        The measurement vector is built from the fields that are present. Real
        surveillance drops fields, and a filter that demanded a full measurement
        would either have to invent the missing ones -- biasing the estimate --
        or throw the report away.
        """
        self.predict(dt_s)

        t = self._tuning
        rows: list[int] = [_EAST, _NORTH]
        east, north = to_local_enu(self._ref_lat, self._ref_lon, lat, lon)
        z_values: list[float] = [east, north]
        variances: list[float] = [t.measurement_position_nm**2] * 2

        if altitude_ft is not None:
            rows.append(_ALT)
            z_values.append(altitude_ft)
            variances.append(t.measurement_altitude_ft**2)

        if ground_speed_kt is not None and track_deg is not None:
            heading = np.radians(track_deg)
            rows.extend((_VEAST, _VNORTH))
            z_values.extend(
                (
                    ground_speed_kt * float(np.sin(heading)),
                    ground_speed_kt * float(np.cos(heading)),
                )
            )
            variances.extend((t.measurement_speed_kt**2,) * 2)

        if vertical_rate_fpm is not None:
            rows.append(_VRATE)
            z_values.append(vertical_rate_fpm)
            variances.append((t.measurement_altitude_ft * 4.0) ** 2)

        h = np.zeros((len(rows), STATE_DIM), dtype=np.float64)
        for i, row in enumerate(rows):
            h[i, row] = 1.0
        z = np.array(z_values, dtype=np.float64)
        r = np.diag(np.array(variances, dtype=np.float64))

        innovation = z - h @ self._x
        # The horizontal part of the innovation is the manoeuvre signal: how far
        # the aircraft was from where constant velocity said it would be.
        self._last_innovation_nm = float(np.hypot(innovation[0], innovation[1]))

        s = h @ self._p @ h.T + r
        gain = self._p @ h.T @ np.linalg.inv(s)
        self._x = self._x + gain @ innovation

        # Joseph form. The textbook `(I - KH)P` is algebraically identical but
        # loses symmetry to floating-point error over thousands of updates, and
        # an asymmetric covariance eventually goes non-positive-definite and
        # takes the filter with it.
        identity = np.eye(STATE_DIM, dtype=np.float64)
        a = identity - gain @ h
        self._p = a @ self._p @ a.T + gain @ r @ gain.T

    # -- output -------------------------------------------------------------

    def estimate(self) -> FilterEstimate:
        """Current best estimate, in the units the contracts use."""
        lat, lon = from_local_enu(
            self._ref_lat, self._ref_lon, float(self._x[_EAST]), float(self._x[_NORTH])
        )
        v_east = float(self._x[_VEAST])
        v_north = float(self._x[_VNORTH])
        speed_kt = float(np.hypot(v_east, v_north))
        track = normalize_bearing(float(np.degrees(np.arctan2(v_east, v_north))))

        # One sigma over both horizontal axes, converted from NM to metres.
        position_variance_nm2 = float(self._p[_EAST, _EAST] + self._p[_NORTH, _NORTH])
        uncertainty_m = float(np.sqrt(max(0.0, position_variance_nm2))) * 1852.0

        # Per-axis, so the conflict-probability model can treat the horizontal
        # error as an isotropic 2-D Gaussian. Averaging the two axes rather
        # than summing them is the difference between a sigma and an RSS, and
        # getting it wrong would inflate every predicted uncertainty by 41%.
        velocity_variance_kt2 = 0.5 * float(self._p[_VEAST, _VEAST] + self._p[_VNORTH, _VNORTH])

        return FilterEstimate(
            lat=lat,
            lon=lon,
            altitude_ft=max(0.0, float(self._x[_ALT])),
            ground_speed_kt=speed_kt,
            track_deg=track,
            vertical_rate_fpm=float(self._x[_VRATE]),
            position_uncertainty_m=uncertainty_m,
            velocity_uncertainty_kt=math.sqrt(max(0.0, velocity_variance_kt2)),
            altitude_uncertainty_ft=math.sqrt(max(0.0, float(self._p[_ALT, _ALT]))),
            vertical_rate_uncertainty_fpm=math.sqrt(max(0.0, float(self._p[_VRATE, _VRATE]))),
            innovation_nm=self._last_innovation_nm,
        )

    @property
    def position_covariance_trace(self) -> float:
        """Sum of the horizontal position variances, in NM^2. Used by tests."""
        return float(self._p[_EAST, _EAST] + self._p[_NORTH, _NORTH])


def reference_for(lat: float, lon: float) -> tuple[float, float]:
    """Pick the tangent-plane origin for a track starting at this position.

    Snapped to a whole degree, which buys two things:

    * The origin stays within about 40 NM of where the track began, so the
      equirectangular approximation is well inside its accurate range.
    * It is a *stable* value. Using the exact initial position would give every
      track a different origin to no benefit, and re-deriving it later would
      shift the whole state vector discontinuously.

    Note that this is **not** how tracks are made comparable to each other. Each
    filter works in its own frame and reports back in latitude and longitude;
    :class:`~acp.services.conformance.separation.SeparationMonitor` re-projects
    every track into one shared frame before comparing them. Two aircraft a few
    miles apart either side of a whole degree get different origins here, and
    that is harmless.
    """
    return float(round(lat)), float(round(lon))
