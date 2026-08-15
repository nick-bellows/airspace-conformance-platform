"""API service: the read side and the display.

It never consumes Kafka and never writes anything. The live picture comes from
Redis, history from Postgres. That is what keeps a display refresh off the hot
path -- rendering the airspace does not touch the pipeline that is estimating
it.

Health endpoints are split on purpose, because Kubernetes treats them
differently:

* ``/health`` is liveness. It answers "is this process wedged?" and must not
  depend on anything external -- a failing dependency would otherwise get the
  pod killed and restarted in a loop, which fixes nothing.
* ``/ready`` is readiness. It answers "can I serve traffic?" and therefore does
  check Redis and Postgres. A failure removes the pod from the load balancer
  and leaves it running.

Getting these the wrong way round is the classic Kubernetes outage.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from acp.common.config import Settings, load_settings
from acp.common.contracts import TrackUpdate
from acp.common.logging import configure_logging, get_logger
from acp.storage.stores import LiveTrackStore, TrackHistoryStore

_log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

#: A track older than this is not shown. Chosen well above the 1 Hz report rate
#: but below the tracker's 30 s termination timeout, so the display drops a
#: stale aircraft slightly before the tracker formally gives up on it. Showing
#: an aircraft that is no longer there is the worse error.
LIVE_WINDOW_S = 20.0


class HealthResponse(BaseModel):
    """Liveness: this process is running."""

    status: str = "ok"
    service: str = "api"


class ReadyResponse(BaseModel):
    """Readiness: dependencies are reachable."""

    ready: bool
    redis: bool
    postgres: bool


class TracksResponse(BaseModel):
    """The current airspace picture."""

    generated_at: datetime
    live_window_s: float = LIVE_WINDOW_S
    count: int
    tracks: list[TrackUpdate]


class TrackPoint(BaseModel):
    """One historical position."""

    observed_at: datetime
    lat: float
    lon: float
    altitude_ft: float
    ground_speed_kt: float
    track_deg: float
    vertical_rate_fpm: float
    position_uncertainty_m: float


class HistoryResponse(BaseModel):
    """Where a track has been."""

    track_id: str
    count: int
    points: list[TrackPoint]


def get_live(request: Request) -> LiveTrackStore:
    store: LiveTrackStore = request.app.state.live
    return store


def get_history(request: Request) -> TrackHistoryStore:
    store: TrackHistoryStore = request.app.state.history
    return store


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application. Injectable settings keep it testable."""
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = resolved
        app.state.live = LiveTrackStore.from_url(resolved.redis_url)
        app.state.history = TrackHistoryStore.from_dsn(resolved.postgres_dsn)
        _log.info("api ready", extra={"port": resolved.api_port})
        try:
            yield
        finally:
            await app.state.live.close()
            await app.state.history.dispose()

    app = FastAPI(
        title="Airspace Conformance Platform",
        version="0.1.0",
        summary="Advisory airspace monitoring over simulated traffic. Not an ATC system.",
        description=(
            "Read-only view of the live airspace picture and track history.\n\n"
            "**All data is synthetic.** See `docs/safety-notes.md` in the repository: "
            "this system is advisory only, is not certified, and must never be used "
            "for actual air traffic control."
        ),
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, tags=["operations"])
    async def health() -> HealthResponse:
        """Liveness. Deliberately checks nothing external."""
        return HealthResponse()

    @app.get("/ready", response_model=ReadyResponse, tags=["operations"])
    async def ready(
        live: Annotated[LiveTrackStore, Depends(get_live)],
        history: Annotated[TrackHistoryStore, Depends(get_history)],
    ) -> JSONResponse:
        """Readiness. Reports each dependency separately so a failure names itself."""
        redis_ok = await live.ping()
        postgres_ok = await history.ping()
        body = ReadyResponse(ready=redis_ok and postgres_ok, redis=redis_ok, postgres=postgres_ok)
        return JSONResponse(
            status_code=200 if body.ready else 503,
            content=body.model_dump(),
        )

    @app.get("/v1/tracks", response_model=TracksResponse, tags=["airspace"])
    async def tracks(
        live: Annotated[LiveTrackStore, Depends(get_live)],
        window_s: Annotated[float, Query(gt=0.0, le=600.0)] = LIVE_WINDOW_S,
    ) -> TracksResponse:
        """Every track seen within the last `window_s` seconds."""
        now = datetime.now(UTC)
        updates = await live.live(since=now - timedelta(seconds=window_s))
        updates.sort(key=lambda u: u.callsign or u.icao24)
        return TracksResponse(
            generated_at=now, live_window_s=window_s, count=len(updates), tracks=updates
        )

    @app.get("/v1/tracks/{track_id}/history", response_model=HistoryResponse, tags=["airspace"])
    async def history_for(
        track_id: str,
        history: Annotated[TrackHistoryStore, Depends(get_history)],
        limit: Annotated[int, Field(gt=0, le=5000)] = 600,
    ) -> HistoryResponse:
        """Recent positions for one track, oldest first."""
        rows: list[dict[str, Any]] = await history.history(track_id, limit=limit)
        if not rows:
            raise HTTPException(status_code=404, detail=f"no history for track {track_id!r}")
        return HistoryResponse(
            track_id=track_id,
            count=len(rows),
            points=[TrackPoint.model_validate(row) for row in rows],
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def main() -> int:
    """Run the service under uvicorn."""
    import uvicorn

    settings = load_settings()
    configure_logging("api", settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # our JSON formatter owns stdout
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
