"""Entry point for the conformance service.

acp-conformance
"""

from __future__ import annotations

import argparse
import asyncio

from acp.common.config import load_settings
from acp.common.contracts import TOPIC_TRACK_UPDATES, TrackUpdate
from acp.common.logging import configure_logging, get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
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
    return parser


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    monitor = SeparationMonitor(
        horizontal_nm=settings.horizontal_separation_nm,
        vertical_ft=settings.vertical_separation_ft,
        lookahead_s=settings.conflict_lookahead_s,
    )
    alert_store = LiveAlertStore.from_url(settings.redis_url)

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
                scan_interval_s=args.scan_interval_s,
            ).run()
            _log.info("conformance stopped", extra={"alerts": stats.alerts_published})
        finally:
            await alert_store.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging("conformance", settings.log_level)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        _log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
