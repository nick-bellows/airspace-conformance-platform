"""Tests for conflict geometry.

The case that matters most is the **negative** one: two aircraft laterally close
but vertically separated. Both standards must be breached at the same moment for
a conflict to exist, and a detector that forgets the altitude check fires
constantly on perfectly legal traffic. The committed `head-on-conflict` scenario
contains an aircraft placed specifically to catch that mistake.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from acp.common.contracts import DataSource, TrackState, TrackUpdate
from acp.common.geodesy import destination_point
from acp.services.conformance.separation import SeparationMonitor

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_track(
    track_id: str,
    *,
    lat: float,
    lon: float,
    altitude_ft: float = 35000.0,
    speed_kt: float = 450.0,
    track_deg: float = 90.0,
    vertical_rate_fpm: float = 0.0,
    update_count: int = 50,
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
        vertical_rate_fpm=vertical_rate_fpm,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=update_count,
        source=DataSource.SIMULATOR,
    )


def head_on_pair(*, separation_nm: float, altitude_gap_ft: float = 0.0) -> list[TrackUpdate]:
    """Two aircraft closing head-on, `separation_nm` apart right now."""
    west_lat, west_lon = 40.0, -75.0
    east_lat, east_lon = destination_point(west_lat, west_lon, 90.0, separation_nm)
    return [
        a_track("trk-west", lat=west_lat, lon=west_lon, track_deg=90.0),
        a_track(
            "trk-east",
            lat=east_lat,
            lon=east_lon,
            track_deg=270.0,
            altitude_ft=35000.0 + altitude_gap_ft,
        ),
    ]


# --------------------------------------------------------------------------
# The core rule: both standards, at the same moment
# --------------------------------------------------------------------------


def test_a_head_on_pair_is_a_conflict() -> None:
    conflicts = SeparationMonitor().scan(head_on_pair(separation_nm=60.0))
    assert len(conflicts) == 1
    assert conflicts[0].min_horizontal_nm < 5.0
    assert conflicts[0].time_to_cpa_s > 0.0


def test_vertical_separation_prevents_a_conflict() -> None:
    """The mistake this whole module exists to avoid.

    Same lateral geometry as the test above, but 4000 ft apart. Legal, routine,
    and must not alert.
    """
    assert SeparationMonitor().scan(head_on_pair(separation_nm=60.0, altitude_gap_ft=4000.0)) == []


def test_a_pair_at_exactly_the_vertical_standard_does_not_alert() -> None:
    """1000 ft *is* separation. The comparison is strict on purpose."""
    assert SeparationMonitor().scan(head_on_pair(separation_nm=60.0, altitude_gap_ft=1000.0)) == []


def test_a_pair_just_inside_the_vertical_standard_does_alert() -> None:
    assert SeparationMonitor().scan(head_on_pair(separation_nm=60.0, altitude_gap_ft=900.0))


def test_diverging_aircraft_are_not_a_conflict() -> None:
    """Closest approach is in the past, so the geometry is already resolved."""
    west_lat, west_lon = 40.0, -75.0
    east_lat, east_lon = destination_point(west_lat, west_lon, 90.0, 30.0)
    tracks = [
        a_track("trk-west", lat=west_lat, lon=west_lon, track_deg=270.0),
        a_track("trk-east", lat=east_lat, lon=east_lon, track_deg=90.0),
    ]
    assert SeparationMonitor().scan(tracks) == []


def test_parallel_aircraft_holding_separation_do_not_alert() -> None:
    lat, lon = 40.0, -75.0
    other_lat, other_lon = destination_point(lat, lon, 0.0, 10.0)
    tracks = [
        a_track("trk-a", lat=lat, lon=lon, track_deg=90.0),
        a_track("trk-b", lat=other_lat, lon=other_lon, track_deg=90.0),
    ]
    assert SeparationMonitor().scan(tracks) == []


def test_parallel_aircraft_already_too_close_do_alert() -> None:
    """Never converging, but never separated either. Closest approach is now."""
    lat, lon = 40.0, -75.0
    other_lat, other_lon = destination_point(lat, lon, 0.0, 2.0)
    tracks = [
        a_track("trk-a", lat=lat, lon=lon, track_deg=90.0),
        a_track("trk-b", lat=other_lat, lon=other_lon, track_deg=90.0),
    ]
    conflicts = SeparationMonitor().scan(tracks)
    assert len(conflicts) == 1
    assert conflicts[0].time_to_cpa_s == 0.0


def test_a_climbing_aircraft_through_another_level_is_a_conflict() -> None:
    """Vertically clear now, not clear at the closest point of approach."""
    west_lat, west_lon = 40.0, -75.0
    east_lat, east_lon = destination_point(west_lat, west_lon, 90.0, 30.0)
    tracks = [
        a_track("trk-level", lat=west_lat, lon=west_lon, track_deg=90.0, altitude_ft=35000.0),
        a_track(
            "trk-climb",
            lat=east_lat,
            lon=east_lon,
            track_deg=270.0,
            altitude_ft=31000.0,
            vertical_rate_fpm=2000.0,
        ),
    ]
    conflicts = SeparationMonitor().scan(tracks)
    assert len(conflicts) == 1
    assert conflicts[0].current_vertical_ft == pytest.approx(4000.0)
    assert conflicts[0].min_vertical_ft < 1000.0


# --------------------------------------------------------------------------
# Lookahead
# --------------------------------------------------------------------------


def test_a_conflict_beyond_the_lookahead_is_not_reported_yet() -> None:
    """900 NM apart closing at 900 kt is an hour away. Not our problem now."""
    assert SeparationMonitor(lookahead_s=300.0).scan(head_on_pair(separation_nm=900.0)) == []


def test_a_longer_lookahead_finds_a_more_distant_conflict() -> None:
    far = head_on_pair(separation_nm=140.0)
    assert SeparationMonitor(lookahead_s=300.0).scan(far) == []
    assert SeparationMonitor(lookahead_s=900.0).scan(far)


# --------------------------------------------------------------------------
# Eligibility and hygiene
# --------------------------------------------------------------------------


def test_a_new_track_is_not_conflict_tested() -> None:
    """Two reports in, the velocity estimate is mostly the initial guess.

    Pairing two of those produces confident nonsense, so young tracks are
    excluded until they have enough history to be believed.
    """
    tracks = head_on_pair(separation_nm=60.0)
    young = [t.model_copy(update={"update_count": 2}) for t in tracks]
    assert SeparationMonitor().scan(young) == []


def test_a_single_aircraft_cannot_conflict_with_itself() -> None:
    assert SeparationMonitor().scan([a_track("trk-a", lat=40.0, lon=-75.0)]) == []
    assert SeparationMonitor().scan([]) == []


def test_each_pair_is_reported_once() -> None:
    """The grid makes a pair reachable from both cells; it must not double-count."""
    conflicts = SeparationMonitor().scan(head_on_pair(separation_nm=20.0))
    assert len(conflicts) == 1


def test_the_pair_key_does_not_depend_on_argument_order() -> None:
    """Otherwise the alert lifecycle would treat A-B and B-A as two conflicts."""
    conflicts = SeparationMonitor().scan(head_on_pair(separation_nm=60.0))
    reversed_scan = SeparationMonitor().scan(list(reversed(head_on_pair(separation_nm=60.0))))
    assert conflicts[0].key == reversed_scan[0].key


def test_conflicts_are_ordered_by_urgency() -> None:
    """An alert list is read from the top when things are busy."""
    # 60 NM apart closing at 900 kt: about 4 minutes out. The second pair is
    # roughly 14 NM apart and closing just as fast, so under a minute out.
    tracks = [
        *head_on_pair(separation_nm=60.0),
        a_track("trk-c", lat=41.0, lon=-75.0, track_deg=90.0),
        a_track("trk-d", lat=41.0, lon=-74.7, track_deg=270.0),
    ]
    conflicts = SeparationMonitor().scan(tracks)
    assert len(conflicts) == 2
    assert conflicts == sorted(conflicts, key=lambda c: c.time_to_cpa_s)
    assert conflicts[0].time_to_cpa_s < conflicts[1].time_to_cpa_s


def test_an_imminent_conflict_is_flagged_as_such() -> None:
    conflicts = SeparationMonitor().scan(head_on_pair(separation_nm=5.5))
    assert (
        "imminent" in conflicts[0].reason_codes
        or "already_in_conflict" in conflicts[0].reason_codes
    )


def test_reason_codes_explain_the_alert() -> None:
    conflict = SeparationMonitor().scan(head_on_pair(separation_nm=60.0))[0]
    assert "horizontal_below_standard" in conflict.reason_codes
    assert "vertical_below_standard" in conflict.reason_codes


# --------------------------------------------------------------------------
# The grid is an optimisation, not a behaviour change
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The projection has an operating limit, and it is observable
# --------------------------------------------------------------------------


def test_a_wide_picture_warns_that_geometry_is_degraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Beyond a few hundred miles the single tangent plane distorts separation
    by a meaningful fraction of the 5 NM standard.

    Warning rather than failing is deliberate: degraded conflict detection is
    still much better than none. The correct fix at that scale is to partition
    the airspace into sectors, which is what the message says.
    """
    far_lat, far_lon = destination_point(40.0, -75.0, 90.0, 700.0)
    tracks = [
        a_track("trk-here", lat=40.0, lon=-75.0),
        a_track("trk-far", lat=far_lat, lon=far_lon),
    ]
    with caplog.at_level("WARNING"):
        SeparationMonitor().scan(tracks)
    assert any("projection envelope" in r.message for r in caplog.records)


