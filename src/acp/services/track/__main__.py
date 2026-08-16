"""Entry point for the track service.

acp-track
"""

from __future__ import annotations

import argparse
import asyncio

from aiokafka import TopicPartition

from acp.common.config import load_settings
from acp.common.contracts import TOPIC_SURVEILLANCE_REPORTS, SurveillanceReport
from acp.common.logging import configure_logging, get_logger
from acp.common.messaging import MessagePublisher, MessageSubscriber
from acp.common.metrics import serve_metrics
from acp.common.tracing import configure_tracing
from acp.services.track.runner import TrackRunner
from acp.storage.stores import LiveTrackStore, TrackHistoryStore

_log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acp-track", description=__doc__)
    parser.add_argument(
        "--group-id",
        default="track-service",
        help="Kafka consumer group; scaling out means running more instances with the same value",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    history = TrackHistoryStore.from_dsn(settings.postgres_dsn)
    live = LiveTrackStore.from_url(settings.redis_url)

    # The runner has to exist before the subscriber, because the subscriber's
    # rebalance listener calls into it. Constructing it with `subscriber=None`
    # and attaching afterwards would be the other way round and would leave a
    # window where a rebalance could arrive with nothing to handle it.
    runner: TrackRunner | None = None

    async def on_revoke(revoked: frozenset[TopicPartition]) -> None:
        if runner is not None:
            await runner.release_partitions(revoked)

    async def on_assign(assigned: frozenset[TopicPartition]) -> None:
        if runner is not None:
            await runner.claim_partitions(assigned)

    try:
        async with (
            MessageSubscriber(
                settings.kafka_bootstrap_servers,
                TOPIC_SURVEILLANCE_REPORTS,
                SurveillanceReport,
                group_id=args.group_id,
                auto_offset_reset=settings.kafka_auto_offset_reset,
                client_id="track",
                # Without these the tracker keeps filtering, ageing, and finally
                # terminating aircraft that Kafka has handed to another replica.
                # See ADR 0011.
                on_revoke=on_revoke,
                on_assign=on_assign,
            ) as subscriber,
            MessagePublisher(settings.kafka_bootstrap_servers, client_id="track") as publisher,
        ):
            runner = TrackRunner(subscriber, publisher, history, live)
            _log.info("tracker ready", extra={"group_id": args.group_id})
            stats = await runner.run()
            _log.info(
                "tracker stopped",
                extra={
                    "reports": stats.reports_consumed,
                    "released": stats.tracks_released,
                },
            )
    finally:
        await history.dispose()
        await live.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging("track", settings.log_level)
    configure_tracing("acp-track", settings.otlp_endpoint)
    serve_metrics(settings.metrics_port)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        _log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
