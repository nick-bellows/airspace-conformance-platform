"""Predicted loss of separation between pairs of aircraft.

## The geometry

Both aircraft are projected forward at constant velocity in a shared local
tangent plane. For a relative position ``p`` and relative velocity ``v``, the
separation at time ``t`` is ``|p + v t|``, which is a parabola in ``t``. Its
minimum is at

    t_cpa = -(p . v) / (v . v)

the *time of closest point of approach*. Clamp that to the lookahead window --
negative means the pair is already diverging, beyond the window means it is not
our problem yet -- and evaluate the separation there.

A conflict is declared when the predicted minimum breaches **both** standards at
the same moment: less than 5 NM apart horizontally *and* less than 1000 ft
vertically. Either alone is normal and legal. This is the single most common
mistake in a naive implementation, and the `head-on-conflict` scenario contains
an aircraft placed specifically to catch it: laterally close to the conflicting
pair, but 4000 ft above.

## What this deliberately does not do

Real conflict probes account for turn intent from the flight plan, for
climb/descent profiles rather than a constant vertical rate, and for the
uncertainty in each track. This one assumes straight-line, constant-rate flight
for both aircraft over the whole lookahead. That is why the lookahead is minutes
rather than tens of minutes: the assumption decays quickly.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from acp.common.contracts import TrackUpdate
from acp.common.geodesy import knots_to_nm_per_second, to_local_enu

# Bump when the detection geometry or the defaults change. A bump invalidates
# every committed conflict-detection report.
DETECTOR_VERSION = "acp-separation-v1"

#: En-route separation standards. Terminal airspace uses tighter ones, and
#: oceanic airspace much looser; a real system selects per airspace class.
DEFAULT_HORIZONTAL_NM = 5.0
DEFAULT_VERTICAL_FT = 1000.0
DEFAULT_LOOKAHEAD_S = 300.0

#: Tracks with fewer updates than this are not conflict-tested. A track two
#: reports old has a velocity estimate dominated by its initial guess, and
#: pairing two of those produces confident nonsense.
MIN_UPDATES_FOR_CONFLICT = 5


@dataclass(frozen=True, slots=True)
class Conflict:
    """A predicted loss of separation between two tracks."""

    first_track_id: str
    second_track_id: str
    first_callsign: str | None
    second_callsign: str | None
    time_to_cpa_s: float
    min_horizontal_nm: float
    min_vertical_ft: float
    current_horizontal_nm: float
    current_vertical_ft: float
    lookahead_s: float
    horizontal_standard_nm: float
    vertical_standard_ft: float

    @property
    def key(self) -> str:
        """Stable identity for this pair, independent of argument order."""
        first, second = sorted((self.first_track_id, self.second_track_id))
        return f"predicted_conflict:{first}:{second}"

    @property
    def reason_codes(self) -> tuple[str, ...]:
        codes = ["horizontal_below_standard", "vertical_below_standard"]
        if self.time_to_cpa_s <= 0.0:
            codes.append("already_in_conflict")
        elif self.time_to_cpa_s < 60.0:
            codes.append("imminent")
        if self.current_horizontal_nm < self.horizontal_standard_nm:
            codes.append("currently_within_horizontal_standard")
        return tuple(codes)


@dataclass(frozen=True, slots=True)
class _Kinematics:
    """A track reduced to the numbers the geometry needs."""

    track: TrackUpdate
    east_nm: float
    north_nm: float
    v_east_kt: float
    v_north_kt: float
    altitude_ft: float
    v_alt_fpm: float


class SeparationMonitor:
    """Finds pairs of tracks predicted to lose separation."""

    def __init__(
        self,
        *,
        horizontal_nm: float = DEFAULT_HORIZONTAL_NM,
        vertical_ft: float = DEFAULT_VERTICAL_FT,
        lookahead_s: float = DEFAULT_LOOKAHEAD_S,
        min_updates: int = MIN_UPDATES_FOR_CONFLICT,
    ) -> None:
        self._horizontal_nm = horizontal_nm
        self._vertical_ft = vertical_ft
        self._lookahead_s = lookahead_s
        self._min_updates = min_updates

    def scan(self, tracks: list[TrackUpdate]) -> list[Conflict]:
        """Test every plausible pair and return the conflicts found."""
        eligible = [t for t in tracks if t.update_count >= self._min_updates]
        if len(eligible) < 2:
            return []

        # All tracks share one tangent-plane origin so their coordinates are
        # directly comparable. Using each track's own origin would mean two
        # aircraft either side of a degree boundary had incomparable positions.
        ref_lat = sum(t.lat for t in eligible) / len(eligible)
        ref_lon = sum(t.lon for t in eligible) / len(eligible)
        states = [self._kinematics(t, ref_lat, ref_lon) for t in eligible]

        conflicts = []
        for first, second in self._candidate_pairs(states):
            conflict = self._test_pair(first, second)
            if conflict is not None:
                conflicts.append(conflict)
        # Soonest first: an alert list is read from the top under pressure.
        conflicts.sort(key=lambda c: c.time_to_cpa_s)
        return conflicts

    @staticmethod
    def _kinematics(track: TrackUpdate, ref_lat: float, ref_lon: float) -> _Kinematics:
        east, north = to_local_enu(ref_lat, ref_lon, track.lat, track.lon)
        heading = math.radians(track.track_deg)
        return _Kinematics(
            track=track,
            east_nm=east,
            north_nm=north,
            v_east_kt=track.ground_speed_kt * math.sin(heading),
            v_north_kt=track.ground_speed_kt * math.cos(heading),
            altitude_ft=track.altitude_ft,
            v_alt_fpm=track.vertical_rate_fpm,
        )

    @property
    def _interaction_radius_nm(self) -> float:
        """How far apart two aircraft can be and still conflict in the window.

        Two aircraft closing head-on at 600 kt each cover 100 NM in 5 minutes.
        Anything further apart than that plus the horizontal standard cannot
        reach a conflict inside the lookahead, whatever they do.
        """
        max_closing_kt = 1200.0
        return knots_to_nm_per_second(max_closing_kt) * self._lookahead_s + self._horizontal_nm

    def _candidate_pairs(self, states: list[_Kinematics]) -> list[tuple[_Kinematics, _Kinematics]]:
        """Pairs close enough to be worth the full geometry test.

        A uniform grid, cell size equal to the interaction radius, so a pair can
        only conflict if they share a cell or sit in adjacent ones. At the scale
        this system runs today the plain O(n^2) loop would be perfectly fine;
        the grid is here because the M4 latency budget puts 500 aircraft through
        this path every second, where 125,000 pair tests per scan is not fine.
        """
        cell = self._interaction_radius_nm
        buckets: dict[tuple[int, int], list[_Kinematics]] = defaultdict(list)
        for state in states:
            buckets[(int(state.east_nm // cell), int(state.north_nm // cell))].append(state)

        pairs: list[tuple[_Kinematics, _Kinematics]] = []
        seen: set[tuple[str, str]] = set()
        for (cell_x, cell_y), occupants in buckets.items():
            neighbourhood = [
                other
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for other in buckets.get((cell_x + dx, cell_y + dy), ())
            ]
            for first in occupants:
                for second in neighbourhood:
                    if first.track.track_id == second.track.track_id:
                        continue
                    # Each unordered pair once, even though a pair straddling a
                    # cell boundary is reachable from both cells.
                    key = tuple(sorted((first.track.track_id, second.track.track_id)))
                    if key in seen:
                        continue
                    seen.add(key)  # type: ignore[arg-type]
                    pairs.append((first, second))
        return pairs

    def _test_pair(self, first: _Kinematics, second: _Kinematics) -> Conflict | None:
        rel_east = second.east_nm - first.east_nm
        rel_north = second.north_nm - first.north_nm
        rel_v_east = knots_to_nm_per_second(second.v_east_kt - first.v_east_kt)
        rel_v_north = knots_to_nm_per_second(second.v_north_kt - first.v_north_kt)

        current_horizontal = math.hypot(rel_east, rel_north)
        current_vertical = abs(second.altitude_ft - first.altitude_ft)

        closing_speed_sq = rel_v_east**2 + rel_v_north**2
        if closing_speed_sq < 1e-12:
            # Parallel and matched speed: separation never changes, so the
            # closest approach is now.
            t_cpa = 0.0
        else:
            t_cpa = -(rel_east * rel_v_east + rel_north * rel_v_north) / closing_speed_sq
            # Negative means the closest approach is in the past and the pair is
            # already diverging. Clamping to zero evaluates the geometry now,
            # which correctly still fires if they are currently too close.
            t_cpa = max(0.0, min(self._lookahead_s, t_cpa))

        min_horizontal = math.hypot(rel_east + rel_v_east * t_cpa, rel_north + rel_v_north * t_cpa)
        rel_v_alt_ft_s = (second.v_alt_fpm - first.v_alt_fpm) / 60.0
        min_vertical = abs((second.altitude_ft - first.altitude_ft) + rel_v_alt_ft_s * t_cpa)

        # Both standards, at the same moment. Either alone is routine.
        if min_horizontal >= self._horizontal_nm or min_vertical >= self._vertical_ft:
            return None

        return Conflict(
            first_track_id=first.track.track_id,
            second_track_id=second.track.track_id,
            first_callsign=first.track.callsign,
            second_callsign=second.track.callsign,
            time_to_cpa_s=t_cpa,
            min_horizontal_nm=min_horizontal,
            min_vertical_ft=min_vertical,
            current_horizontal_nm=current_horizontal,
            current_vertical_ft=current_vertical,
            lookahead_s=self._lookahead_s,
            horizontal_standard_nm=self._horizontal_nm,
            vertical_standard_ft=self._vertical_ft,
        )
