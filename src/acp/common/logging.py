"""Structured JSON logging.

One line of JSON per event, on stdout, because that is what a container
orchestrator collects. ``trace_id`` is carried on the record so a single
surveillance report can be followed across all four services; the Kafka
wrappers propagate it as a message header.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

#: Correlation id for the message currently being handled. Set by the Kafka
#: consumer loop and the API middleware; read by the formatter.
trace_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id = trace_id_var.get()
        if trace_id is not None:
            payload["trace_id"] = trace_id
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        # Anything passed via `extra=` lands in __dict__ and is worth keeping.
        payload.update(
            {
                k: v
                for k, v in record.__dict__.items()
                if k not in _RESERVED and not k.startswith("_")
            }
        )
        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Install the JSON formatter as the only root handler.

    Safe to call more than once; existing handlers are replaced so a re-import
    cannot produce duplicate lines.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
