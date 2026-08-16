"""Conformance service: track updates in, advisory alerts out.

Unlike the tracker, this service does not act on each message as it arrives. It
maintains a picture of the airspace from the incoming updates and **scans that
picture on a timer**. The reason is that conflicts are a property of the *set*
of aircraft, not of any one of them: re-running the pairwise geometry on every
individual update would repeat almost identical work hundreds of times a second
and still not answer a different question.

The scan interval is therefore the real latency knob. It trades detection
promptness against CPU, and the M4 latency budget measures exactly this.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from acp.common.contracts import (
    TOPIC_ALERTS,
    AlertKind,
    ConflictEvidence,
    Severity,
    TrackState,
    TrackUpdate,
)
from acp.common.logging import get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.common.metrics import METRICS
from acp.common.tracing import current_span_context, span
from acp.services.conformance.alerts import AlertManager, Detection
from acp.services.conformance.monitor import ConformanceMonitor, NonConformance
from acp.services.conformance.rules import check as check_rules
from acp.services.conformance.separation import Conflict, SeparationMonitor
from acp.storage.stores import LiveAlertStore

_log = get_logger(__name__)

#: How often the airspace picture is scanned for conflicts.
DEFAULT_SCAN_INTERVAL_S = 1.0

#: A track not updated for this long is dropped from the picture. Shorter than
#: the tracker's termination timeout, because reasoning about a conflict with an
#: aircraft whose position is half a minute stale is worse than not reasoning.
STALE_TRACK_AFTER_S = 15.0

#: How far ahead of the wall clock a message timestamp may be before it is
#: treated as a bad clock rather than as replayed data. Generous, because a
#: legitimate replay run races hours ahead; bounded, because the scan clock
#: ratchets and a single absurd timestamp would otherwise poison it forever.
MAX_DATA_CLOCK_LEAD = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ConformanceStats:
    """End-of-run totals for the shutdown log line.

    Distinct from the Prometheus counters in `acp.common.metrics`, which are
    live and scrapeable; these summarise one run and are what the tests assert
    on.
    """

    updates_consumed: int
    scans: int
    alerts_published: int


class ConformanceRunner:
    """Maintains the airspace picture and scans it for conflicts."""

    def __init__(
        self,
        subscriber: MessageSubscriber[TrackUpdate] | None,
        publisher: MessagePublisher,
        *,
        alert_store: LiveAlertStore | None = None,
        monitor: SeparationMonitor | None = None,
        conformance: ConformanceMonitor | None = None,
        manager: AlertManager | None = None,
        scan_interval_s: float = DEFAULT_SCAN_INTERVAL_S,
        stale_after_s: float = STALE_TRACK_AFTER_S,
    ) -> None:
        self._subscriber = subscriber
        self._publisher = publisher
        self._alert_store = alert_store
        self._monitor = monitor or SeparationMonitor()
        self._conformance = conformance
        self._manager = manager or AlertManager()
        self._scan_interval_s = scan_interval_s
        self._stale_after_s = stale_after_s

        self._picture: dict[str, TrackUpdate] = {}
        #: The trace that delivered each track's latest position, for linking a
        #: scan back to the reports that caused it. Values are opaque
        #: OpenTelemetry span contexts, or None when tracing is not installed.
        self._trace_of: dict[str, Any] = {}
        #: Non-conformance findings seen since the last scan, latest per track.
        #: Held rather than published immediately so that every alert in the
        #: system goes through one lifecycle and one hysteresis policy.
        self._pending_conformance: dict[str, NonConformance] = {}
        self._updates = 0
        self._scans = 0
        self._alerts = 0
        self._latest_seen: datetime | None = None

    @property
    def live_tracks(self) -> int:
        return len(self._picture)

    @property
    def clock(self) -> datetime:
        """The time the scanner should reason about.

        The later of the wall clock and the newest timestamp in the data, and
        the reason is that this service must work in two different time regimes.

        Live, the feed is paced against the wall clock and the two agree. Under
        replay -- used for evaluation and for regenerating datasets -- the feed
        runs flat out, so data time races minutes or hours ahead of wall time.
        Judging staleness by the wall clock there would mark nothing stale, the
        picture would grow without bound, and conflicts would be computed across
        wildly inconsistent timestamps.

        Taking the maximum handles both, and also handles the feed stopping: the
        wall clock keeps advancing, so tracks still expire rather than freezing
        on the display forever.
        """
        wall = datetime.now(UTC)
        if self._latest_seen is None:
            return wall
        return max(wall, self._latest_seen)

    async def run(self) -> ConformanceStats:
        """Consume track updates until cancelled, scanning on a timer."""
        if self._subscriber is None:
            raise RuntimeError(
                "ConformanceRunner was built without a subscriber; "
                "drive it with absorb() and scan_now() instead of run()"
            )
        scanner = asyncio.create_task(self._scan_forever())
        try:
            async for envelope in self._subscriber.stream():
                self.absorb(envelope.message)
        finally:
            scanner.cancel()
            await asyncio.gather(scanner, return_exceptions=True)
        return ConformanceStats(
            updates_consumed=self._updates, scans=self._scans, alerts_published=self._alerts
        )

    def absorb(self, update: TrackUpdate) -> None:
        """Fold one track update into the airspace picture."""
        self._updates += 1
        if update.state is TrackState.TERMINATED:
            self._picture.pop(update.track_id, None)
            self._trace_of.pop(update.track_id, None)
            return
        existing = self._picture.get(update.track_id)
        # Out-of-order updates would move a track backwards in time and could
        # manufacture a closing geometry that never happened.
        if existing is not None and update.updated_at < existing.updated_at:
            return
        self._picture[update.track_id] = update
        # Remember which trace delivered this aircraft's latest position, so the
        # scan that acts on it can link back to the report that caused it. See
        # `scan_now` for why this is a link rather than a parent.
        self._trace_of[update.track_id] = current_span_context()
        # Clamped, because this ratchets and never goes backwards. One upstream
        # clock a year fast would otherwise pin the scan clock a year ahead
        # permanently, marking every real track stale and emptying the picture
        # for good. Under replay the feed legitimately runs ahead of the wall
        # clock, so the bound is generous rather than exact.
        if update.updated_at <= datetime.now(UTC) + MAX_DATA_CLOCK_LEAD and (
            self._latest_seen is None or update.updated_at > self._latest_seen
        ):
            self._latest_seen = update.updated_at

        # Conformance is checked per update, not per scan: the question is
        # whether *this* aircraft went where it was predicted to, which is
        # answered the moment its next position arrives. Deferring it to the
        # scan would add up to a scan interval of latency for no benefit.
        if self._conformance is not None:
            finding = self._conformance.observe(update)
            if finding is not None:
                self._pending_conformance[finding.key] = finding

    async def scan_now(self, now: datetime) -> list[str]:
        """Run one scan and publish whatever changed. Returns the alert keys.

        ## Why the span wraps the whole method, and why it has links

        The first version opened a span around the geometry alone and closed it
        before the alerts were built and published — so the interval anybody
        actually wants, *report observed to alert raised*, ended one step short
        of the alert. It was also an unparented root span, because the scanner
        runs as a detached task with no ambient trace context. Jaeger showed the
        report's trace stopping at the conformance consume, and a separate,
        unrelated `conflict-scan` trace beside it. Two external reviews found
        it.

        Parenting is the wrong repair. A timer-driven scan is not caused by one
        report; it is caused by every track in the picture, and nesting it under
        whichever update happened to arrive last would be a fiction that looks
        like a fact. **Span links** say what is true: these traces contributed
        to this scan. A reviewer following an alert can jump straight to the
        reports behind it, and the causality is many-to-one because it really
        is many-to-one.
        """
        self._scans += 1
        vanished = self._expire_stale(now)
        links = [context for context in self._trace_of.values() if context is not None]

        with span("conflict-scan", links=links, tracks=len(self._picture)):
            return await self._scan_within_span(now, vanished)

    async def _scan_within_span(self, now: datetime, vanished: list[str]) -> list[str]:
        with METRICS.time(METRICS.scan_duration, "conformance"):
            conflicts = self._monitor.scan(list(self._picture.values()))
        detections = [self._as_detection(c) for c in conflicts]
        for track in self._picture.values():
            detections.extend(
                Detection(
                    key=finding.key,
                    kind=finding.kind,
                    severity=finding.severity,
                    summary=finding.summary,
                    reason_codes=finding.reason_codes,
                    track_ids=(finding.track_id,),
                    scenario_id=track.scenario_id,
                )
                for finding in check_rules(track)
            )

        detections.extend(
            Detection(
                key=finding.key,
                kind=AlertKind.NON_CONFORMANCE,
                severity=finding.severity,
                summary=finding.summary,
                reason_codes=finding.reason_codes,
                track_ids=(finding.track_id,),
                conformance=finding.as_evidence(),
                scenario_id=(
                    self._picture[finding.track_id].scenario_id
                    if finding.track_id in self._picture
                    else None
                ),
            )
            for finding in self._pending_conformance.values()
        )
        self._pending_conformance.clear()

        alerts = self._manager.reconcile(detections, now)
        alerts.extend(self._manager.forget(vanished, now))

        if self._conformance is not None and vanished:
            self._conformance.forget(vanished)

        for alert in alerts:
            # Keyed by alert_key so every state change for one condition lands
            # on the same partition and arrives in order. A CLEARED overtaking
            # its own NEW would leave a consumer showing an alert forever.
            await self._publisher.publish(TOPIC_ALERTS, key=alert.alert_key, message=alert)
            self._alerts += 1
            METRICS.alerts_published.labels(
                service="conformance", kind=str(alert.kind), state=str(alert.state)
            ).inc()

        # The read model the API renders from. Written after publishing, so a
        # crash between the two loses the display update but not the event --
        # the topic is the record, Redis is a convenience.
        if self._alert_store is not None and alerts:
            await self._alert_store.apply(alerts)

        METRICS.live_tracks.labels(service="conformance").set(len(self._picture))
        METRICS.active_alerts.labels(service="conformance").set(self._manager.active_count)

        if alerts:
            _log.info(
                "published alerts",
                extra={
                    "count": len(alerts),
                    "active": self._manager.active_count,
                    "tracks": len(self._picture),
                },
            )
        return [alert.alert_key for alert in alerts]

    def _expire_stale(self, now: datetime) -> list[str]:
        """Drop tracks whose last update is too old to reason about."""
        cutoff = now - timedelta(seconds=self._stale_after_s)
        stale = [tid for tid, t in self._picture.items() if t.updated_at < cutoff]
        for track_id in stale:
            del self._picture[track_id]
            # Otherwise the span-link map grows for the life of the process and
            # every scan links to traces for aircraft that left hours ago.
            self._trace_of.pop(track_id, None)
        return stale

    @staticmethod
    def _as_detection(conflict: Conflict) -> Detection:
        first = conflict.first_callsign or conflict.first_track_id
        second = conflict.second_callsign or conflict.second_track_id
        return Detection(
            key=conflict.key,
            kind=AlertKind.PREDICTED_CONFLICT,
            severity=_severity_for(conflict),
            summary=(
                f"{first} and {second}: predicted separation "
                f"{conflict.min_horizontal_nm:.1f} NM / "
                f"{conflict.min_vertical_ft:.0f} ft "
                f"in {conflict.time_to_cpa_s:.0f} s"
            ),
            reason_codes=conflict.reason_codes,
            track_ids=(conflict.first_track_id, conflict.second_track_id),
            conflict=ConflictEvidence(
                time_to_cpa_s=conflict.time_to_cpa_s,
                min_horizontal_sep_nm=conflict.min_horizontal_nm,
                min_vertical_sep_ft=conflict.min_vertical_ft,
                lookahead_s=conflict.lookahead_s,
                horizontal_standard_nm=conflict.horizontal_standard_nm,
                vertical_standard_ft=conflict.vertical_standard_ft,
            ),
        )

    async def _scan_forever(self) -> None:
        while True:
            await asyncio.sleep(self._scan_interval_s)
            try:
                await self.scan_now(self.clock)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed scan must not stop consumption
                _log.exception("conflict scan failed")


def _severity_for(conflict: Conflict) -> Severity:
    """Escalate with urgency, not with how bad the predicted miss is.

    A predicted 0.1 NM miss five minutes out is less urgent than a 4 NM miss
    thirty seconds out, because there is still time to do something about the
    former. Time is the scarce resource, so time drives severity.
    """
    if conflict.time_to_cpa_s <= 60.0:
        return Severity.WARNING
    if conflict.time_to_cpa_s <= 180.0:
        return Severity.CAUTION
    return Severity.ADVISORY
