"""Runtime configuration, read from the environment.

Every service reads the same settings object. Defaults point at the docker
compose stack in `deploy/compose.yml` so a clean clone runs without an env file;
Kubernetes overrides the same names through a ConfigMap.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings shared by all services."""

    model_config = SettingsConfigDict(env_prefix="ACP_", env_file=".env", extra="ignore")

    # Every field here is read by something. `service_name` and
    # `kafka_consumer_group` used to be here and were not: each service passes a
    # literal service name to `configure_logging` and takes its consumer group
    # from `--group-id`. A setting nothing reads is worse than a missing one --
    # someone sets `ACP_SERVICE_NAME=track`, nothing happens, and there is no
    # error to explain why. They were removed rather than wired up, because
    # neither had a call site that wanted them.

    log_level: str = "INFO"

    kafka_bootstrap_servers: str = "localhost:19092"
    # At-least-once delivery: offsets commit only after a message is handled, so
    # a crash replays rather than drops. Consumers must therefore be idempotent.
    kafka_auto_offset_reset: str = "earliest"

    postgres_dsn: str = "postgresql+asyncpg://acp:acp@localhost:5432/acp"
    redis_url: str = "redis://localhost:6379/0"

    #: Binds every interface *inside the container*, which is the only way a
    #: published port or a Kubernetes Service can reach the process at all.
    #: Exposure is decided one level up: compose publishes on 127.0.0.1 and
    #: Kubernetes fronts it with a ClusterIP Service. Both linters flag the
    #: literal -- ruff as S104, bandit as B104 -- and both are answered here
    #: rather than left to fire on every run until people stop reading them.
    api_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    api_port: int = 8000

    #: Extra browser origins permitted to open a WebSocket, comma-separated.
    #:
    #: Same-origin requests and non-browser clients (no `Origin` header at all)
    #: are always allowed; this is for the case where the display is served from
    #: a different host than the API.
    #:
    #: The check exists because **WebSockets do not respect CORS**. A browser
    #: will happily open one to any origin and attach the user's cookies, which
    #: is Cross-Site WebSocket Hijacking. It is not exploitable here today --
    #: there is no authentication, so a hostile page learns nothing `curl` could
    #: not already fetch -- but the mitigation belongs in place *before* auth
    #: is added rather than after, because afterwards it is a vulnerability
    #: rather than a gap.
    allowed_websocket_origins: str = ""

    #: OTLP HTTP endpoint for traces, e.g. http://jaeger:4318/v1/traces.
    #: Empty disables tracing, which is the normal state for a local run with no
    #: collector attached.
    otlp_endpoint: str = ""

    #: Port the three worker services expose `/metrics` on.
    #:
    #: One number for all three is correct under compose and Kubernetes, where
    #: each service has its own network namespace. Running two workers directly
    #: on one host makes the second fail to bind; that is logged and tolerated
    #: rather than fatal, and `ACP_METRICS_PORT` overrides it per process.
    #:
    #: 9100 is deliberately *not* used -- that is node_exporter's port, and
    #: colliding with it on a real host would be a confusing failure.
    metrics_port: int = Field(default=9464, gt=0, le=65535)

    # Separation standards applied by the conflict detector. En-route defaults;
    # real airspace varies these by class, altitude, and surveillance type.
    horizontal_separation_nm: float = Field(default=5.0, gt=0.0)
    vertical_separation_ft: float = Field(default=1000.0, gt=0.0)
    conflict_lookahead_s: float = Field(default=300.0, gt=0.0)


def load_settings() -> Settings:
    """Build a settings object from the environment."""
    return Settings()
