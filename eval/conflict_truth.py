"""Ground truth for conflict detection, computed from the simulator.

This module is what makes the headline metric meaningful. It reads the exact
simulator state -- which the pipeline never sees -- and works out when two
aircraft *actually* lost separation. The detector is scored against that, having
consumed only the noisy observation stream.

## What counts as a conflict event

A **violation** is an instant at which two aircraft are simultaneously closer
than the horizontal standard and closer than the vertical standard. Consecutive
violating instants for the same pair are merged into one **event**, because a
single encounter that dips below the standard for ninety seconds is one thing
that happened, not ninety.

A short gap between violations is bridged rather than splitting the event in
two: an encounter that momentarily pops back above 5 NM and immediately drops
below again is one continuous problem, and counting it twice would inflate both
the event count and the apparent miss rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from acp.common.contracts import TruthState
from acp.common.geodesy import haversine_nm

#: Violations separated by less than this are treated as one event.
EVENT_BRIDGE_S = 30.0


@dataclass(frozen=True, slots=True)
class ConflictEvent:
    """A real loss of separation, as it actually happened."""

    first_icao24: str
    second_icao24: str
    started_at: datetime
    ended_at: datetime
    min_horizontal_nm: float
    min_vertical_ft: float

    @property
    def pair(self) -> tuple[str, str]:
        return tuple(sorted((self.first_icao24, self.second_icao24)))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class _Violation:
    pair: tuple[str, str]
    at: datetime
    horizontal_nm: float
    vertical_ft: float


class TruthConflictFinder:
    """Accumulates truth samples and extracts the conflict events in them."""

    def __init__(self, *, horizontal_nm: float = 5.0, vertical_ft: float = 1000.0) -> None:
        self._horizontal_nm = horizontal_nm
        self._vertical_ft = vertical_ft
        self._violations: list[_Violation] = []

    def observe(self, states: tuple[TruthState, ...], at: datetime) -> None:
        """Record any pairs violating separation at this instant."""
        ordered = sorted(states, key=lambda s: s.icao24)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1 :]:
                horizontal = haversine_nm(first.lat, first.lon, second.lat, second.lon)
                if horizontal >= self._horizontal_nm:
                    continue
                vertical = abs(first.altitude_ft - second.altitude_ft)
                if vertical >= self._vertical_ft:
                    continue
                self._violations.append(
                    _Violation(
                        pair=(first.icao24, second.icao24),
                        at=at,
                        horizontal_nm=horizontal,
                        vertical_ft=vertical,
                    )
                )

    def events(self) -> list[ConflictEvent]:
        """Merge instantaneous violations into discrete events, per pair."""
        by_pair: dict[tuple[str, str], list[_Violation]] = {}
        for violation in self._violations:
            by_pair.setdefault(violation.pair, []).append(violation)

        events: list[ConflictEvent] = []
        for pair, violations in by_pair.items():
            violations.sort(key=lambda v: v.at)
            run: list[_Violation] = [violations[0]]
            for violation in violations[1:]:
                if (violation.at - run[-1].at).total_seconds() <= EVENT_BRIDGE_S:
                    run.append(violation)
                else:
                    events.append(_event_from(pair, run))
                    run = [violation]
            events.append(_event_from(pair, run))

        events.sort(key=lambda e: e.started_at)
        return events


def _event_from(pair: tuple[str, str], run: list[_Violation]) -> ConflictEvent:
    return ConflictEvent(
        first_icao24=pair[0],
        second_icao24=pair[1],
        started_at=run[0].at,
        ended_at=run[-1].at,
        min_horizontal_nm=min(v.horizontal_nm for v in run),
        min_vertical_ft=min(v.vertical_ft for v in run),
    )
