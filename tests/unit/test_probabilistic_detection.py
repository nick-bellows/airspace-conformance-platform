"""The probabilistic detector, and where it differs from the deterministic one.

`test_probability.py` checks the maths against scipy. This checks the wiring:
that the covariance reaches the decision, that a pair is judged on how well it
is known and not only on where its mean landed, and that the detector degrades
safely when a producer sends no uncertainty at all.

The deterministic detector remains the default, so every test here that does not
pass `probability_threshold` is also asserting that nothing changed for it.
"""

from __future__ import annotations

import pytest

from acp.common.contracts import DataSource, TrackState, TrackUpdate
from acp.common.geodesy import destination_point
from acp.services.conformance.separation import Conflict, SeparationMonitor
from tests.unit.test_separation import NOW


def a_track(
    track_id: str,
    *,
    lat: float,
    lon: float,
    altitude_ft: float = 35000.0,
    speed_kt: float = 450.0,
    track_deg: float = 90.0,
    position_uncertainty_m: float = 30.0,
    velocity_uncertainty_kt: float | None = 2.0,
    altitude_uncertainty_ft: float | None = 25.0,
    vertical_rate_uncertainty_fpm: float | None = 60.0,
) -> TrackUpdate:
    return TrackUpdate(
        track_id=track_id,
        icao24=f"{abs(hash(track_id)) % 0xFFFFFF:06x}",
        callsign=track_id.upper()[:8],
        updated_at=NOW,
        last_report_at=NOW,
        state=TrackState.CONFIRMED,
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        ground_speed_kt=speed_kt,
        track_deg=track_deg,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=position_uncertainty_m,
        velocity_uncertainty_kt=velocity_uncertainty_kt,
        altitude_uncertainty_ft=altitude_uncertainty_ft,
        vertical_rate_uncertainty_fpm=vertical_rate_uncertainty_fpm,
        update_count=50,
        source=DataSource.SIMULATOR,
    )


def converging_pair(
    *, separation_nm: float, offset_nm: float, **kwargs: object
) -> list[TrackUpdate]:
    """Two aircraft closing head-on but offset laterally by `offset_nm`.

    The offset is what sets the predicted miss distance: head-on with no offset
    passes through zero, and with a 6 NM offset they miss by 6 NM.
    """
    west_lat, west_lon = 40.0, -75.0
    east_lat, east_lon = destination_point(west_lat, west_lon, 90.0, separation_nm)
    shifted_lat, shifted_lon = destination_point(east_lat, east_lon, 0.0, offset_nm)
    return [
        a_track("trk-west", lat=west_lat, lon=west_lon, track_deg=90.0, **kwargs),  # type: ignore[arg-type]
        a_track("trk-east", lat=shifted_lat, lon=shifted_lon, track_deg=270.0, **kwargs),  # type: ignore[arg-type]
    ]


def only(conflicts: list[Conflict]) -> Conflict:
    assert len(conflicts) == 1, f"expected exactly one conflict, got {len(conflicts)}"
    return conflicts[0]


# --------------------------------------------------------------------------
# The default is unchanged
# --------------------------------------------------------------------------


def test_the_deterministic_detector_is_still_the_default() -> None:
    """Every published number before M7 was measured without a threshold."""
    monitor = SeparationMonitor()
    conflict = only(monitor.scan(converging_pair(separation_nm=60.0, offset_nm=0.0)))
    assert conflict.probability is None


def test_a_threshold_outside_zero_to_one_is_rejected() -> None:
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(ValueError, match="probability_threshold"):
            SeparationMonitor(probability_threshold=bad)


# --------------------------------------------------------------------------
# The behaviour that motivated the whole thing
# --------------------------------------------------------------------------


def test_a_certain_conflict_is_reported_with_high_probability() -> None:
    """Head-on, well tracked, passing through zero: no ambiguity to model."""
    monitor = SeparationMonitor(probability_threshold=0.05)
    conflict = only(monitor.scan(converging_pair(separation_nm=60.0, offset_nm=0.0)))
    assert conflict.probability is not None
    assert conflict.probability > 0.95


def test_a_marginal_miss_on_a_well_known_pair_is_still_rejected() -> None:
    """Tight covariance means the point estimate is trustworthy.

    Predicted to miss by 6 NM against a 5 NM standard, with both tracks known
    to a few hundred metres. The probabilistic detector should agree with the
    deterministic one here -- the value is in disagreeing only when the
    uncertainty actually warrants it.
    """
    pair = converging_pair(
        separation_nm=60.0,
        offset_nm=6.0,
        position_uncertainty_m=30.0,
        velocity_uncertainty_kt=0.5,
    )
    assert SeparationMonitor(probability_threshold=0.05).scan(pair) == []
    assert SeparationMonitor().scan(pair) == []


