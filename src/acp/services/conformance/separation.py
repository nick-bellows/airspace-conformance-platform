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
an aircraft placed specifically to catch it: it passes within 0.2 NM of the
conflicting pair at the moment they meet, 4000 ft above. Deleting both vertical
guards below makes the detector alert on it, which is what
`tests/unit/test_scenarios.py` asserts against.

## Operating limit

All tracks are projected into **one** tangent plane centred on the mean of the
current picture. That projection distorts the distance between two aircraft in
proportion to how far they are from the centre -- about 0.05 NM on a 4 NM gap at
100 NM out, 0.17 NM at 300 NM, and 0.61 NM at 800 NM (see `acp.common.geodesy`).

Against a 5 NM standard, anything past a few hundred miles is no longer a
rounding error, so this class **warns when the picture exceeds
`MAX_PICTURE_RADIUS_NM`**. The correct answer at that scale is to partition the
airspace and run a monitor per sector, which is what real ATC does and why
sectors exist at all. Warning rather than failing is deliberate: degraded
geometry is still far better than no conflict detection.

## Two detectors

By default a conflict is declared when the predicted minimum breaches both
standards -- a threshold on a point estimate, and the behaviour every published
number was measured on.

Passing `probability_threshold` switches to the probabilistic detector, which
asks how *likely* a breach is given the covariance the filter is already
maintaining, rather than which side of a line the mean landed on. The maths is
in `probability.py` and the reasoning is ADR 0012; the head-to-head measurement
is `eval/results/detector_comparison.md`. The two share all the geometry below
-- only the final accept/reject differs.

## What this deliberately does not do

Real conflict probes account for turn intent from the flight plan and for
climb/descent profiles rather than a constant vertical rate. This one assumes
straight-line, constant-rate flight for both aircraft over the whole lookahead.
That is why the lookahead is minutes rather than tens of minutes: the assumption
decays quickly. The probabilistic detector widens the *uncertainty* around that
assumption; it does not replace the assumption.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from acp.common.contracts import TrackUpdate
from acp.common.geodesy import knots_to_nm_per_second, to_local_enu
from acp.common.logging import get_logger
from acp.services.conformance.probability import (
    SIGMA_CUTOFF,
    conflict_probability,
    projected_sigma,
)

_log = get_logger(__name__)

# Bump when the detection geometry or the defaults change. A bump invalidates
# every committed conflict-detection report.
DETECTOR_VERSION = "acp-separation-v2"

#: En-route separation standards. Terminal airspace uses tighter ones, and
#: oceanic airspace much looser; a real system selects per airspace class.
DEFAULT_HORIZONTAL_NM = 5.0
DEFAULT_VERTICAL_FT = 1000.0
DEFAULT_LOOKAHEAD_S = 300.0

#: Tracks with fewer updates than this are not conflict-tested. A track two
#: reports old has a velocity estimate dominated by its initial guess, and
#: pairing two of those produces confident nonsense.
MIN_UPDATES_FOR_CONFLICT = 5

#: How far from the centre of the picture a track may sit before the shared
#: tangent-plane projection stops being accurate enough to trust against a 5 NM
#: standard. At 300 NM the distortion is about 0.17 NM on a 4 NM gap; beyond
#: that it grows quickly. Exceeding this logs a warning rather than failing.
MAX_PICTURE_RADIUS_NM = 300.0


def _quadratic_below(a: float, b: float, c: float) -> tuple[float, float] | None:
    """The open interval of `t` where `a t^2 + b t + c < 0`, or `None`.

    Used for the horizontal standard: squared separation is a quadratic in
    time, so the times at which the pair is closer than the standard are the
    roots of that quadratic minus the threshold.
    """
    if abs(a) < 1e-12:
        if abs(b) < 1e-12:
            # Constant separation for all time.
            return (-math.inf, math.inf) if c < 0.0 else None
        root = -c / b
        return (-math.inf, root) if b > 0.0 else (root, math.inf)
    discriminant = b * b - 4.0 * a * c
    if discriminant <= 0.0:
        # Never strictly inside the threshold: the parabola only touches, or
        # misses entirely.
        return None
    root = math.sqrt(discriminant)
    return ((-b - root) / (2.0 * a), (-b + root) / (2.0 * a))


def _linear_within(offset: float, rate: float, limit: float) -> tuple[float, float] | None:
    """The open interval of `t` where `|offset + rate t| < limit`, or `None`.

    Used for the vertical standard, where separation changes linearly.
    """
    if abs(rate) < 1e-12:
        return (-math.inf, math.inf) if abs(offset) < limit else None
    low = (-limit - offset) / rate
    high = (limit - offset) / rate
    return (low, high) if low <= high else (high, low)


