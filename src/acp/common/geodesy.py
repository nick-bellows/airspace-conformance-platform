"""Spherical-earth geometry and aviation unit conversions.

Two coordinate treatments live here and they are used for different jobs:

* **Great-circle** (:func:`haversine_nm`, :func:`initial_bearing_deg`,
  :func:`destination_point`) is exact enough for anything, but every call costs
  several trigonometric evaluations.
* **Local tangent plane** (:func:`to_local_enu`, :func:`from_local_enu`) flattens
  a small neighbourhood to plain Cartesian nautical miles. Pairwise conflict
  search is O(n^2) in the worst case, so the inner loop uses this instead.

**How wrong the flat-earth shortcut is, measured rather than asserted.** Two
distinct errors matter and they are not the same size:

* *Distance from the reference point* is accurate to better than 0.1% out to a
  few hundred miles. Pinned by ``test_local_projection_agrees_with_great_circle``.
* *Distance between two points that are both offset from the reference* -- which
  is what the conflict detector actually computes -- degrades considerably
  faster, because the longitude scale factor is evaluated at the wrong latitude
  for both of them. Measured on a 4 NM separation:

  =============  ==========  ============
  Offset from    Error on a  Error
  reference      4 NM gap    (relative)
  =============  ==========  ============
  50 NM          0.026 NM    0.6%
  100 NM         0.052 NM    1.3%
  200 NM         0.110 NM    2.8%
  300 NM         0.174 NM    4.4%
  800 NM         0.610 NM    15.2%
  =============  ==========  ============

So the projection is fit for a sector a few hundred nautical miles across and
not for a continent. :class:`~acp.services.conformance.separation.SeparationMonitor`
states that as an operating limit and warns when traffic exceeds it. Real ATC
partitions airspace into sectors for exactly this kind of reason.

A spherical earth (not WGS-84) is deliberate: the ellipsoidal correction is
roughly 0.3%, smaller than the projection error above and far smaller than the
velocity-extrapolation error the conflict detector already carries.
"""

from __future__ import annotations

import math

#: Mean earth radius, converted from 6 371 008.8 m at 1852 m per nautical mile.
EARTH_RADIUS_NM = 3440.0695
FEET_PER_NM = 6076.11549
SECONDS_PER_MINUTE = 60.0


def knots_to_nm_per_second(knots: float) -> float:
    """Convert ground speed in knots to nautical miles per second."""
    return knots / 3600.0


def fpm_to_ft_per_second(fpm: float) -> float:
    """Convert a vertical rate in feet per minute to feet per second."""
    return fpm / SECONDS_PER_MINUTE


def normalize_bearing(degrees: float) -> float:
    """Wrap any angle into ``[0, 360)``.

    The explicit clamp is load-bearing, not defensive noise. For a tiny negative
    input such as -1e-64 the true result is ``360 - 1e-64``, which rounds to
    exactly 360.0 in float64 -- outside the half-open interval this function
    promises and outside the ``lt=360`` bound on the ``Bearing`` contract type,
    so it would have failed validation at runtime. Found by Hypothesis.
    """
    wrapped = degrees % 360.0
    return 0.0 if wrapped >= 360.0 else wrapped


def bearing_difference_deg(from_deg: float, to_deg: float) -> float:
    """Signed shortest turn from one bearing to another, in ``(-180, 180]``.

    Positive is a right turn. Used for turn-rate estimation, where the naive
    difference would spike by 360 every time an aircraft crosses north.
    """
    delta = (to_deg - from_deg + 180.0) % 360.0 - 180.0
    return delta + 360.0 if delta <= -180.0 else delta


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in nautical miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing along the great circle from point 1 to point 2.

    Undefined at the poles and for coincident points; both return 0.0 rather
    than raising, because a track sitting exactly on its previous position is a
    normal occurrence for a stationary aircraft.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_lambda = math.radians(lon2 - lon1)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    if x == 0.0 and y == 0.0:
        return 0.0
    return normalize_bearing(math.degrees(math.atan2(y, x)))


def destination_point(
    lat: float, lon: float, bearing_deg: float, distance_nm: float
) -> tuple[float, float]:
    """Point reached by travelling ``distance_nm`` along ``bearing_deg``.

    Returns ``(lat, lon)`` with longitude wrapped into ``[-180, 180)``.
    """
    phi1 = math.radians(lat)
    lambda1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_nm / EARTH_RADIUS_NM

    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon2 = (math.degrees(lambda2) + 180.0) % 360.0 - 180.0
    return math.degrees(phi2), lon2


def to_local_enu(ref_lat: float, ref_lon: float, lat: float, lon: float) -> tuple[float, float]:
    """Project a point to east/north nautical miles about a reference point.

    Equirectangular projection about ``(ref_lat, ref_lon)``. Accurate to well
    under a tenth of a percent inside a 100 NM radius; do not use it to span
    continents.
    """
    # Take the shortest way round. Without this, a pair straddling the
    # antimeridian differs by ~360 degrees of longitude and projects to a
    # separation of some 21 000 NM, so two adjacent aircraft near the date line
    # would never be tested for conflict. Found by Hypothesis.
    d_lon = (lon - ref_lon + 180.0) % 360.0 - 180.0
    mean_lat = math.radians((ref_lat + lat) / 2.0)
    east = math.radians(d_lon) * math.cos(mean_lat) * EARTH_RADIUS_NM
    north = math.radians(lat - ref_lat) * EARTH_RADIUS_NM
    return east, north


def from_local_enu(
    ref_lat: float, ref_lon: float, east_nm: float, north_nm: float
) -> tuple[float, float]:
    """Inverse of :func:`to_local_enu`."""
    lat = ref_lat + math.degrees(north_nm / EARTH_RADIUS_NM)
    mean_lat = math.radians((ref_lat + lat) / 2.0)
    cos_mean = math.cos(mean_lat)
    if abs(cos_mean) < 1e-12:
        return lat, ref_lon
    lon = ref_lon + math.degrees(east_nm / (EARTH_RADIUS_NM * cos_mean))
    return lat, (lon + 180.0) % 360.0 - 180.0


def project_forward(
    lat: float,
    lon: float,
    track_deg: float,
    ground_speed_kt: float,
    seconds: float,
) -> tuple[float, float]:
    """Dead-reckon a position forward at constant velocity.

    This is the physics baseline the trajectory model learns a correction to,
    and the extrapolation the conflict detector uses inside its lookahead
    window. It assumes the aircraft holds its current track and speed, which is
    exactly what makes an unmodelled turn detectable as a large residual.
    """
    return destination_point(lat, lon, track_deg, knots_to_nm_per_second(ground_speed_kt) * seconds)
