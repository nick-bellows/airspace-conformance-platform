"""Tests for the wire contracts themselves.

Three things are being protected here: that messages are immutable once built,
that malformed values are rejected at the boundary rather than deep inside a
detector, and that the committed JSON Schemas match the models.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from scripts.contracts import check_all

from acp.common.contracts import (
    TOPIC_ALERTS,
    TOPIC_MODELS,
    TOPIC_SURVEILLANCE_REPORTS,
    TOPIC_TRACK_UPDATES,
    Alert,
    AlertKind,
    AlertState,
    ConflictEvidence,
    DataSource,
    Severity,
    SurveillanceReport,
    TrackState,
    TrackUpdate,
    TruthState,
)

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def a_report(**overrides: object) -> SurveillanceReport:
    fields: dict[str, object] = {
        "report_id": "r-1",
        "icao24": "a1b2c3",
        "callsign": "TEST123",
        "observed_at": NOW,
        "lat": 40.0,
        "lon": -75.0,
        "altitude_baro_ft": 35000.0,
        "ground_speed_kt": 450.0,
        "track_deg": 270.0,
    }
    fields.update(overrides)
    return SurveillanceReport(**fields)  # type: ignore[arg-type]


def a_track(**overrides: object) -> TrackUpdate:
    fields: dict[str, object] = {
        "track_id": "t-1",
        "icao24": "a1b2c3",
        "updated_at": NOW,
        "last_report_at": NOW,
        "state": TrackState.CONFIRMED,
        "lat": 40.0,
        "lon": -75.0,
        "altitude_ft": 35000.0,
        "ground_speed_kt": 450.0,
        "track_deg": 270.0,
        "vertical_rate_fpm": 0.0,
        "turn_rate_deg_s": 0.0,
        "position_uncertainty_m": 50.0,
        "update_count": 12,
    }
    fields.update(overrides)
    return TrackUpdate(**fields)  # type: ignore[arg-type]


def test_committed_schemas_match_the_models() -> None:
    """The same gate CI runs, so drift fails locally before it fails in CI."""
    assert check_all() == []


def test_every_topic_has_a_schema_file() -> None:
    assert set(TOPIC_MODELS) == {
        TOPIC_SURVEILLANCE_REPORTS,
        TOPIC_TRACK_UPDATES,
        TOPIC_ALERTS,
        "sim.truth.v1",
    }


@pytest.mark.parametrize("topic", sorted(TOPIC_MODELS))
def test_wire_models_are_immutable(topic: str) -> None:
    """Frozen models mean a consumer cannot mutate a message another handler still holds."""
    model = TOPIC_MODELS[topic]
    assert model.model_config.get("frozen") is True
    assert model.model_config.get("extra") == "forbid"


def test_a_report_cannot_be_mutated_after_construction() -> None:
    report = a_report()
    with pytest.raises(ValidationError):
        report.lat = 41.0  # type: ignore[misc]


def test_unknown_fields_are_rejected() -> None:
    """Catches a producer that adds a field without regenerating the schema."""
    with pytest.raises(ValidationError):
        a_report(altitude_geometric_ft=35100.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lat", 90.5),
        ("lat", -90.5),
        ("lon", 180.5),
        ("track_deg", 360.0),
        ("track_deg", -1.0),
        ("ground_speed_kt", -1.0),
        ("icao24", "A1B2C3"),  # uppercase: ADS-B addresses are lowercase hex here
        ("icao24", "a1b2c"),  # too short
        ("squawk", "7800"),  # squawk is octal
        ("squawk", "770"),
    ],
)
def test_out_of_range_values_are_rejected_at_the_boundary(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        a_report(**{field: value})


def test_emergency_squawks_are_valid_values() -> None:
    """7500/7600/7700 are the hijack, radio-failure, and general-emergency codes."""
    for code in ("7500", "7600", "7700"):
        assert a_report(squawk=code).squawk == code


def test_optional_kinematics_may_be_absent_on_a_report() -> None:
    """Real surveillance drops fields; a report with position only must still parse."""
    sparse = SurveillanceReport(
        report_id="r-2", icao24="a1b2c3", observed_at=NOW, lat=40.0, lon=-75.0
    )
    assert sparse.ground_speed_kt is None
    assert sparse.source is DataSource.SIMULATOR


def test_track_updates_always_carry_kinematics() -> None:
    """Unlike a report, an estimate is never partial - the filter always has one."""
    with pytest.raises(ValidationError):
        TrackUpdate(  # type: ignore[call-arg]
            track_id="t-1",
            icao24="a1b2c3",
            updated_at=NOW,
            last_report_at=NOW,
            state=TrackState.CONFIRMED,
            lat=40.0,
            lon=-75.0,
        )


@pytest.mark.parametrize("topic", sorted(TOPIC_MODELS))
def test_messages_survive_a_json_round_trip(topic: str) -> None:
    """Kafka carries bytes; anything that cannot round-trip is unusable."""
    samples = {
        TOPIC_SURVEILLANCE_REPORTS: a_report(),
        TOPIC_TRACK_UPDATES: a_track(),
        TOPIC_ALERTS: an_alert(),
        "sim.truth.v1": a_truth(),
    }
    original = samples[topic]
    restored = TOPIC_MODELS[topic].model_validate(json.loads(original.model_dump_json()))
    assert restored == original


def an_alert() -> Alert:
    return Alert(
        alert_id="al-1",
        alert_key="predicted_conflict:t-1:t-2",
        kind=AlertKind.PREDICTED_CONFLICT,
        severity=Severity.CAUTION,
        state=AlertState.NEW,
        raised_at=NOW,
        updated_at=NOW,
        track_ids=("t-1", "t-2"),
        reason_codes=("horizontal_below_standard", "vertical_below_standard"),
        summary="Predicted loss of separation in 142 s",
        conflict=ConflictEvidence(
            time_to_cpa_s=142.0,
            min_horizontal_sep_nm=2.1,
            min_vertical_sep_ft=300.0,
            lookahead_s=300.0,
            horizontal_standard_nm=5.0,
            vertical_standard_ft=1000.0,
        ),
    )


def a_truth() -> TruthState:
    return TruthState(
        icao24="a1b2c3",
        scenario_id="head-on-1",
        sim_version="acp-sim-v1",
        valid_at=NOW,
        lat=40.0,
        lon=-75.0,
        altitude_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=270.0,
        vertical_rate_fpm=0.0,
        phase="cruise",
    )


def test_an_alert_always_explains_itself() -> None:
    """Reason codes make an alert reviewable without re-running the detector."""
    alert = an_alert()
    assert alert.reason_codes
    assert alert.conflict is not None
    assert alert.conflict.min_horizontal_sep_nm < alert.conflict.horizontal_standard_nm
