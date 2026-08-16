"""The whole stack, running under docker compose.

Every other suite tests a piece. This one drives the system the way the demo
does -- `docker compose up`, a scripted scenario, wait -- and asserts the
outcome a user would see. It is the only test that would catch a broken compose
file, a wrong environment variable, a service that cannot reach another, or an
image that builds but does not run.

It is slow (minutes, including a build) and it is skipped without Docker, so it
runs in CI as its own job rather than inside the fast gate.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose.yml"

pytestmark = pytest.mark.e2e

if shutil.which("docker") is None:
    pytest.skip("docker is not on PATH", allow_module_level=True)


def compose(*args: str, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        # Explicit UTF-8. Without it Python decodes with the platform codec,
        # which on Windows is cp1252 and cannot represent the box-drawing
        # characters Redpanda's banner emits -- so reading the logs raised
        # UnicodeDecodeError and took the test with it.
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )


def _docker_running() -> bool:
    try:
        return compose("version", check=False, timeout=60).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


if not _docker_running():
    pytest.skip("docker compose is not usable", allow_module_level=True)


def api_get(path: str, *, timeout_s: float = 10.0) -> Any:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=timeout_s) as response:
        return json.loads(response.read())


def wait_until(predicate: Any, *, timeout_s: float, description: str) -> Any:
    """Poll until the predicate returns something truthy.

    Sleeping a fixed amount is how e2e suites become flaky in both directions:
    too short and it fails on a slow machine, too long and every run pays for
    the worst case. The failure message names what was being waited for.
    """
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as error:  # noqa: BLE001 - the stack is still coming up
            last_error = error
        time.sleep(2.0)
    raise AssertionError(
        f"timed out after {timeout_s}s waiting for {description}"
        + (f" (last error: {last_error})" if last_error else "")
    )


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """The full stack, on the head-on conflict scenario."""
    environment = dict(os.environ, ACP_SCENARIO="scenarios/head-on-conflict.yaml")
    subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    # `ACP_E2E_PREBUILT=1` means "the `acp:dev` image is already loaded; use it".
    # CI sets it so this suite exercises the *same* image artefact that the
    # security scan, the kind cluster, and the publish step use. Rebuilding here
    # would produce a fourth independent build, which is exactly the gap a
    # review found in the release path: unpinned dependency ranges and a mutable
    # base tag mean two builds of one commit are not guaranteed to be the same
    # image. Locally the variable is unset and compose builds as before.
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d"]
    if not os.environ.get("ACP_E2E_PREBUILT"):
        command.append("--build")
    build = subprocess.run(  # noqa: S603
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=1800,
    )
    if build.returncode != 0:
        pytest.fail(f"stack failed to start:\n{build.stdout}\n{build.stderr}")

    try:
        wait_until(
            lambda: api_get("/ready").get("ready"),
            timeout_s=180,
            description="the API to report ready",
        )
        yield
    finally:
        logs = compose("logs", check=False, timeout=120).stdout
        errors = [line for line in logs.splitlines() if '"level":"ERROR"' in line]
        compose("down", "-v", check=False, timeout=300)
        # Reported after teardown so a failure here never leaves containers
        # running, but still fails the test -- an ERROR anywhere in a clean run
        # is a defect even if every assertion passed.
        assert not errors, "services logged errors:\n" + "\n".join(errors[:10])


def test_the_api_reports_every_dependency_healthy(stack: None) -> None:
    ready = api_get("/ready")
    assert ready == {"ready": True, "redis": True, "postgres": True}


def test_aircraft_appear_on_the_live_picture(stack: None) -> None:
    """Feed to Kafka to tracker to Redis to API, with nothing stubbed."""
    payload = wait_until(
        lambda: api_get("/v1/tracks") if api_get("/v1/tracks")["count"] >= 4 else None,
        timeout_s=120,
        description="four aircraft to reach the live picture",
    )
    callsigns = {track["callsign"] for track in payload["tracks"]}
    assert callsigns == {"ACP101", "ACP202", "ACP303", "ACP404"}


def test_track_history_reaches_postgres(stack: None) -> None:
    # The wait predicate has to be the same condition as the assertion. It was
    # not: it waited for the endpoint to *respond*, which happens as soon as one
    # row exists, and then asserted on ten. That passed for as long as the
    # stack happened to take longer to come up than the tracker took to write
    # ten rows, and failed the first time the ordering went the other way --
    # a flake that would have been blamed on CI rather than on the test.
    history = wait_until(
        lambda: (
            found
            if (found := api_get("/v1/tracks/trk-a1b2c3/history?limit=50"))["count"] > 10
            else None
        ),
        timeout_s=120,
        description="at least ten history points to be persisted",
    )
    assert history["count"] > 10
    observed = [point["observed_at"] for point in history["points"]]
    assert observed == sorted(observed), "history came back out of order"


def test_the_conflict_is_detected_before_separation_is_lost(stack: None) -> None:
    """The whole point of the system, end to end.

    The scenario stages a co-altitude head-on convergence. An alert naming both
    aircraft must appear well before they actually lose separation.
    """
    alerts = wait_until(
        lambda: (
            [
                alert
                for alert in api_get("/v1/alerts")["alerts"]
                if alert["kind"] == "predicted_conflict"
            ]
            or None
        ),
        timeout_s=240,
        description="a predicted conflict alert",
    )
    alert = alerts[0]
    assert set(alert["track_ids"]) == {"trk-a1b2c3", "trk-d4e5f6"}
    assert alert["conflict"]["min_horizontal_sep_nm"] < 5.0
    assert alert["conflict"]["min_vertical_sep_ft"] < 1000.0
    # Raised with time to spare, not after the fact.
    assert alert["conflict"]["time_to_cpa_s"] > 30.0
    assert alert["reason_codes"]


def test_the_aircraft_flying_4000_feet_above_is_not_alerted_on(stack: None) -> None:
    """The negative case the scenario exists to protect, through the real stack.

    ACP303 is 4000 ft above the conflicting pair and must never appear in an
    alert. Note what this does *not* prove: the feed runs in real time and the
    suite finishes shortly after the conflict alert at T+2:38, while ACP303 does
    not reach the pair until T+7:58 -- so at this point it is still tens of
    miles away and the altitude check has not yet been put under pressure.
    Waiting eight minutes to find out would cost more CI time than the
    guarantee is worth here.

    `tests/unit/test_scenarios.py` runs the encounter to completion, where
    ACP303 passes within 0.2 NM, and is where that guarantee actually lives.
    What this test adds is that the whole pipeline -- broker, filter, detector,
    API -- agrees, rather than just the detector in isolation.
    """
    alerts = api_get("/v1/alerts")["alerts"]
    involved = {track for alert in alerts for track in alert["track_ids"]}
    assert "trk-0a1b2c" not in involved


def test_the_live_stream_pushes_the_same_picture(stack: None) -> None:
    """The WebSocket carries tracks and alerts in one frame."""
    from websockets.sync.client import connect

    with connect("ws://127.0.0.1:8000/v1/stream", open_timeout=30) as socket:
        frame = json.loads(socket.recv(timeout=30))

    assert frame["tracks"], "the stream sent an empty picture"
    assert "alerts" in frame
    assert {track["callsign"] for track in frame["tracks"]} <= {
        "ACP101",
        "ACP202",
        "ACP303",
        "ACP404",
    }


def test_the_display_is_served(stack: None) -> None:
    import re
    import urllib.request

    with urllib.request.urlopen("http://127.0.0.1:8000/", timeout=10) as response:
        body = response.read().decode()
    # Whitespace-normalised: the disclaimer wraps across lines in the source,
    # and asserting on the literal string made this fail for formatting rather
    # than for the thing it cares about.
    flattened = re.sub(r"\s+", " ", body).lower()
    assert "airspace conformance platform" in flattened
    assert "not an air traffic control system" in flattened


def test_the_conformance_service_loaded_its_model(stack: None) -> None:
    """A silent fallback to physics in a real deployment would go unnoticed
    for a long time, so the startup line states it explicitly."""
    logs = compose("logs", "conformance", check=False, timeout=120).stdout
    startup = [line for line in logs.splitlines() if "conformance monitoring enabled" in line]
    assert startup, "the conformance service never logged its startup line"
    assert '"model_loaded":true' in startup[-1].replace(" ", "")
