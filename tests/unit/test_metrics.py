"""Tests for the Prometheus metrics layer.

Two properties matter here and neither is about the numbers themselves.

**The disabled path must be complete.** A service without the `observability`
extra installed has to run. If one attribute is left off the no-op branch, the
failure is an `AttributeError` at whatever call site touches it first -- in a
worker, at three in the morning, in the environment that was already the
degraded one. So the test walks the declared attributes rather than a
hand-written list.

**The exposition output must actually contain the series.** A counter that is
incremented but never rendered is indistinguishable from no instrumentation.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest

from acp.common.metrics import (
    CONTENT_TYPE,
    FILTER_BUCKETS,
    METRICS_AVAILABLE,
    SCAN_BUCKETS,
    Metrics,
    _NoOpMetric,
    serve_metrics,
)


def _fresh() -> Metrics:
    """A Metrics with its own registry, so tests never see each other's counts."""
    if not METRICS_AVAILABLE:  # pragma: no cover - the no-extra CI job
        return Metrics()
    from prometheus_client import CollectorRegistry

    return Metrics(CollectorRegistry())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


pytestmark = pytest.mark.skipif(not METRICS_AVAILABLE, reason="requires the observability extra")


def test_every_declared_metric_exists_on_a_live_instance() -> None:
    metrics = _fresh()
    for name in Metrics._NAMES:
        assert hasattr(metrics, name), name


def test_every_declared_metric_exists_on_a_disabled_instance() -> None:
    """The whole point of `_NAMES`: the degraded path cannot be missing one.

    Built by hand rather than by monkeypatching the import, because what is
    under test is that the constructor's disabled branch installs a stand-in for
    every attribute the enabled branch creates.
    """
    disabled = Metrics.__new__(Metrics)
    disabled.enabled = False
    disabled.registry = None
    for name in Metrics._NAMES:
        setattr(disabled, name, _NoOpMetric())

    for name in Metrics._NAMES:
        # Any chain a call site might use has to be harmless.
        getattr(disabled, name).labels(service="track", topic="t", partition="0").inc()


def test_declared_annotations_and_names_agree() -> None:
    """Adding a metric without adding it to `_NAMES` is the failure to catch."""
    annotated = {
        name
        for name, annotation in Metrics.__annotations__.items()
        if annotation == "_Metric" and not name.startswith("_")
    }
    assert annotated == set(Metrics._NAMES)


def test_counters_appear_in_the_exposition_output() -> None:
    metrics = _fresh()
    metrics.reports_consumed.labels(service="track").inc()
    metrics.reports_consumed.labels(service="track").inc()

    body = metrics.render().decode()
    assert 'acp_reports_consumed_total{service="track"} 2.0' in body


def test_labelled_alert_counter_keeps_the_dimensions_apart() -> None:
    metrics = _fresh()
    metrics.alerts_published.labels(service="conformance", kind="CONFLICT", state="NEW").inc()
    metrics.alerts_published.labels(service="conformance", kind="CONFLICT", state="CLEARED").inc()

    body = metrics.render().decode()
    assert 'state="NEW"' in body
    assert 'state="CLEARED"' in body


def test_time_records_into_the_histogram() -> None:
    metrics = _fresh()
    with metrics.time(metrics.pipeline_latency, "track"):
        pass

    body = metrics.render().decode()
    assert 'acp_report_filter_seconds_count{service="track"} 1.0' in body


def test_time_records_even_when_the_block_raises() -> None:
    """A timing that only records on success hides exactly the slow failures."""
    metrics = _fresh()
    with pytest.raises(ValueError, match="boom"), metrics.time(metrics.scan_duration, "x"):
        raise ValueError("boom")

    assert 'acp_conflict_scan_seconds_count{service="x"} 1.0' in metrics.render().decode()


def test_bucket_ladders_are_sorted_and_bracket_their_budgets() -> None:
    """Two stages, two budgets, two ladders.

    They shared one ladder starting at 5 ms while the filter budget is 1 ms, so
    the histogram could not resolve the number it was supposed to enforce.
    `tests/unit/test_deployment.py` checks the values against
    `docs/latency-budget.md`; this checks the shape.
    """
    for ladder in (FILTER_BUCKETS, SCAN_BUCKETS):
        assert tuple(sorted(ladder)) == ladder
        assert len(set(ladder)) == len(ladder)
    assert min(FILTER_BUCKETS) < min(SCAN_BUCKETS), (
        "the filter ladder must resolve finer than the scan ladder; that is why it exists"
    )


def test_disabled_render_says_so_rather_than_returning_empty() -> None:
    """An empty scrape looks like a healthy system with no traffic. Say the truth."""
    disabled = Metrics.__new__(Metrics)
    disabled.enabled = False
    disabled.registry = None
    assert b"unavailable" in disabled.render()


def test_serve_metrics_exposes_a_scrapeable_endpoint() -> None:
    """The reason the workers are instrumented at all: something can reach them."""
    metrics = _fresh()
    metrics.live_tracks.labels(service="track").set(7)
    port = _free_port()

    assert serve_metrics(port, metrics) is True

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        body = response.read().decode()
    assert 'acp_live_tracks{service="track"} 7.0' in body


def test_serve_metrics_tolerates_a_failure_to_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing the scrape endpoint must not stop a service processing air traffic.

    The failure is forced rather than provoked by taking the port first. Binding
    an already-bound port raises on Linux but succeeds on Windows, because
    `ThreadingHTTPServer` sets `SO_REUSEADDR` and Windows lets that share a live
    listener. Reproducing the OS behaviour is not what this test is about --
    what matters is that `serve_metrics` reports the failure and returns rather
    than propagating it into a service's startup path.
    """
    import prometheus_client

    def refuse(*args: object, **kwargs: object) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(prometheus_client, "start_http_server", refuse)
    assert serve_metrics(_free_port(), _fresh()) is False


def test_serve_metrics_declines_when_metrics_are_disabled() -> None:
    disabled = Metrics.__new__(Metrics)
    disabled.enabled = False
    disabled.registry = None
    assert serve_metrics(_free_port(), disabled) is False


def test_content_type_is_the_one_prometheus_expects() -> None:
    assert CONTENT_TYPE.startswith("text/plain")
    assert "version=0.0.4" in CONTENT_TYPE
