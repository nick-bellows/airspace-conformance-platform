"""The HTTP surface conforms to its own published specification.

Two different questions, both worth asking.

**Does the spec still describe the service?** `scripts/contracts.py --check`
answers that by diffing the committed `contracts/openapi.json` against the one
the application generates. A spec allowed to drift is one nobody can generate a
client from.

**Does the service obey the spec it publishes?** That is what the fuzzing here
is for. Schemathesis reads the committed document, generates requests from it --
including inputs no hand-written test would think of -- and checks that every
response matches a documented status code and schema. A route that returns an
undocumented 500 on a plausible input fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from acp.common.config import Settings
from acp.services.api.app import create_app, get_alerts, get_history, get_live
from acp.storage.stores import LiveAlertStore, LiveTrackStore, TrackHistoryStore
from tests.unit.fakes import FakeAlertStore, FakeHistoryStore, FakeLiveStore

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "contracts" / "openapi.json"

schemathesis = pytest.importorskip("schemathesis", reason="the integration extra is not installed")


def _client() -> TestClient:
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    app.dependency_overrides[get_live] = lambda: cast(LiveTrackStore, FakeLiveStore())
    app.dependency_overrides[get_history] = lambda: cast(TrackHistoryStore, FakeHistoryStore())
    app.dependency_overrides[get_alerts] = lambda: cast(LiveAlertStore, FakeAlertStore())
    return TestClient(app)


# --------------------------------------------------------------------------
# The spec is complete and honest about what it offers
# --------------------------------------------------------------------------


def test_the_committed_spec_documents_every_public_route() -> None:
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert set(spec["paths"]) == {
        "/health",
        "/ready",
        "/v1/tracks",
        "/v1/alerts",
        "/v1/tracks/{track_id}/history",
    }


def test_the_spec_documents_the_503_readiness_response() -> None:
    """A consumer writing a health check needs to know 503 is expected.

    Readiness returning 503 is normal operation, not an error, and a spec that
    only listed 200 would lead someone to treat it as one.
    """
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    responses = spec["paths"]["/ready"]["get"]["responses"]
    assert "503" in responses, "readiness can return 503; the spec must say so"


def test_the_websocket_is_absent_from_the_spec_and_that_is_intentional() -> None:
    """OpenAPI 3.1 cannot describe a WebSocket.

    Pinned so its absence reads as a known limitation rather than an oversight.
    The frame format is `acp.services.api.stream.StreamFrame`.
    """
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert "/v1/stream" not in spec["paths"]


def test_the_spec_carries_the_safety_scope() -> None:
    """Anyone generating a client reads this before anything else."""
    spec = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    description = spec["info"]["description"].lower()
    assert "synthetic" in description
    assert "not certified" in description or "advisory" in description


# --------------------------------------------------------------------------
# The implementation obeys the spec
# --------------------------------------------------------------------------


def _fuzzable_app() -> object:
    """The application with fake stores, driven directly over ASGI.

    Schemathesis talks to the app itself rather than to a `TestClient`, which
    keeps this a test of the HTTP layer -- validation, status codes, response
    shapes -- with no infrastructure involved. The stores are fakes because
    storage correctness is the integration suite's job, not the contract's.
    """
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    app.dependency_overrides[get_live] = lambda: cast(LiveTrackStore, FakeLiveStore())
    app.dependency_overrides[get_history] = lambda: cast(TrackHistoryStore, FakeHistoryStore())
    app.dependency_overrides[get_alerts] = lambda: cast(LiveAlertStore, FakeAlertStore())
    # The broadcaster's background task and the store clients are only needed by
    # the lifespan, which schemathesis does not run.
    app.state.live = FakeLiveStore()
    app.state.alerts = FakeAlertStore()
    app.state.history = FakeHistoryStore()
    return app


#: Read from the running application rather than from the committed file,
#: because schemathesis needs an ASGI target to drive. That is not a loophole:
#: `scripts/contracts.py --check` fails the build if the two differ, so fuzzing
#: the live spec is fuzzing the committed one.
schema = schemathesis.openapi.from_asgi("/openapi.json", _fuzzable_app())


@schema.parametrize()
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_the_api_conforms_to_its_own_specification(case: object) -> None:
    """Fuzz every documented operation against the committed contract.

    Generates inputs from the spec -- including ones no hand-written test would
    think of -- and asserts every response carries a documented status code and
    matches its declared schema. An undocumented 500 on a plausible input fails.
    """
    case.call_and_validate()  # type: ignore[attr-defined]
