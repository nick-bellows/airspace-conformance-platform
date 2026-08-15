"""Property tests for the geometry primitives.

These are invariants, not example checks: Hypothesis searches for the pole,
antimeridian, and coincident-point cases that hand-written examples miss. Every
downstream distance, conflict test, and prediction rests on this module, so it
is worth pinning hard.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from acp.common.geodesy import (
    EARTH_RADIUS_NM,
    bearing_difference_deg,
    destination_point,
    fpm_to_ft_per_second,
    from_local_enu,
    haversine_nm,
    initial_bearing_deg,
    knots_to_nm_per_second,
    normalize_bearing,
    project_forward,
    to_local_enu,
)

latitudes = st.floats(min_value=-90.0, max_value=90.0, allow_nan=False)
longitudes = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False)
bearings = st.floats(min_value=0.0, max_value=359.999, allow_nan=False)
# The tangent-plane projection is documented as a mid-latitude, short-range
# tool, so its properties are asserted over the range it claims to serve.
mid_latitudes = st.floats(min_value=-80.0, max_value=80.0, allow_nan=False)


@given(latitudes, longitudes, latitudes, longitudes)
def test_haversine_is_symmetric(lat1: float, lon1: float, lat2: float, lon2: float) -> None:
    forward = haversine_nm(lat1, lon1, lat2, lon2)
    backward = haversine_nm(lat2, lon2, lat1, lon1)
    assert forward == pytest.approx(backward, abs=1e-9)


@given(latitudes, longitudes)
def test_haversine_of_a_point_with_itself_is_zero(lat: float, lon: float) -> None:
    assert haversine_nm(lat, lon, lat, lon) == pytest.approx(0.0, abs=1e-9)


@given(latitudes, longitudes, latitudes, longitudes)
def test_haversine_never_exceeds_half_the_circumference(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> None:
    assert 0.0 <= haversine_nm(lat1, lon1, lat2, lon2) <= math.pi * EARTH_RADIUS_NM + 1e-6


@given(latitudes, longitudes, latitudes, longitudes, latitudes, longitudes)
@settings(max_examples=200)
def test_haversine_obeys_the_triangle_inequality(
    lat1: float, lon1: float, lat2: float, lon2: float, lat3: float, lon3: float
) -> None:
    direct = haversine_nm(lat1, lon1, lat3, lon3)
    via = haversine_nm(lat1, lon1, lat2, lon2) + haversine_nm(lat2, lon2, lat3, lon3)
    assert direct <= via + 1e-6


@given(mid_latitudes, longitudes, bearings, st.floats(min_value=0.1, max_value=500.0))
def test_destination_point_lands_at_the_requested_distance(
    lat: float, lon: float, bearing: float, distance_nm: float
) -> None:
    dest_lat, dest_lon = destination_point(lat, lon, bearing, distance_nm)
    assert haversine_nm(lat, lon, dest_lat, dest_lon) == pytest.approx(distance_nm, rel=1e-6)


@given(mid_latitudes, longitudes, bearings, st.floats(min_value=1.0, max_value=200.0))
def test_bearing_to_the_destination_is_the_bearing_travelled(
    lat: float, lon: float, bearing: float, distance_nm: float
) -> None:
    dest_lat, dest_lon = destination_point(lat, lon, bearing, distance_nm)
    recovered = initial_bearing_deg(lat, lon, dest_lat, dest_lon)
    assert abs(bearing_difference_deg(bearing, recovered)) < 1e-4


@given(st.floats(min_value=-1e6, max_value=1e6, allow_nan=False))
def test_normalize_bearing_lands_in_the_unit_circle(degrees: float) -> None:
    assert 0.0 <= normalize_bearing(degrees) < 360.0


@given(bearings, bearings)
def test_bearing_difference_is_the_shortest_turn(from_deg: float, to_deg: float) -> None:
    delta = bearing_difference_deg(from_deg, to_deg)
    assert -180.0 < delta <= 180.0
    assert normalize_bearing(from_deg + delta) == pytest.approx(to_deg, abs=1e-9)


@given(bearings, bearings)
def test_bearing_difference_is_antisymmetric(from_deg: float, to_deg: float) -> None:
    forward = bearing_difference_deg(from_deg, to_deg)
    backward = bearing_difference_deg(to_deg, from_deg)
    assume(abs(abs(forward) - 180.0) > 1e-9)  # the antipodal turn has no sign
    assert forward == pytest.approx(-backward, abs=1e-9)


@given(
    mid_latitudes,
    longitudes,
    st.floats(min_value=-60.0, max_value=60.0),
    st.floats(min_value=-60.0, max_value=60.0),
)
def test_local_projection_round_trips(
    ref_lat: float, ref_lon: float, east_nm: float, north_nm: float
) -> None:
    lat, lon = from_local_enu(ref_lat, ref_lon, east_nm, north_nm)
    back_east, back_north = to_local_enu(ref_lat, ref_lon, lat, lon)
    assert back_east == pytest.approx(east_nm, abs=1e-6)
    assert back_north == pytest.approx(north_nm, abs=1e-6)


@given(
    mid_latitudes,
    st.floats(min_value=-170.0, max_value=170.0),
    st.floats(min_value=-50.0, max_value=50.0),
    st.floats(min_value=-50.0, max_value=50.0),
)
def test_local_projection_agrees_with_great_circle_at_short_range(
    ref_lat: float, ref_lon: float, east_nm: float, north_nm: float
) -> None:
    """The flat-earth shortcut must stay within its documented 0.1% error."""
    lat, lon = from_local_enu(ref_lat, ref_lon, east_nm, north_nm)
    planar = math.hypot(east_nm, north_nm)
    spherical = haversine_nm(ref_lat, ref_lon, lat, lon)
    assert planar == pytest.approx(spherical, rel=1e-3, abs=1e-6)


@given(
    mid_latitudes,
    longitudes,
    bearings,
    st.floats(min_value=0.0, max_value=600.0),
    st.floats(min_value=0.0, max_value=600.0),
)
def test_dead_reckoning_covers_speed_times_time(
    lat: float, lon: float, track: float, speed_kt: float, seconds: float
) -> None:
    dest_lat, dest_lon = project_forward(lat, lon, track, speed_kt, seconds)
    expected_nm = knots_to_nm_per_second(speed_kt) * seconds
    assert haversine_nm(lat, lon, dest_lat, dest_lon) == pytest.approx(expected_nm, abs=1e-6)


@pytest.mark.parametrize(
    ("offset_nm", "max_error_nm"),
    [(50.0, 0.03), (100.0, 0.06), (200.0, 0.12), (300.0, 0.20)],
)
def test_separation_error_at_range_stays_within_the_documented_envelope(
    offset_nm: float, max_error_nm: float
) -> None:
    """Pins the table in the module docstring.

    This is the error that actually matters: two aircraft a few miles apart,
    both far from the projection origin. It grows much faster than the error in
    distance-from-origin, and the conflict detector's operating limit is derived
    from it. If someone widens the airspace without revisiting the projection,
    this fails.
    """
    ref_lat, ref_lon = 40.6, -75.5
    for bearing in range(0, 360, 15):
        a_lat, a_lon = destination_point(ref_lat, ref_lon, float(bearing), offset_nm)
        b_lat, b_lon = destination_point(a_lat, a_lon, float((bearing + 90) % 360), 4.0)

        a_east, a_north = to_local_enu(ref_lat, ref_lon, a_lat, a_lon)
        b_east, b_north = to_local_enu(ref_lat, ref_lon, b_lat, b_lon)

        planar = math.hypot(b_east - a_east, b_north - a_north)
        true_nm = haversine_nm(a_lat, a_lon, b_lat, b_lon)
        assert abs(planar - true_nm) < max_error_nm, (
            f"bearing {bearing} at {offset_nm} NM: {abs(planar - true_nm):.4f} NM error"
        )


def test_vertical_rate_conversion() -> None:
    """A 1800 fpm climb is 30 ft per second."""
    assert fpm_to_ft_per_second(1800.0) == pytest.approx(30.0)
    assert fpm_to_ft_per_second(-1200.0) == pytest.approx(-20.0)


def test_projection_degrades_gracefully_at_the_pole() -> None:
    """East/west has no meaning at 90 degrees; return the reference meridian.

    Nothing we simulate flies over the pole, but the branch exists so a bad
    input produces a defined answer instead of a division by zero deep inside
    the conflict detector.
    """
    lat, lon = from_local_enu(90.0, 10.0, 50.0, 0.0)
    assert lat == pytest.approx(90.0)
    assert lon == 10.0


def test_projection_handles_the_antimeridian() -> None:
    """Two aircraft either side of the date line are 12 NM apart, not 21 000."""
    east, north = to_local_enu(51.0, 179.95, 51.0, -179.95)
    assert math.hypot(east, north) == pytest.approx(
        haversine_nm(51.0, 179.95, 51.0, -179.95), rel=1e-3
    )
    assert east > 0.0  # eastbound across the line, not the long way round


def test_bearing_of_a_coincident_point_is_defined() -> None:
    """A stationary aircraft reports the same position twice; that is not an error."""
    assert initial_bearing_deg(40.0, -75.0, 40.0, -75.0) == 0.0


def test_known_distance_matches_published_value() -> None:
    """JFK to LAX is about 2144 NM great-circle; a sanity anchor for the constants."""
    jfk_lat, jfk_lon = 40.6398, -73.7789
    lax_lat, lax_lon = 33.9425, -118.4081
    assert haversine_nm(jfk_lat, jfk_lon, lax_lat, lax_lon) == pytest.approx(2144.0, rel=0.005)
