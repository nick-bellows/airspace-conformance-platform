"""Alert lifecycle: raise once, sustain while true, clear when it stops.

Detectors are stateless -- they answer "is this a conflict right now?" for the
current picture, several times a second. Publishing that answer directly would
be unusable: a marginal geometry sitting on the threshold produces a new alert
every scan, and an operator watching it would see a flood.

This class turns a stream of detections into a stream of *state changes*:

    NEW        the first time a condition is seen
    SUSTAINED  periodically, while it persists
    CLEARED    once it has been absent for long enough to believe

**Hysteresis is the whole point.** A conflict that disappears for one scan is
almost always noise, not a resolution -- one aircraft's velocity estimate
wobbled and the predicted miss distance crossed the threshold. Clearing
immediately and re-raising a second later is worse than useless: it trains
whoever is watching to ignore the display. So a condition must be absent for
`clear_after` consecutive scans before it clears.

The asymmetry is deliberate. Raising is instant, clearing is slow. Being late to
warn is dangerous; being late to stop warning is merely annoying.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from acp.common.contracts import (
    Alert,
    AlertKind,
    AlertState,
    ConflictEvidence,
    ConformanceEvidence,
    DataSource,
    Severity,
)

#: Consecutive clean scans before a condition is believed to be over.
DEFAULT_CLEAR_AFTER_SCANS = 3

#: How often a still-active alert is republished. Consumers key on `alert_key`
#: and keep the latest, so this is a heartbeat rather than a new event: it is
#: what lets a consumer that started late learn about an alert already running.
DEFAULT_SUSTAIN_INTERVAL_S = 10.0


@dataclass(frozen=True, slots=True)
class Detection:
    """A detector's stateless answer for one condition at one instant."""

    key: str
    kind: AlertKind
    severity: Severity
    summary: str
    reason_codes: tuple[str, ...]
    track_ids: tuple[str, ...]
    conflict: ConflictEvidence | None = None
    conformance: ConformanceEvidence | None = None
    scenario_id: str | None = None


@dataclass(slots=True)
class _Active:
    """Bookkeeping for one condition currently being tracked."""

    alert_id: str
    raised_at: datetime
    last_published_at: datetime
    #: The most recent detection for this key. Kept so a CLEARED alert can still
    #: name the aircraft and the reason, after the condition itself is gone.
    detection: Detection
    missed_scans: int = 0


class AlertManager:
    """Turns repeated detections into an alert lifecycle."""

    def __init__(
        self,
        *,
        clear_after_scans: int = DEFAULT_CLEAR_AFTER_SCANS,
        sustain_interval_s: float = DEFAULT_SUSTAIN_INTERVAL_S,
    ) -> None:
        self._active: dict[str, _Active] = {}
        self._clear_after_scans = clear_after_scans
        self._sustain_interval_s = sustain_interval_s

    @property
    def active_count(self) -> int:
        return len(self._active)

    def active_keys(self) -> frozenset[str]:
        return frozenset(self._active)

    def reconcile(self, detections: Iterable[Detection], now: datetime) -> list[Alert]:
        """Fold one scan's detections into the lifecycle.

        Returns only alerts worth publishing: newly raised, due for a heartbeat,
        or newly cleared. A scan where nothing changed publishes nothing.
        """
        current = {detection.key: detection for detection in detections}
        published: list[Alert] = []

        for key, detection in current.items():
            existing = self._active.get(key)
            if existing is None:
                active = _Active(
                    alert_id=uuid.uuid4().hex,
                    raised_at=now,
                    last_published_at=now,
                    detection=detection,
                )
                self._active[key] = active
                published.append(self._render(active, detection, AlertState.NEW, now))
                continue

            existing.missed_scans = 0
            existing.detection = detection
            since_s = (now - existing.last_published_at).total_seconds()
            if since_s >= self._sustain_interval_s:
                existing.last_published_at = now
                published.append(self._render(existing, detection, AlertState.SUSTAINED, now))

        for key in list(self._active):
            if key in current:
                continue
            active = self._active[key]
            active.missed_scans += 1
            if active.missed_scans >= self._clear_after_scans:
                del self._active[key]
                published.append(self._render(active, active.detection, AlertState.CLEARED, now))

        return published

    def _render(
        self, active: _Active, detection: Detection, state: AlertState, now: datetime
    ) -> Alert:
        summary = detection.summary
        if state is AlertState.CLEARED:
            # The stored summary describes a condition that no longer holds, so
            # say so rather than republishing a stale present-tense claim.
            summary = f"Cleared: {summary}"
        return Alert(
            alert_id=active.alert_id,
            alert_key=detection.key,
            kind=detection.kind,
            severity=detection.severity,
            state=state,
            raised_at=active.raised_at,
            updated_at=now,
            track_ids=detection.track_ids,
            reason_codes=detection.reason_codes,
            summary=summary,
            conflict=detection.conflict,
            conformance=detection.conformance,
            source=DataSource.SIMULATOR,
            scenario_id=detection.scenario_id,
        )

    def forget(self, track_ids: Iterable[str], now: datetime) -> list[Alert]:
        """Clear alerts involving tracks that no longer exist.

        A terminated track cannot resolve its own conflict by flying away -- it
        simply stops being reported. Without this, its alert would sit at the
        top of the list forever, describing two aircraft one of which the system
        no longer believes in.
        """
        gone = frozenset(track_ids)
        cleared = []
        for key in list(self._active):
            active = self._active[key]
            if gone.intersection(active.detection.track_ids):
                del self._active[key]
                cleared.append(self._render(active, active.detection, AlertState.CLEARED, now))
        return cleared
