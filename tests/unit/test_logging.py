"""Tests for structured logging.

Log lines are an operational interface, not decoration: the e2e tests and any
future dashboard parse them. A line that is not valid JSON, or that silently
drops the correlation id, breaks that interface.
"""

from __future__ import annotations

import json
import logging

import pytest

from acp.common.logging import JsonFormatter, configure_logging, trace_id_var


@pytest.fixture(autouse=True)
def _reset_trace_id() -> None:
    trace_id_var.set(None)


def _emit(caplog_free_name: str = "test") -> logging.LogRecord:
    logger = logging.getLogger(caplog_free_name)
    return logger.makeRecord(caplog_free_name, logging.INFO, "f.py", 10, "hello", None, None)


def test_every_line_is_valid_json() -> None:
    payload = json.loads(JsonFormatter("feed").format(_emit()))
    assert payload["message"] == "hello"
    assert payload["level"] == "INFO"
    assert payload["service"] == "feed"
    assert payload["ts"].endswith("+00:00")


def test_trace_id_is_included_when_one_is_set() -> None:
    """A report must be followable across all four services by this field."""
    trace_id_var.set("trace-abc")
    payload = json.loads(JsonFormatter("track").format(_emit()))
    assert payload["trace_id"] == "trace-abc"


def test_trace_id_is_omitted_rather_than_null_when_unset() -> None:
    assert "trace_id" not in json.loads(JsonFormatter("track").format(_emit()))


def test_extra_fields_survive_into_the_payload() -> None:
    record = _emit()
    record.icao24 = "a1b2c3"  # what `logger.info(..., extra={"icao24": ...})` produces
    record.lag_ms = 42
    payload = json.loads(JsonFormatter("track").format(record))
    assert payload["icao24"] == "a1b2c3"
    assert payload["lag_ms"] == 42


def test_exceptions_are_rendered_as_a_string_field() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _emit()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter("api").format(record))
    assert "ValueError: boom" in payload["exception"]


def test_configure_logging_replaces_handlers_instead_of_stacking_them() -> None:
    """Called twice (import cycles, reloads) must not double every log line."""
    configure_logging("feed")
    configure_logging("feed")
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger().handlers.clear()


def test_configure_logging_applies_the_requested_level() -> None:
    configure_logging("feed", level="warning")
    assert logging.getLogger().level == logging.WARNING
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.WARNING)
