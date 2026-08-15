"""Tests for the alert lifecycle and the single-aircraft rules.

The behaviour worth protecting here is **hysteresis**. Detectors are stateless
and run several times a second; publishing their raw output would flood anyone
watching with alerts that appear and vanish as a marginal geometry wobbles
across a threshold. The asymmetry -- raise instantly, clear slowly -- is a
safety choice, and these tests are what stop someone "simplifying" it away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from acp.common.contracts import (
    AlertKind,
    AlertState,
    DataSource,
    Severity,
    TrackState,
    TrackUpdate,
)
from acp.services.conformance.alerts import AlertManager, Detection
from acp.services.conformance.rules import check as check_rules

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_detection(key: str = "predicted_conflict:trk-a:trk-b", **overrides: object) -> Detection:
    fields: dict[str, object] = {
        "key": key,
        "kind": AlertKind.PREDICTED_CONFLICT,
        "severity": Severity.CAUTION,
        "summary": "A and B: predicted separation 2.1 NM / 300 ft in 142 s",
        "reason_codes": ("horizontal_below_standard", "vertical_below_standard"),
        "track_ids": ("trk-a", "trk-b"),
    }
    fields.update(overrides)
    return Detection(**fields)  # type: ignore[arg-type]


def a_track(**overrides: object) -> TrackUpdate:
    fields: dict[str, object] = {
        "track_id": "trk-a",
        "icao24": "a1b2c3",
        "callsign": "ACP101",
        "updated_at": NOW,
        "last_report_at": NOW,
        "state": TrackState.CONFIRMED,
        "lat": 40.0,
        "lon": -75.0,
        "altitude_ft": 35000.0,
        "ground_speed_kt": 450.0,
        "track_deg": 90.0,
        "vertical_rate_fpm": 0.0,
        "turn_rate_deg_s": 0.0,
        "position_uncertainty_m": 30.0,
        "update_count": 50,
        "source": DataSource.SIMULATOR,
    }
    fields.update(overrides)
    return TrackUpdate(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Raising
# --------------------------------------------------------------------------


def test_a_first_detection_raises_a_new_alert() -> None:
    published = AlertManager().reconcile([a_detection()], NOW)
    assert len(published) == 1
    assert published[0].state is AlertState.NEW
    assert published[0].kind is AlertKind.PREDICTED_CONFLICT


def test_a_repeated_detection_does_not_republish_immediately() -> None:
    """Otherwise every scan is an event and the display is unreadable."""
    manager = AlertManager(sustain_interval_s=10.0)
    manager.reconcile([a_detection()], NOW)
    assert manager.reconcile([a_detection()], NOW + timedelta(seconds=1)) == []


def test_a_persisting_condition_is_republished_as_a_heartbeat() -> None:
    """A consumer that started late has to be able to learn about it."""
    manager = AlertManager(sustain_interval_s=10.0)
    manager.reconcile([a_detection()], NOW)
    published = manager.reconcile([a_detection()], NOW + timedelta(seconds=11))
    assert [a.state for a in published] == [AlertState.SUSTAINED]


def test_the_alert_id_is_stable_across_state_changes() -> None:
    """One condition is one alert, however many times it is republished."""
    manager = AlertManager(sustain_interval_s=1.0, clear_after_scans=1)
    first = manager.reconcile([a_detection()], NOW)[0]
    sustained = manager.reconcile([a_detection()], NOW + timedelta(seconds=2))[0]
    cleared = manager.reconcile([], NOW + timedelta(seconds=3))[0]
    assert first.alert_id == sustained.alert_id == cleared.alert_id
    assert first.raised_at == cleared.raised_at


# --------------------------------------------------------------------------
# Hysteresis: the point of the whole class
# --------------------------------------------------------------------------


def test_a_single_missed_scan_does_not_clear_an_alert() -> None:
    """A marginal geometry crossing the threshold for one scan is noise.

    Clearing and re-raising trains whoever is watching to ignore the display,
    which is a worse failure than a slightly stale alert.
    """
    manager = AlertManager(clear_after_scans=3)
    manager.reconcile([a_detection()], NOW)
    assert manager.reconcile([], NOW + timedelta(seconds=1)) == []
    assert manager.active_count == 1


def test_an_alert_clears_after_enough_consecutive_quiet_scans() -> None:
    manager = AlertManager(clear_after_scans=3)
    manager.reconcile([a_detection()], NOW)
    for offset in (1, 2):
        assert manager.reconcile([], NOW + timedelta(seconds=offset)) == []
    published = manager.reconcile([], NOW + timedelta(seconds=3))
    assert [a.state for a in published] == [AlertState.CLEARED]
    assert manager.active_count == 0


def test_a_reappearing_condition_resets_the_clear_countdown() -> None:
    """Flickering must not accumulate toward a clear."""
    manager = AlertManager(clear_after_scans=3)
    manager.reconcile([a_detection()], NOW)
    manager.reconcile([], NOW + timedelta(seconds=1))
    manager.reconcile([], NOW + timedelta(seconds=2))
    manager.reconcile([a_detection()], NOW + timedelta(seconds=3))  # back
    assert manager.reconcile([], NOW + timedelta(seconds=4)) == []
    assert manager.active_count == 1


def test_a_cleared_alert_says_so_in_its_summary() -> None:
    """The stored summary describes a condition that no longer holds."""
    manager = AlertManager(clear_after_scans=1)
    manager.reconcile([a_detection()], NOW)
    cleared = manager.reconcile([], NOW + timedelta(seconds=1))[0]
    assert cleared.summary.startswith("Cleared:")


# --------------------------------------------------------------------------
# Vanishing tracks
# --------------------------------------------------------------------------


def test_alerts_are_cleared_when_a_track_disappears() -> None:
    """A terminated track cannot fly its way out of a conflict.

    Without this its alert sits at the top of the list forever, describing two
    aircraft one of which the system no longer believes exists.
    """
    manager = AlertManager()
    manager.reconcile([a_detection()], NOW)
    cleared = manager.forget(["trk-b"], NOW + timedelta(seconds=5))
    assert [a.state for a in cleared] == [AlertState.CLEARED]
    assert manager.active_count == 0


def test_forgetting_an_unrelated_track_leaves_the_alert_alone() -> None:
    manager = AlertManager()
    manager.reconcile([a_detection()], NOW)
    assert manager.forget(["trk-zzz"], NOW + timedelta(seconds=5)) == []
    assert manager.active_count == 1


# --------------------------------------------------------------------------
# Multiple conditions
# --------------------------------------------------------------------------


def test_separate_conditions_are_tracked_independently() -> None:
    manager = AlertManager(clear_after_scans=1)
    manager.reconcile(
        [a_detection("conflict:a:b"), a_detection("conflict:c:d", track_ids=("trk-c", "trk-d"))],
        NOW,
    )
    assert manager.active_count == 2

    published = manager.reconcile([a_detection("conflict:a:b")], NOW + timedelta(seconds=1))
    assert [a.alert_key for a in published] == ["conflict:c:d"]
    assert manager.active_keys() == frozenset({"conflict:a:b"})


def test_an_updated_detection_replaces_the_stored_evidence() -> None:
    """A conflict that gets more urgent must publish the newer numbers."""
    manager = AlertManager(sustain_interval_s=1.0)
    manager.reconcile([a_detection(summary="4.9 NM in 280 s")], NOW)
    published = manager.reconcile(
        [a_detection(summary="2.1 NM in 40 s", severity=Severity.WARNING)],
        NOW + timedelta(seconds=2),
    )
    assert published[0].summary == "2.1 NM in 40 s"
    assert published[0].severity is Severity.WARNING


# --------------------------------------------------------------------------
# Single-aircraft rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("squawk", "code"),
    [
        ("7500", "squawk_7500_hijack"),
        ("7600", "squawk_7600_radio_failure"),
        ("7700", "squawk_7700_emergency"),
    ],
)
def test_emergency_squawks_always_raise_a_warning(squawk: str, code: str) -> None:
    """There is no such thing as a low-priority 7700."""
    findings = check_rules(a_track(squawk=squawk))
    assert len(findings) == 1
    assert findings[0].kind is AlertKind.EMERGENCY_SQUAWK
    assert findings[0].severity is Severity.WARNING
    assert code in findings[0].reason_codes


def test_an_ordinary_squawk_raises_nothing() -> None:
    assert check_rules(a_track(squawk="2000")) == []
    assert check_rules(a_track(squawk=None)) == []


def test_a_very_steep_descent_at_altitude_is_a_caution() -> None:
    """Expedited descents happen. Worth noticing, not worth alarming about."""
    findings = check_rules(a_track(altitude_ft=33000.0, vertical_rate_fpm=-7000.0))
    assert len(findings) == 1
    assert findings[0].severity is Severity.CAUTION
    assert "descent_rate_above_6000_fpm" in findings[0].reason_codes


def test_a_steep_descent_at_low_altitude_is_a_warning() -> None:
    """Same rate, far less room. Altitude is what makes it serious."""
    findings = check_rules(a_track(altitude_ft=4000.0, vertical_rate_fpm=-4000.0))
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARNING
    assert "steep_descent_below_10000_ft" in findings[0].reason_codes


def test_a_normal_descent_raises_nothing() -> None:
    assert check_rules(a_track(altitude_ft=20000.0, vertical_rate_fpm=-2000.0)) == []
    assert check_rules(a_track(altitude_ft=5000.0, vertical_rate_fpm=-1500.0)) == []


def test_a_climb_never_triggers_the_descent_rule() -> None:
    assert check_rules(a_track(altitude_ft=5000.0, vertical_rate_fpm=4000.0)) == []


def test_several_rules_can_fire_on_one_aircraft() -> None:
    findings = check_rules(a_track(squawk="7700", altitude_ft=3000.0, vertical_rate_fpm=-5000.0))
    assert {f.kind for f in findings} == {AlertKind.EMERGENCY_SQUAWK, AlertKind.EXCESSIVE_DESCENT}


def test_rule_findings_have_distinct_keys_per_kind() -> None:
    """Two rules on one aircraft are two alerts, not one overwriting the other."""
    findings = check_rules(a_track(squawk="7700", altitude_ft=3000.0, vertical_rate_fpm=-5000.0))
    assert len({f.key for f in findings}) == len(findings)


def test_a_rule_summary_names_the_aircraft() -> None:
    finding = check_rules(a_track(callsign="ACP999", squawk="7700"))[0]
    assert "ACP999" in finding.summary