def test_a_normal_sized_picture_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """The warning has to stay rare or it becomes noise people filter out."""
    with caplog.at_level("WARNING"):
        SeparationMonitor().scan(head_on_pair(separation_nm=60.0))
    assert not [r for r in caplog.records if "projection envelope" in r.message]


def test_the_grid_finds_the_same_conflicts_as_an_exhaustive_search() -> None:
    """A spatial index that changes the answer is a bug, not an optimisation.

    Aircraft are spread across many grid cells, including pairs straddling a
    cell boundary, which is exactly where a naive grid drops a pair.
    """
    monitor = SeparationMonitor()
    tracks = []
    for i in range(12):
        lat, lon = destination_point(40.0, -75.0, i * 30.0, 40.0 + i * 8.0)
        tracks.append(a_track(f"trk-{i}", lat=lat, lon=lon, track_deg=(i * 30.0 + 180.0) % 360.0))

    found = {c.key for c in monitor.scan(tracks)}

    exhaustive = set()
    for i, first in enumerate(tracks):
        for second in tracks[i + 1 :]:
            exhaustive.update(c.key for c in monitor.scan([first, second]))

    assert found == exhaustive


# --------------------------------------------------------------------------
# Both standards must be breached *at the same moment* -- and "the same moment"
# is not necessarily horizontal closest approach.
# --------------------------------------------------------------------------


