"""Single-aircraft rule checks.

These are deterministic tests on one track, with no geometry and no model. They
exist partly because they are genuinely useful and partly because they are the
control group: if the rule alerts behave and the model-driven ones do not, the
problem is the model rather than the plumbing.

Every rule returns a reason code, never a bare boolean, so an alert can be
explained without re-running the detector.
"""

from __future__ import annotations

from dataclasses import dataclass

from acp.common.contracts import AlertKind, Severity, TrackUpdate

RULES_VERSION = "acp-rules-v1"

#: Transponder codes reserved worldwide for emergencies. A pilot squawking one
#: of these has declared something; there is no interpretation involved.
EMERGENCY_SQUAWKS = {
    "7500": ("unlawful interference", "squawk_7500_hijack"),
    "7600": ("radio failure", "squawk_7600_radio_failure"),
    "7700": ("general emergency", "squawk_7700_emergency"),
}

#: Descent rates beyond this are outside normal operations for a transport
#: aircraft. Emergency descents legitimately exceed it -- which is the point.
EXCESSIVE_DESCENT_FPM = -6000.0

#: Below this, a high descent rate is far more serious than it is at cruise.
LOW_ALTITUDE_FT = 10000.0
LOW_ALTITUDE_DESCENT_FPM = -3000.0


@dataclass(frozen=True, slots=True)
class RuleFinding:
    """One rule that fired on one track."""

    track_id: str
    kind: AlertKind
    severity: Severity
    summary: str
    reason_codes: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.track_id}"


def check(track: TrackUpdate) -> list[RuleFinding]:
    """Apply every rule to one track."""
    findings = []
    label = track.callsign or track.icao24

    if track.squawk in EMERGENCY_SQUAWKS:
        description, code = EMERGENCY_SQUAWKS[track.squawk]
        findings.append(
            RuleFinding(
                track_id=track.track_id,
                kind=AlertKind.EMERGENCY_SQUAWK,
                # Always WARNING. There is no such thing as a low-priority 7700.
                severity=Severity.WARNING,
                summary=f"{label} squawking {track.squawk} ({description})",
                reason_codes=(code,),
            )
        )

    descent = _descent_finding(track, label)
    if descent is not None:
        findings.append(descent)

    return findings


def _descent_finding(track: TrackUpdate, label: str) -> RuleFinding | None:
    """Flag a descent rate that is abnormal for the altitude it is happening at."""
    rate = track.vertical_rate_fpm
    if rate >= 0.0:
        return None

    low_and_steep = track.altitude_ft < LOW_ALTITUDE_FT and rate <= LOW_ALTITUDE_DESCENT_FPM
    very_steep = rate <= EXCESSIVE_DESCENT_FPM
    if not (low_and_steep or very_steep):
        return None

    codes = []
    if very_steep:
        codes.append("descent_rate_above_6000_fpm")
    if low_and_steep:
        codes.append("steep_descent_below_10000_ft")

    return RuleFinding(
        track_id=track.track_id,
        kind=AlertKind.EXCESSIVE_DESCENT,
        # Steep and low is the dangerous combination; steep at altitude may be
        # an entirely normal expedited descent.
        severity=Severity.WARNING if low_and_steep else Severity.CAUTION,
        summary=(
            f"{label} descending at {abs(round(rate)):,.0f} fpm "
            f"through {round(track.altitude_ft):,.0f} ft"
        ),
        reason_codes=tuple(codes),
    )
