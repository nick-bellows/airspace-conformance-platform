"""Prometheus metrics.

## What is measured, and why these and not others

Four questions an operator actually asks at three in the morning, and the metric
that answers each:

* *Is data flowing?* -- counters on reports, track updates, and alerts.
* *Is it keeping up?* -- a histogram of how long a report takes end to end, and
  consumer lag. Lag is the single most useful number in a Kafka system: it says
  whether the backlog is growing, which is the difference between "slow" and
  "failing".
* *Is the picture believable?* -- gauges for live tracks and active alerts. A
  track count that drops to zero while the feed is running is a visible failure
  that no error log would report.
* *Is the clever part working?* -- whether the trajectory model loaded, and how
  often predictions fall back to physics. A silent, permanent fallback to dead
  reckoning is exactly the kind of degradation that goes unnoticed for months.

## Why this module is import-safe without the extra

`prometheus_client` lives in the `observability` extra. A service without it
installed must still start -- the same principle as the trajectory model
degrading to physics rather than refusing to boot. Absent the library, every
metric becomes a no-op and the service logs once that it is running blind.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from acp.common.logging import get_logger

_log = get_logger(__name__)


class _Metric(Protocol):
    """The slice of the prometheus_client API this module uses."""

    def labels(self, *args: str, **kwargs: str) -> Any: ...


class _NoOpMetric:
    """Stands in for a metric when prometheus_client is not installed.

    Every method returns self so any chain of calls is harmless. The service
    runs, it simply cannot be scraped -- which is a worse operational position
    but a much better one than refusing to start.
    """

    def labels(self, *args: str, **kwargs: str) -> _NoOpMetric:
        return self

    def inc(self, amount: float = 1.0) -> None:
        return None

    def dec(self, amount: float = 1.0) -> None:
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, value: float) -> None:
        return None


try:
    from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
    from prometheus_client import generate_latest as _generate_latest

    METRICS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the no-extra CI job
    METRICS_AVAILABLE = False


#: Buckets for the end-to-end latency histogram, in seconds.
#:
#: Chosen around the budget in `docs/latency-budget.md` rather than from a
#: default ladder: the interesting region is tens to hundreds of milliseconds,
#: and the 1 s bucket is the one that matters -- past it the system is no longer
#: keeping up with a 1 Hz report rate.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metrics:
    """The platform's metrics, or no-ops if the extra is not installed.

    Every attribute is declared as `_Metric` rather than as its concrete
    prometheus_client type. That is the point of the protocol: a call site does
    the same thing whether it is holding a real `Counter` or a no-op, and the
    type checker enforces that only the narrow slice of the API both support is
    ever used.
    """

    #: `CollectorRegistry` when metrics are live, `None` otherwise. Untyped
    #: because the class only exists when the extra is installed, and a
    #: conditional import cannot be used in an annotation under strict mode.
    registry: Any

    reports_consumed: _Metric
    messages_published: _Metric
    track_updates_published: _Metric
    alerts_published: _Metric
    messages_discarded: _Metric
    live_tracks: _Metric
    active_alerts: _Metric
    consumer_lag: _Metric
    pipeline_latency: _Metric
    scan_duration: _Metric
    model_loaded: _Metric
    predictions: _Metric

    #: Every attribute above that holds a metric. Used to install the no-ops in
    #: one place, so adding a metric cannot leave the disabled path with a
    #: missing attribute and an AttributeError at the first call site.
    _NAMES = (
        "reports_consumed",
        "messages_published",
        "track_updates_published",
        "alerts_published",
        "messages_discarded",
        "live_tracks",
        "active_alerts",
        "consumer_lag",
        "pipeline_latency",
        "scan_duration",
        "model_loaded",
        "predictions",
    )

    def __init__(self, registry: Any | None = None) -> None:
        self.enabled = METRICS_AVAILABLE
        if not METRICS_AVAILABLE:
            _log.warning(
                "prometheus_client is not installed; running without metrics. "
                "Install the 'observability' extra to enable /metrics."
            )
            self.registry = None
            for name in self._NAMES:
                setattr(self, name, _NoOpMetric())
            return

        self.registry = registry if registry is not None else CollectorRegistry()

        self.reports_consumed = Counter(
            "acp_reports_consumed_total",
            "Surveillance reports consumed by the tracker.",
            ["service"],
            registry=self.registry,
        )
        self.messages_published = Counter(
            "acp_messages_published_total",
            "Messages written to Kafka, counted in the publisher itself.",
            ["service", "topic"],
            registry=self.registry,
        )
        self.track_updates_published = Counter(
            "acp_track_updates_published_total",
            "Track state estimates published.",
            ["service"],
            registry=self.registry,
        )
        self.alerts_published = Counter(
            "acp_alerts_published_total",
            "Alert state changes published.",
            ["service", "kind", "state"],
            registry=self.registry,
        )
        self.messages_discarded = Counter(
            "acp_messages_discarded_total",
            "Messages skipped because they failed contract validation.",
            ["service", "topic"],
            registry=self.registry,
        )
        self.live_tracks = Gauge(
            "acp_live_tracks",
            "Aircraft currently held in this service's picture.",
            ["service"],
            registry=self.registry,
        )
        self.active_alerts = Gauge(
            "acp_active_alerts",
            "Alerts currently believed to be live.",
            ["service"],
            registry=self.registry,
        )
        self.consumer_lag = Gauge(
            "acp_consumer_lag_messages",
            "Messages behind the head of the partition. Growing lag means the "
            "backlog is building, which is the difference between slow and failing.",
            ["service", "topic", "partition"],
            registry=self.registry,
        )
        self.pipeline_latency = Histogram(
            "acp_report_processing_seconds",
            "Time to process one surveillance report into a track update.",
            ["service"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.scan_duration = Histogram(
            "acp_conflict_scan_seconds",
            "Time to scan the whole airspace picture for conflicts.",
            ["service"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.model_loaded = Gauge(
            "acp_trajectory_model_loaded",
            "1 if a trained trajectory model is in use, 0 if predictions have "
            "fallen back to dead reckoning.",
            ["service"],
            registry=self.registry,
        )
        self.predictions = Counter(
            "acp_trajectory_predictions_total",
            "Trajectory predictions, by which path produced them.",
            ["service", "source"],
            registry=self.registry,
        )

    @contextmanager
    def time(self, metric: _Metric, service: str) -> Iterator[None]:
        """Time a block into a histogram.

        Uses `perf_counter` rather than the library's own timer so the call site
        reads the same whether or not metrics are installed.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            metric.labels(service=service).observe(time.perf_counter() - started)

    def render(self) -> bytes:
        """The Prometheus exposition payload for a scrape."""
        if not self.enabled or self.registry is None:
            return b"# metrics unavailable: prometheus_client is not installed\n"
        return bytes(_generate_latest(self.registry))


