"""Tests for environment-driven configuration.

The defaults are load-bearing: a clean clone must run against `deploy/compose.yml`
without an env file, and Kubernetes must be able to override the same names
through a ConfigMap. Both properties are asserted here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from acp.common.config import Settings, load_settings


def test_defaults_point_at_the_compose_stack() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.kafka_bootstrap_servers == "localhost:19092"
    assert "localhost:5432" in settings.postgres_dsn
    assert settings.redis_url.startswith("redis://localhost:6379")


def test_separation_standards_default_to_en_route_values() -> None:
    """5 NM laterally and 1000 ft vertically; both must be overridable."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.horizontal_separation_nm == 5.0
    assert settings.vertical_separation_ft == 1000.0
    assert settings.conflict_lookahead_s == 300.0


def test_environment_overrides_use_the_acp_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACP_KAFKA_BOOTSTRAP_SERVERS", "redpanda:9092")
    monkeypatch.setenv("ACP_HORIZONTAL_SEPARATION_NM", "3")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.kafka_bootstrap_servers == "redpanda:9092"
    assert settings.horizontal_separation_nm == 3.0


def test_unprefixed_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray `LOG_LEVEL` in a shared container must not reconfigure us."""
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert Settings(_env_file=None).log_level == "INFO"  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ACP_HORIZONTAL_SEPARATION_NM", "0"),
        ("ACP_HORIZONTAL_SEPARATION_NM", "-5"),
        ("ACP_VERTICAL_SEPARATION_FT", "0"),
        ("ACP_CONFLICT_LOOKAHEAD_S", "-1"),
    ],
)
def test_nonsensical_separation_standards_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """Fail loudly on boot rather than silently never raising a conflict alert."""
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_load_settings_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACP_LOG_LEVEL", "DEBUG")
    assert load_settings().log_level == "DEBUG"


def test_the_metrics_port_must_be_a_real_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd port fails at startup rather than silently never being scraped."""
    monkeypatch.setenv("ACP_METRICS_PORT", "70000")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_tracing_is_off_by_default() -> None:
    """No collector configured is the ordinary local state, not a misconfiguration."""
    assert Settings(_env_file=None).otlp_endpoint == ""  # type: ignore[call-arg]
