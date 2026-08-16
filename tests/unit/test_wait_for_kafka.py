"""Tests for the Kafka readiness gate.

Exists because Kubernetes has no `depends_on`. Compose waits for the broker to
be healthy; the manifests could not, so every Kafka client started before the
broker was listening and crash-looped until it was. That was tolerable until the
backoff pushed the conformance service past a 240 s rollout timeout and failed
the `k8s` job on this project's first CI run.

The interesting cases are the unhappy ones, as with the schema gate: a probe
that only works when the broker is already up is not a gate.
"""

from __future__ import annotations

import pytest

from acp.common import wait_for_kafka
from acp.common.wait_for_kafka import main, wait


async def test_returns_as_soon_as_the_broker_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wait_for_kafka, "broker_is_up", _answers([False, False, True]))
    monkeypatch.setattr(wait_for_kafka, "POLL_INTERVAL_S", 0.0)

    assert await wait("redpanda:9092", timeout_s=5.0) is True


async def test_gives_up_rather_than_hanging_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pod stuck in Init forever is harder to diagnose than one that fails."""
    monkeypatch.setattr(wait_for_kafka, "broker_is_up", _answers([False] * 50))
    monkeypatch.setattr(wait_for_kafka, "POLL_INTERVAL_S", 0.0)

    assert await wait("redpanda:9092", timeout_s=0.05) is False


async def test_an_unreachable_broker_is_not_ready_rather_than_an_error() -> None:
    """A refused connection and an unresolved name are the same answer here."""
    assert await wait_for_kafka.broker_is_up("127.0.0.1:1") is False


def test_main_returns_nonzero_when_the_broker_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The init container must fail the pod rather than let it through."""

    async def never(bootstrap_servers: str, *, timeout_s: float) -> bool:
        return False

    monkeypatch.setattr(wait_for_kafka, "wait", never)
    assert main(["--bootstrap-servers", "redpanda:9092", "--timeout-s", "0.1"]) == 1


def test_main_prefers_an_explicit_bootstrap_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake(bootstrap_servers: str, *, timeout_s: float) -> bool:
        seen["servers"] = bootstrap_servers
        return True

    monkeypatch.setattr(wait_for_kafka, "wait", fake)
    assert main(["--bootstrap-servers", "explicit:9092"]) == 0
    assert seen["servers"] == "explicit:9092"


def _answers(sequence: list[bool]):  # type: ignore[no-untyped-def]
    remaining = list(sequence)

    async def _up(bootstrap_servers: str) -> bool:
        return remaining.pop(0) if remaining else False

    return _up
