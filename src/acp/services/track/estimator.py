"""Turn surveillance reports into track state estimates.

Two responsibilities, deliberately separable:

**Filtering** is delegated to :class:`~acp.services.track.kalman.TrackFilter`,
one per track. Reported position and velocity are noisy; the filter produces a
smoothed estimate and, more usefully, a *covariance* -- so the uncertainty this
publishes is now computed rather than assumed, and it grows on its own while a
track coasts through a dropout.

**Lifecycle** lives here. A track initiates, gets confirmed once it has been
seen enough times not to be a one-off, coasts when reports stop arriving, and
terminates when they stop for long enough. None of that depends on how the
filtering is done, which is why it survived the M1-to-M2 change untouched.

The filter also yields the *innovation* -- how far the aircraft was from where
constant-velocity physics said it would be. That number is passed through to the
conformance service, where it becomes the manoeuvre signal.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from acp.common.contracts import DataSource, SurveillanceReport, TrackState, TrackUpdate
from acp.common.geodesy import bearing_difference_deg
from acp.services.track.kalman import FilterTuning, TrackFilter, reference_for

#: Reports needed before a track is trusted enough to be CONFIRMED. A single
#: report could be a spurious detection; three consistent ones is a track.
CONFIRMATION_UPDATES = 3

#: No report for this long and the track is coasting on its last estimate.
COAST_AFTER_S = 5.0

#: No report for this long and the aircraft is considered gone.
TERMINATE_AFTER_S = 30.0


@dataclass(slots=True)
class TrackRecord:
    """Mutable per-aircraft state held by the estimator."""

    track_id: str
    icao24: str
    callsign: str | None
    first_seen_at: datetime
    last_report_at: datetime
    filter: TrackFilter
    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    turn_rate_deg_s: float
    position_uncertainty_m: float
    innovation_nm: float
    squawk: str | None
    scenario_id: str | None
    update_count: int = 0
    state: TrackState = TrackState.INITIATING
    terminated: bool = False


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
        tuning: FilterTuning | None = None,
    ) -> None:
        self._tracks: dict[str, TrackRecord] = {}
        self._coast_after_s = coast_after_s
        self._terminate_after_s = terminate_after_s
        self._confirmation_updates = confirmation_updates
        self._tuning = tuning or FilterTuning()

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
        ref_lat, ref_lon = reference_for(report.lat, report.lon)
        track_filter = TrackFilter(
            ref_lat=ref_lat,
            ref_lon=ref_lon,
            lat=report.lat,
            lon=report.lon,
            altitude_ft=report.altitude_baro_ft or 0.0,
            ground_speed_kt=report.ground_speed_kt or 0.0,
            track_deg=report.track_deg or 0.0,
            vertical_rate_fpm=report.vertical_rate_fpm or 0.0,
            tuning=self._tuning,
        )
        estimate = track_filter.estimate()
        return TrackRecord(
            track_id=track_id_for(report.icao24),
            icao24=report.icao24,
            callsign=report.callsign,
            first_seen_at=report.observed_at,
            last_report_at=report.observed_at,
            filter=track_filter,
            lat=estimate.lat,
            lon=estimate.lon,
            altitude_ft=estimate.altitude_ft,
            ground_speed_kt=estimate.ground_speed_kt,
            track_deg=estimate.track_deg,
            vertical_rate_fpm=estimate.vertical_rate_fpm,
            turn_rate_deg_s=0.0,
            position_uncertainty_m=estimate.position_uncertainty_m,
            innovation_nm=0.0,
            squawk=report.squawk,
            scenario_id=report.scenario_id,
            update_count=1,
            state=TrackState.INITIATING,
        )

    def _update(self, record: TrackRecord, report: SurveillanceReport) -> TrackRecord:
        dt_s = (report.observed_at - record.last_report_at).total_seconds()

        # A report older than the one already folded in is out of order. With
        # per-aircraft partitioning this should not happen, and accepting it
        # would drag the track backwards, so it is dropped.
        if dt_s <= 0.0:
            return record

        previous_track_deg = record.track_deg
        record.filter.update(
            dt_s=dt_s,
            lat=report.lat,
            lon=report.lon,
            altitude_ft=report.altitude_baro_ft,
            ground_speed_kt=report.ground_speed_kt,
            track_deg=report.track_deg,
            vertical_rate_fpm=report.vertical_rate_fpm,
        )
        estimate = record.filter.estimate()

        # Turn rate comes from the *filtered* headings, not the reported ones.
        # Reported heading carries half a degree of noise, which at 1 Hz reads
        # as a half-degree-per-second turn on a perfectly straight aircraft.
        record.turn_rate_deg_s = (
            bearing_difference_deg(previous_track_deg, estimate.track_deg) / dt_s
        )
        record.lat = estimate.lat
        record.lon = estimate.lon
        record.altitude_ft = estimate.altitude_ft
        record.ground_speed_kt = estimate.ground_speed_kt
        record.track_deg = estimate.track_deg
        record.vertical_rate_fpm = estimate.vertical_rate_fpm
        record.position_uncertainty_m = estimate.position_uncertainty_m
        record.innovation_nm = estimate.innovation_nm
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
        if staleness_s > 0.0:
            # Coasting: run the filter forward on prediction alone. The
            # covariance grows because process noise accumulates with no
            # measurement to check it, so the reported uncertainty rises by
            # itself -- no ad-hoc growth constant needed.
            record.filter.predict(staleness_s)
            estimate = record.filter.estimate()
            record.lat = estimate.lat
            record.lon = estimate.lon
            record.altitude_ft = estimate.altitude_ft
            record.position_uncertainty_m = estimate.position_uncertainty_m

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
            position_uncertainty_m=record.position_uncertainty_m,
            update_count=record.update_count,
            innovation_nm=record.innovation_nm,
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

    @property
    def known_addresses(self) -> frozenset[str]:
        """Every aircraft this estimator still holds, terminated or not.

        Exposed so the runner can drop its partition-map entries for aircraft
        that have been pruned, rather than keeping one per address ever seen.
        """
        return frozenset(self._tracks)

    def release(self, addresses: Iterable[str]) -> int:
        """Forget these aircraft without terminating them. Returns how many.

        Distinct from `prune_terminated`, and the distinction is the whole
        point: this is for aircraft that are still flying but are no longer
        *ours*. When Kafka moves a partition to another consumer, the aircraft
        on it keep reporting -- to somebody else. Ageing them here would end
        with this instance declaring a live aircraft terminated and deleting it
        from the shared picture while its new owner is still updating it.

        So no `TrackUpdate` is produced and nothing is published. The state is
        simply dropped; the new owner rebuilds a filter from the next report.
        That costs a few seconds of convergence for the affected aircraft, which
        is a far smaller error than a track vanishing from a controller's
        display. See ADR 0011.
        """
        released = 0
        for icao24 in list(addresses):
            if self._tracks.pop(icao24, None) is not None:
                released += 1
        return released

    def innovation_for(self, icao24: str) -> float:
        """Latest innovation for a track, in nautical miles.

        Exposed for tests and for the tracking-accuracy evaluation. The value
        also travels downstream inside the track update, where the conformance
        monitor uses it as the manoeuvre signal.
        """
        record = self._tracks.get(icao24)
        return record.innovation_nm if record else 0.0