def test_a_marginal_miss_on_a_poorly_known_pair_is_reported() -> None:
    """The case the deterministic detector gets wrong by being confident.

    The same 6 NM predicted miss, but the velocity estimates are bad enough
    that by closest approach the pair could be anywhere within several miles.
    The deterministic detector says nothing; the probabilistic one says this is
    worth a look, which is the entire argument for it.
    """
    pair = converging_pair(
        separation_nm=60.0,
        offset_nm=6.0,
        position_uncertainty_m=3000.0,
        velocity_uncertainty_kt=45.0,
    )
    assert SeparationMonitor().scan(pair) == []
    conflict = only(SeparationMonitor(probability_threshold=0.05).scan(pair))
    assert conflict.probability is not None
    assert 0.05 <= conflict.probability < 1.0


def test_a_near_miss_on_a_poorly_known_pair_is_reported_with_low_confidence() -> None:
    """And the converse: the detector still fires, but says how sure it is.

    A 4 NM predicted miss breaches the standard, so both detectors report it.
    Only the probabilistic one can say the geometry is barely distinguishable
    from a safe pass, which is what an operator triaging a list needs.
    """
    pair = converging_pair(
        separation_nm=60.0,
        offset_nm=4.0,
        position_uncertainty_m=3000.0,
        velocity_uncertainty_kt=45.0,
    )
    assert len(SeparationMonitor().scan(pair)) == 1
    conflict = only(SeparationMonitor(probability_threshold=0.05).scan(pair))
    assert conflict.probability is not None
    assert conflict.probability < 0.9


def test_raising_the_threshold_only_removes_conflicts() -> None:
    """The threshold is a monotone knob, which is what makes a sweep meaningful."""
    pair = converging_pair(
        separation_nm=60.0,
        offset_nm=6.0,
        position_uncertainty_m=3000.0,
        velocity_uncertainty_kt=45.0,
    )
    counts = [len(SeparationMonitor(probability_threshold=t).scan(pair)) for t in (0.01, 0.2, 0.9)]
    assert counts == sorted(counts, reverse=True)


# --------------------------------------------------------------------------
# Vertical separation still has to hold
# --------------------------------------------------------------------------


def test_vertical_separation_still_prevents_a_conflict() -> None:
    """The guarantee the demo scenario protects, under the new detector too."""
    pair = converging_pair(separation_nm=60.0, offset_nm=0.0)
    pair[1] = a_track(
        "trk-east", lat=pair[1].lat, lon=pair[1].lon, track_deg=270.0, altitude_ft=39000.0
    )
    assert SeparationMonitor(probability_threshold=0.05).scan(pair) == []


def test_a_wide_altitude_uncertainty_does_not_get_silently_pre_filtered() -> None:
    """The cheap vertical rejection must not overrule the probability model.

    4000 ft apart is a firm reject when altitude is known to 25 ft. If a filter
    reported 1500 ft of altitude uncertainty the breach becomes possible, and a
    fast pre-filter that discarded the pair before the model saw it would be a
    silent false negative -- the worst kind here.
    """
    pair = converging_pair(separation_nm=60.0, offset_nm=0.0, altitude_uncertainty_ft=1500.0)
    pair[1] = a_track(
        "trk-east",
        lat=pair[1].lat,
        lon=pair[1].lon,
        track_deg=270.0,
        altitude_ft=39000.0,
        altitude_uncertainty_ft=1500.0,
    )
    conflict = only(SeparationMonitor(probability_threshold=0.001).scan(pair))
    assert conflict.probability is not None
    assert 0.0 < conflict.probability < 0.2


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


def test_a_track_without_uncertainty_falls_back_to_the_deterministic_test() -> None:
    """An older producer, or a message replayed from before the contract grew.

    Inventing a covariance would be worse than not having one, so the pair is
    judged exactly as it was before and the conflict reports no probability.
    """
    pair = converging_pair(
        separation_nm=60.0,
        offset_nm=0.0,
        velocity_uncertainty_kt=None,
        altitude_uncertainty_ft=None,
        vertical_rate_uncertainty_fpm=None,
    )
    conflict = only(SeparationMonitor(probability_threshold=0.05).scan(pair))
    assert conflict.probability is None


def test_one_track_missing_uncertainty_is_enough_to_fall_back() -> None:
    pair = converging_pair(separation_nm=60.0, offset_nm=0.0)
    pair[1] = a_track(
        "trk-east",
        lat=pair[1].lat,
        lon=pair[1].lon,
        track_deg=270.0,
        velocity_uncertainty_kt=None,
        altitude_uncertainty_ft=None,
        vertical_rate_uncertainty_fpm=None,
    )
    assert only(SeparationMonitor(probability_threshold=0.05).scan(pair)).probability is None
