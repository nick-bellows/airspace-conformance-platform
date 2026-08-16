"""Tests for trace context propagation.

The property worth testing is the one the module exists for: **a trace survives
a Kafka message**. Producer and consumer share nothing but bytes in a header, so
if injection and extraction disagree the trace silently splits into two and the
timing question -- where did the seconds go between the report and the alert --
becomes unanswerable. That failure produces no error anywhere.

Everything here uses a locally constructed `TracerProvider` rather than the
global one. Setting a global provider is a one-shot, process-wide side effect;
a test that did it would change the behaviour of every test that ran after it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from acp.common import tracing
from acp.common.logging import trace_id_var
from acp.common.tracing import (
    TRACEPARENT_HEADER,
    TRACING_AVAILABLE,
    configure_tracing,
    consumer_span,
    current_trace_id,
    inject_headers,
    span,
)

pytestmark = pytest.mark.skipif(not TRACING_AVAILABLE, reason="requires the observability extra")


@pytest.fixture
def recording_tracer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Point the module at a real SDK tracer without touching global state."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_tracer", lambda: provider.get_tracer("test"))
    yield exporter
    provider.shutdown()


@pytest.fixture(autouse=True)
def _reset_trace_id() -> None:
    trace_id_var.set(None)


def test_configure_tracing_is_off_without_an_endpoint() -> None:
    """The normal local state: no collector running, and that is not an error."""
    assert configure_tracing("acp-test", "") is False
    assert configure_tracing("acp-test", None) is False


def test_span_records_a_span(recording_tracer: Any) -> None:
    with span("estimate", icao24="a1b2c3"):
        pass

    (recorded,) = recording_tracer.get_finished_spans()
    assert recorded.name == "estimate"
    assert recorded.attributes["icao24"] == "a1b2c3"


def test_inject_adds_traceparent_and_keeps_existing_headers(recording_tracer: Any) -> None:
    with span("publish"):
        headers = inject_headers([("x-acp-trace-id", b"abc")])

    names = {name for name, _ in headers}
    assert "x-acp-trace-id" in names
    assert TRACEPARENT_HEADER in names


def test_trace_survives_the_header_round_trip(recording_tracer: Any) -> None:
    """The whole point of the module, in one assertion.

    A span opened in the "producer", serialised to Kafka headers, and reopened
    in the "consumer" must land in the same trace. If it does not, the report
    and the alert it caused appear as two unrelated traces.
    """
    with span("produce"):
        headers = inject_headers([])
        produced_trace_id = current_trace_id()

    with consumer_span("consume tracks.updates.v1", headers):
        consumed_trace_id = current_trace_id()

    assert consumed_trace_id == produced_trace_id

    names = {recorded.name for recorded in recording_tracer.get_finished_spans()}
    assert names == {"produce", "consume tracks.updates.v1"}


def test_consumer_span_starts_a_fresh_trace_when_headers_are_absent(
    recording_tracer: Any,
) -> None:
    """A message from a producer with no tracing must still be traceable onward."""
    with consumer_span("consume", None):
        assert current_trace_id()

    (recorded,) = recording_tracer.get_finished_spans()
    assert recorded.name == "consume"


def test_consumer_span_ignores_an_unparseable_traceparent(recording_tracer: Any) -> None:
    """A corrupt header is a reason to start a new trace, never to drop a message."""
    with consumer_span("consume", [(TRACEPARENT_HEADER, b"not-a-traceparent")]):
        assert current_trace_id()


def test_a_span_can_link_to_causes_that_are_not_its_parent(recording_tracer: Any) -> None:
    """Links are how a timer-driven scan says what caused it.

    The conformance scan runs on a timer over the whole airspace picture, so it
    has no single parent: it is caused by every track update it acts on. The
    first version simply opened an unparented span, which left the report's
    trace ending at the consume and the alert in a separate, unrelated trace --
    exactly the interval the M5 notes claimed had been fixed.
    """
    from acp.common.tracing import current_span_context

    causes = []
    for icao24 in ("aaa111", "bbb222"):
        with span("consume", icao24=icao24):
            causes.append(current_span_context())

    with span("conflict-scan", links=causes, tracks=2):
        pass

    scan = next(s for s in recording_tracer.get_finished_spans() if s.name == "conflict-scan")
    assert len(scan.links) == 2
    linked = {link.context.trace_id for link in scan.links}
    assert linked == {c.trace_id for c in causes}
    assert scan.parent is None, "a scan has no honest parent; links are the whole point"


def test_links_are_ignored_when_they_are_absent(recording_tracer: Any) -> None:
    """A picture with no traced updates must still produce a usable span."""
    with span("conflict-scan", links=[None, None]):
        pass

    scan = next(s for s in recording_tracer.get_finished_spans() if s.name == "conflict-scan")
    assert not scan.links


def test_current_span_context_is_none_outside_a_span() -> None:
    from acp.common.tracing import current_span_context

    assert current_span_context() is None


def test_current_trace_id_falls_back_to_the_context_variable() -> None:
    """With no active span, the M1 correlation id is still what the logs carry."""
    trace_id_var.set("trace-from-kafka-header")
    assert current_trace_id() == "trace-from-kafka-header"


def test_current_trace_id_mints_one_when_there_is_nothing_at_all() -> None:
    minted = current_trace_id()
    assert len(minted) == 32
    assert minted != current_trace_id()


def test_everything_degrades_to_no_ops_without_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same principle as the trajectory model: run degraded, never refuse to start."""
    monkeypatch.setattr(tracing, "TRACING_AVAILABLE", False)

    assert configure_tracing("acp-test", "http://collector:4318/v1/traces") is False
    assert inject_headers([("x", b"y")]) == [("x", b"y")]
    with span("estimate"):
        pass
    with consumer_span("consume", [("traceparent", b"whatever")]):
        pass

    trace_id_var.set("fallback")
    assert current_trace_id() == "fallback"
