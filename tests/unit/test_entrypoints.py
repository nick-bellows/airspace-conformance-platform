"""Tests for the service command-line interfaces.

Only argument parsing is exercised here. The `run()` bodies open Kafka, Postgres
and Redis connections, so they belong to the integration and e2e suites at M4;
what is testable without infrastructure is that the flags mean what the compose
file and the Kubernetes manifests assume they mean.

That assumption is not idle: `deploy/compose.yml` passes `--scenario=` and
`--loop` to the feed service, and a rename here would break the stack in a way
no other test would catch until it failed to start.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.services.feed.__main__ import build_parser as feed_parser
from acp.services.track.__main__ import build_parser as track_parser


def test_the_feed_defaults_to_the_demo_scenario() -> None:
    """`acp-feed` with no arguments must produce the picture in the README."""
    args = feed_parser().parse_args([])
    assert args.scenario == Path("scenarios/head-on-conflict.yaml")
    assert args.replay is False
    assert args.loop is False
    assert args.no_truth is False


def test_the_feed_accepts_the_flags_compose_passes_it() -> None:
    args = feed_parser().parse_args(
        ["--scenario=scenarios/quiet-cruise.yaml", "--loop", "--replay", "--no-truth"]
    )
    assert args.scenario == Path("scenarios/quiet-cruise.yaml")
    assert args.loop is True
    assert args.replay is True
    assert args.no_truth is True


def test_the_tracker_defaults_to_a_named_consumer_group() -> None:
    """Scaling out means more instances sharing this value; a random default
    would silently give every replica its own copy of every partition."""
    assert track_parser().parse_args([]).group_id == "track-service"


def test_the_tracker_group_can_be_overridden() -> None:
    assert track_parser().parse_args(["--group-id=replay"]).group_id == "replay"


@pytest.mark.parametrize("parser", [feed_parser(), track_parser()])
def test_unknown_flags_are_rejected(parser: object) -> None:
    with pytest.raises(SystemExit):
        parser.parse_args(["--not-a-real-flag"])  # type: ignore[attr-defined]