#: One shared instance. Metrics are process-global by nature -- a scrape returns
#: the whole process -- so threading a registry through every constructor would
#: add ceremony without buying isolation. Tests that need isolation build their
#: own `Metrics` with a fresh registry.
METRICS = Metrics()

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def serve_metrics(port: int, metrics: Metrics | None = None) -> bool:
    """Expose `/metrics` on a background thread. Returns whether it started.

    The API already serves HTTP and exposes the endpoint on its own port. The
    three worker services do not: they consume Kafka and publish to it, and
    have no HTTP surface at all.

    Instrumenting them and then having no way to scrape it would be worse than
    not instrumenting them -- the counters would look like observability while
    being unreachable. A dedicated port on a background thread is the standard
    answer for exactly this shape of process, and it costs one thread and no
    architectural change.

    Failing to bind is logged and tolerated. A metrics endpoint is not worth
    refusing to process air traffic over.

    The listener binds all interfaces, which it has to: Prometheus scrapes it
    from a different container, so binding loopback would make it unreachable
    by the only thing that wants it. Exposure is controlled one level up --
    compose does not publish the port to the host and Kubernetes does not put a
    Service in front of it, and a test fails if either changes.
    """
    target = metrics or METRICS
    if not target.enabled or target.registry is None:
        return False
    try:
        from prometheus_client import start_http_server

        start_http_server(port, registry=target.registry)
    except OSError:
        _log.warning(
            "could not bind the metrics port; running without a scrape endpoint",
            extra={"port": port},
        )
        return False
    _log.info("metrics endpoint listening", extra={"port": port})
    return True
