"""Turn surveillance reports into track state estimates.

**M1 scope, stated plainly: this does not filter.** It carries reported values
through, fills gaps from the last known value, and derives turn rate from
consecutive reported headings. The noise that goes in comes straight back out.

What it *does* implement is the part that is independent of the filter: track
lifecycle. A track initiates, gets confirmed once it has been seen enough times
to not be a one-off, coasts when reports stop arriving, and terminates when they
stop for long enough. That machinery is what M2's Kalman filter plugs into --
:meth:`TrackEstimator.on_report` keeps its signature and only the arithmetic
inside changes.

The uncertainty this reports is therefore a **placeholder constant**, not a
computed quantity. It is labelled as such everywhere it appears so no reader
mistakes it for a real covariance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from acp.common.contracts import DataSource, SurveillanceReport, TrackState, TrackUpdate
from acp.common.geodesy import bearing_difference_deg, haversine_nm, initial_bearing_deg

#: Reports needed before a track is trusted enough to be CONFIRMED. A single
#: report could be a spurious detection; three consistent ones is a track.
CONFIRMATION_UPDATES = 3

#: No report for this long and the track is coasting on its last estimate.
COAST_AFTER_S = 5.0

#: No report for this long and the aircraft is considered gone.
TERMINATE_AFTER_S = 30.0

#: Placeholder position uncertainty, in metres. M1 does not estimate covariance,
#: so this is the sensor's nominal sigma rather than anything derived from the
#: data. M2 replaces it with the square root of the filter's position covariance
#: trace, at which point it becomes meaningful and starts varying per track.
PLACEHOLDER_UNCERTAINTY_M = 30.0

#: Growth applied to the placeholder for each second of coasting, so a stale
#: track at least *looks* less trustworthy on the display.
COASTING_UNCERTAINTY_GROWTH_M_PER_S = 15.0


@dataclass(slots=True)
class TrackRecord:
    """Mutable per-aircraft state held by the estimator."""

    track_id: str
    icao24: str
    callsign: str | None
    first_seen_at: datetime
    last_report_at: datetime
    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    turn_rate_deg_s: float
    squawk: str | None
    scenario_id: str | None
    update_count: int = 0
    state: TrackState = TrackState.INITIATING
    terminated: bool = False
    _history: list[tuple[datetime, float, float]] = field(default_factory=list)


def track_id_for(icao24: str) -> str:
    """Track identifier for an aircraft address.

    One track per address, derived rather than generated. That makes database
    writes naturally idempotent under at-least-once redelivery: replaying a
    report produces the same track_id and the same primary key.

    **Simplification worth knowing.** A real system assigns a fresh track number
    when an aircraft reappears after a long absence, so that two separate flights
    by the same airframe do not share a track. Here they would. Acceptable
    because a scenario is a single continuous run; it would not be acceptable
    against a live feed.
    """
    return f"trk-{icao24}"


class TrackEstimator:
    """Maintains one track per aircraft address."""

    def __init__(
        self,
        *,
        coast_after_s: float = COAST_AFTER_S,
        terminate_after_s: float = TERMINATE_AFTER_S,
        confirmation_updates: int = CONFIRMATION_UPDATES,
    ) -> None:
        self._tracks: dict[str, TrackRecord] = {}
        self._coast_after_s = coast_after_s
        self._terminate_after_s = terminate_after_s
        self._confirmation_updates = confirmation_updates

    @property
    def live_track_count(self) -> int:
        return sum(1 for t in self._tracks.values() if not t.terminated)

    def on_report(self, report: SurveillanceReport) -> TrackUpdate:
        """Fold one report into its track and return the resulting estimate."""
        existing = self._tracks.get(report.icao24)
        record = (
            self._initiate(report)
            if existing is None or existing.terminated
            else self._update(existing, report)
        )
        self._tracks[report.icao24] = record
        return self._to_update(record, at=report.observed_at)

    def sweep(self, now: datetime) -> tuple[TrackUpdate, ...]:
        """Age every track against the clock and return those that changed state.

        Called on a timer, not on a report -- a track that stops updating
        produces no reports to trigger its own expiry, so something has to look
        at the clock instead. This is why the tracker has a periodic tick at all.
        """
        changed = []
        for record in self._tracks.values():
            if record.terminated:
                continue
            silence_s = (now - record.last_report_at).total_seconds()
            previous = record.state

            if silence_s >= self._terminate_after_s:
                record.state = TrackState.TERMINATED
                record.terminated = True
            elif silence_s >= self._coast_after_s:
                record.state = TrackState.COASTING
            if record.state != previous:
                changed.append(self._to_update(record, at=now))
        return tuple(changed)

    def _initiate(self, report: SurveillanceReport) -> TrackRecord:
        return TrackRecord(
            track_id=track_id_for(report.icao24),
            icao24=report.icao24,
            callsign=report.callsign,
            first_seen_at=report.observed_at,
            last_report_at=report.observed_at,
            lat=report.lat,
            lon=report.lon,
            altitude_ft=report.altitude_baro_ft or 0.0,
            ground_speed_kt=report.ground_speed_kt or 0.0,
            track_deg=report.track_deg or 0.0,
            vertical_rate_fpm=report.vertical_rate_fpm or 0.0,
            turn_rate_deg_s=0.0,
            squawk=report.squawk,
            scenario_id=report.scenario_id,
            update_count=1,
            state=TrackState.INITIATING,
        )

    def _update(self, record: TrackRecord, report: SurveillanceReport) -> TrackRecord:
        dt_s = (report.observed_at - record.last_report_at).total_seconds()

        # A report older than the one already folded in is out of order. With
        # per-aircraft partitioning this should not happen, so it is logged by
        # the caller's metrics rather than silently accepted -- accepting it
        # would drag the track backwards.
        if dt_s <= 0.0:
            return record

        track_deg = (
            report.track_deg if report.track_deg is not None else _derive_track(record, report)
        )
        speed_kt = (
            report.ground_speed_kt
            if report.ground_speed_kt is not None
            else _derive_speed_kt(record, report, dt_s)
        )

        # Signed shortest turn, so crossing north does not register as a 359
        # degree per second turn.
        record.turn_rate_deg_s = bearing_difference_deg(record.track_deg, track_deg) / dt_s
        record.lat = report.lat
        record.lon = report.lon
        record.altitude_ft = (
            report.altitude_baro_ft if report.altitude_baro_ft is not None else record.altitude_ft
        )
        record.ground_speed_kt = speed_kt
        record.track_deg = track_deg
        record.vertical_rate_fpm = (
            report.vertical_rate_fpm
            if report.vertical_rate_fpm is not None
            else record.vertical_rate_fpm
        )
        record.squawk = report.squawk or record.squawk
        record.callsign = report.callsign or record.callsign
        record.last_report_at = report.observed_at
        record.update_count += 1
        record.state = (
            TrackState.CONFIRMED
            if record.update_count >= self._confirmation_updates
            else TrackState.INITIATING
        )
        return record

    def _to_update(self, record: TrackRecord, *, at: datetime) -> TrackUpdate:
        staleness_s = max(0.0, (at - record.last_report_at).total_seconds())
        uncertainty = PLACEHOLDER_UNCERTAINTY_M + staleness_s * COASTING_UNCERTAINTY_GROWTH_M_PER_S
        return TrackUpdate(
            track_id=record.track_id,
            icao24=record.icao24,
            callsign=record.callsign,
            updated_at=at,
            last_report_at=record.last_report_at,
            state=record.state,
            lat=record.lat,
            lon=record.lon,
            altitude_ft=record.altitude_ft,
            ground_speed_kt=max(0.0, record.ground_speed_kt),
            track_deg=record.track_deg % 360.0,
            vertical_rate_fpm=record.vertical_rate_fpm,
            turn_rate_deg_s=record.turn_rate_deg_s,
            position_uncertainty_m=uncertainty,
            update_count=record.update_count,
            squawk=record.squawk,
            source=DataSource.SIMULATOR,
            scenario_id=record.scenario_id,
        )

    def prune_terminated(self, now: datetime, older_than: timedelta) -> int:
        """Drop terminated tracks from memory. Returns how many were removed."""
        cutoff = now - older_than
        stale = [
            icao24
            for icao24, record in self._tracks.items()
            if record.terminated and record.last_report_at < cutoff
        ]
        for icao24 in stale:
            del self._tracks[icao24]
        return len(stale)


def _derive_track(record: TrackRecord, report: SurveillanceReport) -> float:
    """Bearing from the previous position to this one.

    Only used when the report omits heading. Position-derived heading is noisy
    at 1 Hz -- 30 m of position error over roughly 230 m of travel is several
    degrees -- so the reported value is always preferred when present.
    """
    if (record.lat, record.lon) == (report.lat, report.lon):
        return record.track_deg
    return initial_bearing_deg(record.lat, record.lon, report.lat, report.lon)


def _derive_speed_kt(record: TrackRecord, report: SurveillanceReport, dt_s: float) -> float:
    """Ground speed from distance covered. Same noise caveat as `_derive_track`."""
    distance_nm = haversine_nm(record.lat, record.lon, report.lat, report.lon)
    return distance_nm / dt_s * 3600.0
