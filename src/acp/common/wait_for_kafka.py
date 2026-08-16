"""Block until the Kafka broker accepts connections.

## Why this exists

Compose expresses the dependency (`depends_on: redpanda, condition:
service_healthy`). Kubernetes has no equivalent, so every Kafka client starts
the moment it is scheduled -- which on a cold cluster is before the broker is
listening. The services then die with `KafkaConnectionError: Unable to bootstrap
from redpanda:9092` and Kubernetes restarts them with an increasing backoff.

Crash-loop-until-ready is a defensible pattern and the pods do eventually come
up. It stopped being defensible when the backoff pushed the conformance service
past a 240 s rollout timeout and failed the `k8s` job on the first CI run this
project ever had. An init container costs one short-lived container and removes
the failure mode.

It waits; it creates nothing. Topic creation stays with the feed, which is the
one service that owns it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from aiokafka.admin import AIOKafkaAdminClient

from acp.common.config import load_settings
from acp.common.logging import configure_logging, get_logger

_log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 240.0
POLL_INTERVAL_S = 2.0


async def broker_is_up(bootstrap_servers: str) -> bool:
    """Whether the broker answers an admin request right now."""
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers)
    try:
        await admin.start()
        await admin.list_topics()
        return True
    except Exception:  # noqa: BLE001 - every failure means "not yet"
        # Deliberately broad. A refused connection, an unresolved hostname, and
        # a broker still electing a controller are different exceptions and the
        # same answer, and enumerating aiokafka's error surface here would be a
        # list that silently goes stale.
        return False
    finally:
        with contextlib.suppress(Exception):
            await admin.close()


async def wait(bootstrap_servers: str, *, timeout_s: float) -> bool:
    """Poll until the broker is up. Returns whether it came up in time."""
    deadline = time.monotonic() + timeout_s
    while True:
        if await broker_is_up(bootstrap_servers):
            _log.info("broker is up", extra={"bootstrap_servers": bootstrap_servers})
            return True
        if time.monotonic() >= deadline:
            _log.error(
                "timed out waiting for the broker",
                extra={"bootstrap_servers": bootstrap_servers, "timeout_s": timeout_s},
            )
            return False
        await asyncio.sleep(POLL_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp-wait-for-kafka", description=__doc__)
    parser.add_argument("--bootstrap-servers", help="defaults to ACP_KAFKA_BOOTSTRAP_SERVERS")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging("wait-for-kafka", settings.log_level)
    servers = args.bootstrap_servers or settings.kafka_bootstrap_servers

    return 0 if asyncio.run(wait(servers, timeout_s=args.timeout_s)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
