"""Entry point for the feed service.

acp-feed --scenario scenarios/head-on-conflict.yaml
acp-feed --scenario scenarios/head-on-conflict.yaml --replay --loop
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from acp.common.config import load_settings
from acp.common.contracts import (
    TOPIC_ALERTS,
    TOPIC_SIM_TRUTH,
    TOPIC_SURVEILLANCE_REPORTS,
    TOPIC_TRACK_UPDATES,
)
from acp.common.logging import configure_logging, get_logger
from acp.common.messaging import MessagePublisher, ensure_topics
from acp.services.feed.runner import FeedRunner
from acp.sim.scenario import load_scenario

_log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acp-feed", description=__doc__)
    parser.add_argument(
        "--scenario",
        type=Path,
        default=Path("scenarios/head-on-conflict.yaml"),
        help="scenario YAML to play",
    )
    parser.add_argument(
        "--replay",
        action="store_true",
        help="run as fast as possible instead of pacing against the wall clock",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="restart the scenario when it ends, so a demo keeps running",
    )
    parser.add_argument(
        "--no-truth",
        action="store_true",
        help="skip publishing sim.truth.v1 (evaluation topic)",
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    scenario = load_scenario(args.scenario)

    # The feed starts first in the compose stack, so it is the natural place to
    # create the topics every other service subscribes to.
    await ensure_topics(
        settings.kafka_bootstrap_servers,
        [TOPIC_SURVEILLANCE_REPORTS, TOPIC_TRACK_UPDATES, TOPIC_ALERTS, TOPIC_SIM_TRUTH],
    )

    async with MessagePublisher(settings.kafka_bootstrap_servers, client_id="feed") as publisher:
        runner = FeedRunner(
            scenario,
            publisher,
            realtime=not args.replay,
            publish_truth=not args.no_truth,
        )
        while True:
            await runner.run()
            if not args.loop:
                return
            _log.info("restarting scenario", extra={"scenario_id": scenario.scenario_id})


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging("feed", settings.log_level)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        _log.info("interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