def descending_pair(
    *, separation_nm: float, altitude_gap_ft: float, descent_fpm: float
) -> list[TrackUpdate]:
    """Head-on, with the higher aircraft descending toward the lower one."""
    west_lat, west_lon = 40.0, -75.0
    east_lat, east_lon = destination_point(west_lat, west_lon, 90.0, separation_nm)
    return [
        a_track("trk-west", lat=west_lat, lon=west_lon, track_deg=90.0),
        a_track(
            "trk-east",
            lat=east_lat,
            lon=east_lon,
            track_deg=270.0,
            altitude_ft=35000.0 + altitude_gap_ft,
            vertical_rate_fpm=-descent_fpm,
        ),
    ]


def test_a_conflict_after_horizontal_closest_approach_is_still_a_conflict() -> None:
    """The defect an external review found, which every earlier test missed.

    Twelve miles apart, head-on at 450 kt each, 2000 ft apart with the higher
    aircraft descending at 1000 fpm. Horizontal closest approach is at t=48s,
    where the pair is still 1200 ft apart vertically -- so a detector that
    evaluates the vertical standard *only at horizontal CPA* sees nothing.

    But the pair stays inside 5 NM from roughly t=39s to t=67s, and drops below
    1000 ft at t=60s. Between t=60s and t=67s both standards are breached at
    the same moment, which is the definition of a conflict.

    Requiring the horizontal-breach interval and the vertical-breach interval
    to *overlap* is the correct test; evaluating a single instant is not.
    """
    conflicts = SeparationMonitor().scan(
        descending_pair(separation_nm=12.0, altitude_gap_ft=2000.0, descent_fpm=1000.0)
    )
    assert len(conflicts) == 1, "a real simultaneous loss of separation was missed"
    conflict = conflicts[0]
    assert conflict.min_horizontal_nm < 5.0
    assert conflict.min_vertical_ft < 1000.0


def test_the_reported_moment_is_inside_the_overlap() -> None:
    """An alert has to name a time at which the conflict actually exists."""
    conflicts = SeparationMonitor().scan(
        descending_pair(separation_nm=12.0, altitude_gap_ft=2000.0, descent_fpm=1000.0)
    )
    assert conflicts
    # 5 NM at 900 kt closure is reached around t=39s; 1000 ft at 1000 fpm at
    # t=60s. The overlap opens at 60s and the pair leaves 5 NM around t=67s.
    assert 58.0 <= conflicts[0].time_to_cpa_s <= 69.0


def test_a_descent_that_arrives_too_late_is_not_a_conflict() -> None:
    """The converse, so the fix cannot pass by simply alerting more.

    Same geometry, but descending slowly enough that the vertical standard is
    still intact by the time the pair has passed and separated horizontally.
    The intervals never overlap, so there is no conflict.
    """
    assert (
        SeparationMonitor().scan(
            descending_pair(separation_nm=12.0, altitude_gap_ft=2000.0, descent_fpm=120.0)
        )
        == []
    )
