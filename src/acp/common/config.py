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

    # Separation standards applied by the conflict detector. En-route defaults;
    # real airspace varies these by class, altitude, and surveillance type.
    horizontal_separation_nm: float = Field(default=5.0, gt=0.0)
    vertical_separation_ft: float = Field(default=1000.0, gt=0.0)
    conflict_lookahead_s: float = Field(default=300.0, gt=0.0)


def load_settings() -> Settings:
    """Build a settings object from the environment."""
    return Settings()
