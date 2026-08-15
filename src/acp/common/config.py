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

    service_name: str = "acp"
    log_level: str = "INFO"

    kafka_bootstrap_servers: str = "localhost:19092"
    kafka_consumer_group: str = "acp"
    # At-least-once delivery: offsets commit only after a message is handled, so
    # a crash replays rather than drops. Consumers must therefore be idempotent.
    kafka_auto_offset_reset: str = "earliest"

    postgres_dsn: str = "postgresql+asyncpg://acp:acp@localhost:5432/acp"
    redis_url: str = "redis://localhost:6379/0"

    api_host: str = "0.0.0.0"  # noqa: S104 - bound to localhost by compose/k8s, not here
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

    # Separation standards applied by the conflict detector. En-route defaults;
    # real airspace varies these by class, altitude, and surveillance type.
    horizontal_separation_nm: float = Field(default=5.0, gt=0.0)
    vertical_separation_ft: float = Field(default=1000.0, gt=0.0)
    conflict_lookahead_s: float = Field(default=300.0, gt=0.0)


def load_settings() -> Settings:
    """Build a settings object from the environment."""
    return Settings()