def _overlap(
    first: tuple[float, float] | None, second: tuple[float, float] | None, horizon: float
) -> tuple[float, float] | None:
    """Intersect two open intervals with the lookahead window `[0, horizon]`."""
    if first is None or second is None:
        return None
    start = max(first[0], second[0], 0.0)
    end = min(first[1], second[1], horizon)
    return (start, end) if end > start else None


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
    #: P(both standards breached at closest approach), or `None` when the
    #: deterministic detector produced this conflict. Reported whether or not
    #: it was the deciding factor, so an operator can see how firm a warning is
    #: and an evaluation can sweep the threshold without re-running the scan.
    probability: float | None = None

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
        max_radius_nm: float = MAX_PICTURE_RADIUS_NM,
        probability_threshold: float | None = None,
    ) -> None:
        """`probability_threshold` switches on the probabilistic detector.

        Left as `None` the detector thresholds the point estimate, which is the
        behaviour every published number before M7 was measured on. Set to a
        probability in (0, 1], a pair is reported when the chance that *both*
        standards are breached at closest approach reaches it, given the
        covariance the filter is already maintaining.

        The probabilistic path degrades to the deterministic one per pair when
        either track arrives without uncertainty fields -- an older producer, or
        a replayed message from before the contract gained them.
        """
        if probability_threshold is not None and not 0.0 < probability_threshold <= 1.0:
            raise ValueError("probability_threshold must be in (0, 1]")
        self._horizontal_nm = horizontal_nm
        self._vertical_ft = vertical_ft
        self._lookahead_s = lookahead_s
        self._min_updates = min_updates
        self._max_radius_nm = max_radius_nm
        self._probability_threshold = probability_threshold

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
        self._check_picture_radius(states)

        conflicts = []
        for first, second in self._candidate_pairs(states):
            conflict = self._test_pair(first, second)
            if conflict is not None:
                conflicts.append(conflict)
        # Soonest first: an alert list is read from the top under pressure.
        conflicts.sort(key=lambda c: c.time_to_cpa_s)
        return conflicts

    def _check_picture_radius(self, states: list[_Kinematics]) -> float:
        """Warn if the picture is too wide for one tangent plane to be honest.

        Returns the radius so tests can assert on it. Logged once per scan at
        WARNING, which is noisy by design: it means every separation figure in
        that scan carries more error than the documented envelope, and a silent
        degradation of a safety-relevant number is the wrong trade.
        """
        radius_nm = max(
            (math.hypot(s.east_nm, s.north_nm) for s in states),
            default=0.0,
        )
        if radius_nm > self._max_radius_nm:
            _log.warning(
                "airspace picture exceeds the accurate projection envelope; "
                "separation distances are distorted. Partition into sectors.",
                extra={
                    "picture_radius_nm": round(radius_nm, 1),
                    "max_radius_nm": self._max_radius_nm,
                    "tracks": len(states),
                },
            )
        return radius_nm

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

    @staticmethod
    def _uncertainty(track: TrackUpdate) -> tuple[float, float, float, float] | None:
        """Per-axis sigmas as (position NM, velocity kt, altitude ft, rate fpm).

        `None` when the producer did not send them, which is what makes the
        probabilistic detector degrade to the deterministic one rather than
        inventing a covariance it does not have.
        """
        if (
            track.velocity_uncertainty_kt is None
            or track.altitude_uncertainty_ft is None
            or track.vertical_rate_uncertainty_fpm is None
        ):
            return None
        # `position_uncertainty_m` is the RSS over both horizontal axes; the
        # model wants a per-axis sigma, hence the sqrt(2).
        position_nm = track.position_uncertainty_m / 1852.0 / math.sqrt(2.0)
        return (
            position_nm,
            track.velocity_uncertainty_kt,
            track.altitude_uncertainty_ft,
            track.vertical_rate_uncertainty_fpm,
        )

    def _vertical_slack(self, first: _Kinematics, second: _Kinematics) -> float:
        """How far the cheap vertical rejection must be widened, in feet."""
        if self._probability_threshold is None:
            return 0.0
        a = self._uncertainty(first.track)
        b = self._uncertainty(second.track)
        if a is None or b is None:
            return 0.0
        return SIGMA_CUTOFF * projected_sigma(
            a[2], b[2], a[3] / 60.0, b[3] / 60.0, self._lookahead_s
        )

    def _probability(
        self,
        first: _Kinematics,
        second: _Kinematics,
        min_horizontal_nm: float,
        min_vertical_ft: float,
        t_cpa_s: float,
    ) -> float | None:
        """P(conflict), or `None` if this pair must be judged deterministically."""
        if self._probability_threshold is None:
            return None
        a = self._uncertainty(first.track)
        b = self._uncertainty(second.track)
        if a is None or b is None:
            return None

        horizontal_sigma = projected_sigma(
            a[0], b[0], knots_to_nm_per_second(a[1]), knots_to_nm_per_second(b[1]), t_cpa_s
        )
        vertical_sigma = projected_sigma(a[2], b[2], a[3] / 60.0, b[3] / 60.0, t_cpa_s)
        return conflict_probability(
            min_horizontal_nm=min_horizontal_nm,
            min_vertical_ft=min_vertical_ft,
            horizontal_sigma_nm=horizontal_sigma,
            vertical_sigma_ft=vertical_sigma,
            horizontal_standard_nm=self._horizontal_nm,
            vertical_standard_ft=self._vertical_ft,
        )

    def _test_pair(self, first: _Kinematics, second: _Kinematics) -> Conflict | None:
        # Vertical rejection first, and it is exact rather than a heuristic: if
        # the closest the two can possibly come vertically within the lookahead
        # still exceeds the standard, no horizontal geometry can make it a
        # conflict. Four floating-point operations against a full trigonometric
        # closest-approach solve.
        #
        # This matters more than the spatial grid does. Measured at 500 aircraft
        # in a 124 NM sector, the grid removed only 7.6% of pairs -- the
        # interaction radius is 105 NM, comparable to the whole airspace, so it
        # barely partitions anything. Traffic is separated by *flight level*
        # far more often than by distance, so that is where the cheap rejection
        # lives.
        vertical_now = abs(second.altitude_ft - first.altitude_ft)
        max_vertical_closure = abs(second.v_alt_fpm - first.v_alt_fpm) / 60.0 * self._lookahead_s
        # In probabilistic mode the rejection must be widened by the altitude
        # uncertainty, or it would discard pairs the probability model would
        # have scored above threshold -- a fast pre-filter silently overruling
        # the detector. Beyond the cutoff the probability is exactly zero, so
        # widening by that many sigma keeps the rejection exact.
        if vertical_now - max_vertical_closure - self._vertical_slack(first, second) >= (
            self._vertical_ft
        ):
            return None

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

        rel_alt = second.altitude_ft - first.altitude_ft
        rel_v_alt_ft_s = (second.v_alt_fpm - first.v_alt_fpm) / 60.0

        # A conflict is both standards breached *at the same moment*, and that
        # moment is not necessarily horizontal closest approach. Evaluating the
        # vertical standard only at horizontal CPA misses a pair that passes
        # inside 5 NM while still vertically clear and then descends through
        # 1000 ft before separating -- a real loss of separation, found by an
        # external review after every earlier test agreed on the wrong answer.
        # So: solve for the interval in which each standard is breached and ask
        # whether those intervals overlap.
        horizontal_window = _quadratic_below(
            closing_speed_sq,
            2.0 * (rel_east * rel_v_east + rel_north * rel_v_north),
            rel_east**2 + rel_north**2 - self._horizontal_nm**2,
        )
        vertical_window = _linear_within(rel_alt, rel_v_alt_ft_s, self._vertical_ft)
        window = _overlap(horizontal_window, vertical_window, self._lookahead_s)

        def geometry_at(t: float) -> tuple[float, float]:
            return (
                math.hypot(rel_east + rel_v_east * t, rel_north + rel_v_north * t),
                abs(rel_alt + rel_v_alt_ft_s * t),
            )

        if window is None:
            # No moment breaches both. Report the geometry at closest approach
            # so the probabilistic detector can still weigh a near miss.
            t_report = t_cpa
            min_horizontal, min_vertical = geometry_at(t_report)
        else:
            # Report the worst of each standard *while the conflict exists*.
            # Horizontal is a parabola, so its minimum is the clamped CPA;
            # vertical is linear, so its minimum is at one end of the window.
            start, end = window
            t_report = max(start, min(end, t_cpa))
            min_horizontal = geometry_at(t_report)[0]
            min_vertical = min(geometry_at(start)[1], geometry_at(end)[1])

        probability = self._probability(first, second, min_horizontal, min_vertical, t_report)
        if probability is None:
            if window is None:
                return None
        elif probability < (self._probability_threshold or 0.0):
            return None

        t_cpa = t_report

        return Conflict(
            probability=probability,
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
