"""Distributed tracing across Kafka.

## The problem this solves

No stack trace crosses a broker. When an alert looks wrong, the question is
"which surveillance report caused this, and what happened to it in between" --
and the answer is spread across three processes that share nothing but topics.

M1 carried a `trace_id` header for exactly this reason, and it worked: one grep
recovers every log line for one report. What it could not do is show *timing* --
where the seconds went between the report arriving and the alert appearing.

This module upgrades that to **W3C trace context**, which is the standard format
every tracing backend understands. The `traceparent` header travels on the same
Kafka messages, and spans in the feed, tracker, and conformance service link
into one trace with real durations.

## Why manual instrumentation across the broker

Auto-instrumentation handles HTTP because a request and its response are one
exchange. A Kafka message is not: the producer finishes long before the consumer
starts, and the two are joined only by what the message carries. So the context
is injected into headers on publish and extracted on consume, explicitly.

## Degrading without the extra

Identical principle to metrics and the trajectory model: a service without the
`observability` extra installed starts and runs. Every function here becomes a
no-op, `trace_id_var` keeps working as it did in M1, and the logs remain
correlatable even when nothing is collecting spans.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from acp.common.logging import get_logger, trace_id_var

_log = get_logger(__name__)

#: Kafka header carrying W3C trace context. The name is the standard one, so a
#: consumer written in another language and another framework can join the trace
#: without knowing anything about this codebase.
TRACEPARENT_HEADER = "traceparent"

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract, inject
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    TRACING_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the no-extra CI job
    TRACING_AVAILABLE = False

_configured = False


def configure_tracing(service_name: str, endpoint: str | None) -> bool:
    """Set up tracing for this process. Returns whether it is active.

    Safe to call when the extra is missing or no endpoint is configured; both
    are ordinary situations -- a developer running the stack locally has no
    collector -- and neither should stop a service.
    """
    global _configured
    if not TRACING_AVAILABLE:
        _log.info("opentelemetry is not installed; tracing disabled")
        return False
    if not endpoint:
        _log.info("no OTLP endpoint configured; tracing disabled")
        return False
    if _configured:
        return True

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _configured = True
    _log.info("tracing enabled", extra={"endpoint": endpoint, "service": service_name})
    return True


def _tracer() -> Any:
    return trace.get_tracer("acp") if TRACING_AVAILABLE else None


def current_span_context() -> Any | None:
    """The active span's context, for linking to later, or None.

    Kept opaque on purpose: callers store it and hand it back to `span(links=)`
    without ever needing the OpenTelemetry types, which is what lets every
    call site stay import-safe when the extra is absent.
    """
    if not TRACING_AVAILABLE:
        return None
    context = trace.get_current_span().get_span_context()
    return context if context.is_valid else None


@contextmanager
def span(name: str, *, links: Sequence[Any] | None = None, **attributes: Any) -> Iterator[None]:
    """Record a span, or do nothing at all if tracing is unavailable.

    `links` attaches *causes* that are not parents. The distinction matters for
    anything driven by a timer over accumulated state: the conformance scan is
    caused by every track update in the airspace picture, not by one of them, so
    there is no honest parent to nest it under. Links say "these contributed"
    without claiming a call stack that never existed.
    """
    tracer = _tracer()
    if tracer is None:
        yield
        return
    span_links = [trace.Link(context) for context in (links or []) if context is not None]
    with tracer.start_as_current_span(name, links=span_links or None) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield


def inject_headers(headers: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Add trace context to outgoing Kafka headers.

    The producer's context is serialised into `traceparent`; the consumer
    reconstructs it. This is the whole mechanism by which a trace survives a
    broker.
    """
    if not TRACING_AVAILABLE:
        return headers
    carrier: dict[str, str] = {}
    inject(carrier)
    return headers + [(key, value.encode()) for key, value in carrier.items()]


@contextmanager
def consumer_span(
    name: str, headers: Sequence[tuple[str, bytes]] | None, **attributes: Any
) -> Iterator[None]:
    """Continue the producer's trace while handling a consumed message."""
    if not TRACING_AVAILABLE:
        yield
        return

    carrier = {key: value.decode(errors="replace") for key, value in (headers or ())}
    context = extract(carrier)
    tracer = _tracer()
    with tracer.start_as_current_span(name, context=context) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        yield


def current_trace_id() -> str:
    """A correlation id for log lines.

    Prefers the active OpenTelemetry trace id, so a log line and a span can be
    matched up in a backend. Falls back to the ambient context variable -- which
    is what M1 used and what still works with no tracing installed -- and mints
    one only if there is nothing at all.
    """
    if TRACING_AVAILABLE:
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return format(context.trace_id, "032x")
    return trace_id_var.get() or uuid.uuid4().hex
