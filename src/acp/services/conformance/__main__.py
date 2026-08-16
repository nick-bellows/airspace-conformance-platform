"""Entry point for the conformance service.

acp-conformance
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from acp.common.config import load_settings
from acp.common.contracts import TOPIC_TRACK_UPDATES, TrackUpdate
from acp.common.logging import configure_logging, get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.common.metrics import METRICS, serve_metrics
from acp.common.tracing import configure_tracing
from acp.ml.predictor import DEFAULT_MODELS_DIR, TrajectoryPredictor
from acp.services.conformance.monitor import DEFAULT_HORIZON_S, ConformanceMonitor
from acp.services.conformance.runner import DEFAULT_SCAN_INTERVAL_S, ConformanceRunner
from acp.services.conformance.separation import SeparationMonitor
from acp.storage.stores import LiveAlertStore

_log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acp-conformance", description=__doc__)
    parser.add_argument(
        "--group-id",
        default="conformance-service",
        help="Kafka consumer group",
    )
    parser.add_argument(
        "--scan-interval-s",
        type=float,
        default=DEFAULT_SCAN_INTERVAL_S,
        help="how often to scan the airspace picture for conflicts",
    )
    parser.add_argument(
        "--conformance-horizon-s",
        type=float,
        default=DEFAULT_HORIZON_S,
        help="how far ahead trajectories are predicted before being checked",
    )
    parser.add_argument(
        "--no-conformance",
        action="store_true",
        help="disable trajectory conformance monitoring; conflict detection is unaffected",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=DEFAULT_MODELS_DIR,
        help="where to look for the trained residual model",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    monitor = SeparationMonitor(
        horizontal_nm=settings.horizontal_separation_nm,
        vertical_ft=settings.vertical_separation_ft,
        lookahead_s=settings.conflict_lookahead_s,
    )
    alert_store = LiveAlertStore.from_url(settings.redis_url)

    conformance = None
    if not args.no_conformance:
        predictor = TrajectoryPredictor(args.conformance_horizon_s, models_dir=args.models_dir)
        conformance = ConformanceMonitor(predictor, horizon_s=args.conformance_horizon_s)
        _log.info(
            "conformance monitoring enabled",
            extra={
                "horizon_s": args.conformance_horizon_s,
                "predictor": predictor.version,
                # Logged explicitly so an operator can tell from the startup
                # line alone whether the model actually loaded, rather than
                # discovering weeks later that everything ran on physics.
                "model_loaded": predictor.has_model,
            },
        )
        # Also a gauge, so "we have been running on physics for three weeks" is
        # visible on a dashboard rather than only in a startup line nobody has
        # scrolled back to.
        METRICS.model_loaded.labels(service="conformance").set(1 if predictor.has_model else 0)

    async with (
        MessageSubscriber(
            settings.kafka_bootstrap_servers,
            TOPIC_TRACK_UPDATES,
            TrackUpdate,
            group_id=args.group_id,
            # Conflicts are about the airspace *now*. Replaying an hour of
            # history on restart would raise alerts about geometries that
            # resolved long ago, so this consumer starts from the live edge --
            # the one place in the system where losing messages is correct.
            auto_offset_reset="latest",
            client_id="conformance",
        ) as subscriber,
        MessagePublisher(settings.kafka_bootstrap_servers, client_id="conformance") as publisher,
    ):
        _log.info(
            "conformance ready",
            extra={
                "group_id": args.group_id,
                "horizontal_nm": settings.horizontal_separation_nm,
                "vertical_ft": settings.vertical_separation_ft,
                "lookahead_s": settings.conflict_lookahead_s,
            },
        )
        try:
            stats = await ConformanceRunner(
                subscriber,
                publisher,
                alert_store=alert_store,
                monitor=monitor,
                conformance=conformance,
                scan_interval_s=args.scan_interval_s,
            ).run()
            _log.info("conformance stopped", extra={"alerts": stats.alerts_published})
        finally:
            await alert_store.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging("conformance", settings.log_level)
    configure_tracing("acp-conformance", settings.otlp_endpoint)
    serve_metrics(settings.metrics_port)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        _log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
