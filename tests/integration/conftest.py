"""Real Redpanda, Postgres, and Redis, started once per test session.

Everything here needs Docker. The whole module skips without it rather than
failing, so `pytest tests/unit` stays runnable on a machine with no daemon --
but CI runs this suite, so skipping locally cannot hide a break.

Containers are session-scoped because starting a broker costs seconds and the
tests are read-mostly against separate topics and keys. Where a test needs
isolation it takes it explicitly, by using a unique topic or consumer group,
rather than by paying for a fresh container.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import closing

import pytest

pytest.importorskip("testcontainers", reason="the integration extra is not installed")

from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

#: Pinned to the same versions `deploy/compose.yml` runs, so a test passing here
#: says something about the stack that actually ships.
REDPANDA_IMAGE = "redpandadata/redpanda:v26.1.16"
POSTGRES_IMAGE = "postgres:17.11-alpine"
REDIS_IMAGE = "redis:8.10"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # noqa: BLE001 - any failure means "no usable Docker"
        return False
    return True


if not _docker_available():
    pytest.skip("Docker is not available", allow_module_level=True)


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_broker(bootstrap: str, *, timeout_s: float = 120.0) -> None:
    """Block until the broker answers a real Kafka request.

    Deliberately not a log-message wait. Redpanda's startup banner changes
    between versions -- an earlier attempt matched a string this image no longer
    prints, and the suite failed after ninety seconds against a broker that had
    been up the whole time. Asking the Kafka API whether it works is both
    version-independent and the thing the tests actually need to be true.
    """

    async def probe() -> None:
        from aiokafka.admin import AIOKafkaAdminClient

        admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
        await admin.start()
        try:
            await admin.list_topics()
        finally:
            await admin.close()

    deadline = time.monotonic() + timeout_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            asyncio.run(probe())
        except Exception as error:  # noqa: BLE001 - any failure means "not ready yet"
            last = error
            time.sleep(1.0)
        else:
            return
    raise TimeoutError(f"broker at {bootstrap} not ready in {timeout_s}s: {last}")


@pytest.fixture(scope="session")
def redpanda() -> Iterator[str]:
    """A single-node Kafka-compatible broker. Yields its bootstrap address.

    The advertised address has to match how the client connects, or the client
    is redirected to a hostname it cannot resolve -- which manifests as a hang
    rather than an error. The port is therefore chosen up front and both
    listeners are configured to agree on it.
    """
    port = _free_port()
    container = (
        DockerContainer(REDPANDA_IMAGE)
        .with_command(
            "redpanda start --mode=dev-container --smp=1 --default-log-level=warn "
            f"--kafka-addr=external://0.0.0.0:{port} "
            f"--advertise-kafka-addr=external://127.0.0.1:{port}"
        )
        .with_bind_ports(port, port)
    )
    container.start()
    bootstrap = f"127.0.0.1:{port}"
    try:
        _wait_for_broker(bootstrap)
        yield bootstrap
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer(REDIS_IMAGE) as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def migrated_postgres(postgres_dsn: str) -> Iterator[str]:
    """Postgres with the schema applied, exactly as the stack applies it.

    Runs Alembic rather than `metadata.create_all`. Those two can diverge, and a
    test suite that builds its schema a different way from production is testing
    a database that does not exist anywhere.
    """
    import os

    from alembic import command
    from alembic.config import Config

    from tests.integration.helpers import repo_root

    config = Config(str(repo_root() / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root() / "migrations"))
    previous = os.environ.get("ACP_POSTGRES_DSN")
    os.environ["ACP_POSTGRES_DSN"] = postgres_dsn
    try:
        command.upgrade(config, "head")
        yield postgres_dsn
    finally:
        if previous is None:
            os.environ.pop("ACP_POSTGRES_DSN", None)
        else:
            os.environ["ACP_POSTGRES_DSN"] = previous


@pytest.fixture
async def clean_redis(redis_url: str) -> AsyncIterator[str]:
    """A Redis with nothing left over from another test."""
    import redis.asyncio as redis_async

    client = redis_async.from_url(redis_url, decode_responses=True)  # type: ignore[no-untyped-call]
    await client.flushall()
    try:
        yield redis_url
    finally:
        await client.aclose()


def unique_suffix() -> str:
    """A short, unique string for topic and consumer-group names.

    Containers are shared across the session, so isolation comes from naming
    rather than from restarting a broker per test.
    """
    return f"{time.time_ns():x}"
