"""Tests for the read API.

Health and readiness get the most attention here, not because they are complex
but because getting them the wrong way round is the classic Kubernetes outage:
a liveness probe that checks the database restarts the pod every time the
database hiccups, turning a recoverable dependency failure into a crash loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi.testclient import TestClient

from acp.common.config import Settings
from acp.common.contracts import DataSource, TrackState, TrackUpdate
from acp.services.api.app import create_app, get_history, get_live
from acp.storage.stores import LiveTrackStore, TrackHistoryStore
from tests.unit.fakes import FakeHistoryStore, FakeLiveStore

NOW = datetime.now(UTC)


def a_track(
    track_id: str = "trk-a1b2c3", *, callsign: str = "ACP101", age_s: float = 0.0
) -> TrackUpdate:
    at = NOW - timedelta(seconds=age_s)
    return TrackUpdate(
        track_id=track_id,
        icao24="a1b2c3",
        callsign=callsign,
        updated_at=at,
        last_report_at=at,
        state=TrackState.CONFIRMED,
        lat=40.6,
        lon=-75.5,
        altitude_ft=35000.0,
        ground_speed_kt=450.0,
        track_deg=90.0,
        vertical_rate_fpm=0.0,
        turn_rate_deg_s=0.0,
        position_uncertainty_m=30.0,
        update_count=42,
        source=DataSource.SIMULATOR,
        scenario_id="head-on-conflict",
    )


def client(live: FakeLiveStore, history: FakeHistoryStore) -> TestClient:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    app.dependency_overrides[get_live] = lambda: cast(LiveTrackStore, live)
    app.dependency_overrides[get_history] = lambda: cast(TrackHistoryStore, history)
    return TestClient(app)


# --------------------------------------------------------------------------
# Operations endpoints
# --------------------------------------------------------------------------


def test_liveness_ignores_dependencies() -> None:
    """A dead database must not get the process killed and restarted forever."""
    live = FakeLiveStore(healthy=False)
    history = FakeHistoryStore(healthy=False)
    with client(live, history) as c:
        response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readiness_passes_when_both_dependencies_answer() -> None:
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        response = c.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "redis": True, "postgres": True}


def test_readiness_fails_with_503_and_names_the_culprit() -> None:
    """503 removes the pod from the load balancer; the body says which half broke."""
    with client(FakeLiveStore(healthy=False), FakeHistoryStore()) as c:
        response = c.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body == {"ready": False, "redis": False, "postgres": True}


def test_readiness_fails_when_postgres_is_unreachable() -> None:
    with client(FakeLiveStore(), FakeHistoryStore(healthy=False)) as c:
        response = c.get("/ready")
    assert response.status_code == 503
    assert response.json()["postgres"] is False


# --------------------------------------------------------------------------
# Airspace picture
# --------------------------------------------------------------------------


def test_an_empty_airspace_is_an_empty_list_not_an_error() -> None:
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        response = c.get("/v1/tracks")
    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["tracks"] == []


def test_live_tracks_are_returned_with_a_generation_time() -> None:
    """The timestamp is what lets a display tell "quiet" from "stuck"."""
    live = FakeLiveStore(current={"trk-a1b2c3": a_track()})
    with client(live, FakeHistoryStore()) as c:
        body = c.get("/v1/tracks").json()
    assert body["count"] == 1
    assert body["tracks"][0]["callsign"] == "ACP101"
    assert body["generated_at"]


def test_tracks_are_ordered_by_callsign() -> None:
    live = FakeLiveStore(
        current={
            "trk-2": a_track("trk-2", callsign="ZZZ999"),
            "trk-1": a_track("trk-1", callsign="AAA111"),
        }
    )
    with client(live, FakeHistoryStore()) as c:
        body = c.get("/v1/tracks").json()
    assert [t["callsign"] for t in body["tracks"]] == ["AAA111", "ZZZ999"]


def test_stale_tracks_fall_out_of_the_live_window() -> None:
    """Showing an aircraft that is no longer there is worse than showing none."""
    live = FakeLiveStore(current={"trk-old": a_track("trk-old", age_s=300.0)})
    with client(live, FakeHistoryStore()) as c:
        body = c.get("/v1/tracks").json()
    assert body["count"] == 0


def test_the_live_window_can_be_widened_by_the_caller() -> None:
    live = FakeLiveStore(current={"trk-old": a_track("trk-old", age_s=300.0)})
    with client(live, FakeHistoryStore()) as c:
        assert c.get("/v1/tracks", params={"window_s": 600}).json()["count"] == 1


def test_an_absurd_live_window_is_rejected() -> None:
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        assert c.get("/v1/tracks", params={"window_s": 0}).status_code == 422
        assert c.get("/v1/tracks", params={"window_s": 100000}).status_code == 422


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_history_returns_points_for_a_known_track() -> None:
    history = FakeHistoryStore(batches=[[a_track(), a_track()]])
    with client(FakeLiveStore(), history) as c:
        body = c.get("/v1/tracks/trk-a1b2c3/history").json()
    assert body["track_id"] == "trk-a1b2c3"
    assert body["count"] == 2
    assert body["points"][0]["lat"] == 40.6


def test_history_for_an_unknown_track_is_a_404() -> None:
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        response = c.get("/v1/tracks/trk-nope/history")
    assert response.status_code == 404
    assert "trk-nope" in response.json()["detail"]


# --------------------------------------------------------------------------
# Contract surface
# --------------------------------------------------------------------------


def test_the_openapi_schema_documents_the_public_routes() -> None:
    """The spec is a deliverable; M4 fuzzes the implementation against it."""
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        schema = c.get("/openapi.json").json()
    assert set(schema["paths"]) >= {
        "/health",
        "/ready",
        "/v1/tracks",
        "/v1/tracks/{track_id}/history",
    }


def test_the_api_description_states_the_safety_scope() -> None:
    """Anyone reading the generated docs sees the disclaimer without hunting."""
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        schema = c.get("/openapi.json").json()
    description = schema["info"]["description"].lower()
    assert "synthetic" in description
    assert "not certified" in description or "advisory" in description


def test_the_display_is_served_at_the_root() -> None:
    with client(FakeLiveStore(), FakeHistoryStore()) as c:
        response = c.get("/")
    assert response.status_code == 200
    assert "Airspace Conformance Platform" in response.text
