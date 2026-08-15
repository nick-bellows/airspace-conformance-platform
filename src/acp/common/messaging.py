"""Kafka publisher and subscriber, typed to the wire contracts.

Three decisions are baked in here and each shows up in the API:

**Keying.** Every publish requires a key. Reports and track updates are keyed by
aircraft address, so Kafka orders all messages for one aircraft while processing
different aircraft in parallel. That is the only ordering guarantee the trackers
and detectors need, and it is the one that costs nothing.

**At-least-once.** Auto-commit is off. The offset advances only after the
handler body has finished, so a crash mid-handler replays the message rather
than losing it. The cost is that handlers must be idempotent -- see ADR 0005.

**Poison messages are skipped, not retried forever.** A record that fails
validation is logged and its offset committed. The alternative -- halting the
consumer -- means one malformed message stops the airspace picture updating for
every aircraft. A real deployment would route it to a dead-letter topic; that is
noted as a gap rather than pretended away.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from pydantic import BaseModel, ValidationError

from acp.common.logging import get_logger, trace_id_var

#: Kafka header carrying the correlation id across a topic boundary. No stack
#: trace crosses a broker, so this is how one report is followed end to end.
TRACE_HEADER = "x-acp-trace-id"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Envelope[MessageT: BaseModel]:
    """A decoded message plus the delivery metadata worth having."""

    message: MessageT
    key: str | None
    topic: str
    partition: int
    offset: int
    trace_id: str


class MessagePublisher:
    """Publishes contract models to Kafka as JSON.

    Use as an async context manager::

        async with MessagePublisher(bootstrap) as publisher:
            await publisher.publish(TOPIC, key=report.icao24, message=report)
    """

    def __init__(self, bootstrap_servers: str, *, client_id: str = "acp") -> None:
        self._bootstrap_servers = bootstrap_servers
        self._client_id = client_id
        self._producer: AIOKafkaProducer | None = None

    async def __aenter__(self) -> Self:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            client_id=self._client_id,
            # Wait for all in-sync replicas. Single-broker development makes
            # this cheap; the setting is what production would want and costs
            # nothing to be correct about now.
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
        )
        await self._producer.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, topic: str, *, key: str, message: BaseModel) -> None:
        """Publish one message. The key is required, never optional.

        Making the key mandatory is deliberate: a message published without one
        is round-robined across partitions, which silently destroys the
        per-aircraft ordering the tracker depends on. That failure is invisible
        until a turn is misread, so the API refuses to allow it.
        """
        if self._producer is None:
            raise RuntimeError("publisher is not started; use it as an async context manager")
        trace_id = trace_id_var.get() or uuid.uuid4().hex
        await self._producer.send_and_wait(
            topic,
            key=key.encode(),
            value=message.model_dump_json().encode(),
            headers=[(TRACE_HEADER, trace_id.encode())],
        )


class MessageSubscriber[MessageT: BaseModel]:
    """Consumes and validates messages of one contract type from one topic."""

    def __init__(
        self,
        bootstrap_servers: str,
        topic: str,
        model: type[MessageT],
        *,
        group_id: str,
        auto_offset_reset: str = "earliest",
        client_id: str = "acp",
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._topic = topic
        self._model = model
        self._group_id = group_id
        self._auto_offset_reset = auto_offset_reset
        self._client_id = client_id
        self._consumer: AIOKafkaConsumer | None = None

    async def __aenter__(self) -> Self:
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            client_id=self._client_id,
            enable_auto_commit=False,
            auto_offset_reset=self._auto_offset_reset,
        )
        await self._consumer.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    async def stream(self) -> AsyncIterator[Envelope[MessageT]]:
        """Yield decoded messages, committing after each is handled.

        The commit happens when the consumer of this generator asks for the next
        item, which is after the loop body has run. That ordering is what makes
        delivery at-least-once rather than at-most-once.
        """
        if self._consumer is None:
            raise RuntimeError("subscriber is not started; use it as an async context manager")

        async for record in self._consumer:
            trace_id = _trace_id_from(record.headers)
            # Saved and restored **by value**, not with the token `set()`
            # returns. A token may only be reset in the context that created it,
            # and an async generator can resume -- or be closed -- in a
            # different one. Breaking out of `async for` over this stream then
            # raised `ValueError: Token was created in a different Context` from
            # inside the `finally`, masking whatever the caller was actually
            # doing. Found by an integration test that stops reading early;
            # no unit test with a fake could have produced it.
            previous = trace_id_var.get()
            trace_id_var.set(trace_id)
            try:
                message = self._model.model_validate_json(record.value)
            except ValidationError as error:
                # Skip and advance. Halting here would stop the airspace picture
                # updating for every aircraft because of one bad message.
                _log.warning(
                    "discarding malformed message",
                    extra={
                        "topic": record.topic,
                        "partition": record.partition,
                        "offset": record.offset,
                        "errors": error.error_count(),
                    },
                )
                await self._consumer.commit()
                trace_id_var.set(previous)
                continue

            try:
                yield Envelope(
                    message=message,
                    key=record.key.decode() if record.key else None,
                    topic=record.topic,
                    partition=record.partition,
                    offset=record.offset,
                    trace_id=trace_id,
                )
                await self._consumer.commit()
            finally:
                trace_id_var.set(previous)


def _trace_id_from(headers: Sequence[tuple[str, bytes]] | None) -> str:
    """Read the correlation id from Kafka headers, or mint a new one."""
    for name, value in headers or ():
        if name == TRACE_HEADER:
            return value.decode(errors="replace")
    return uuid.uuid4().hex


async def ensure_topics(
    bootstrap_servers: str, topics: Sequence[str], *, partitions: int = 6
) -> None:
    """Create topics up front rather than relying on auto-creation.

    Auto-created topics take the broker's default partition count, which is
    usually one. A single partition means every aircraft is processed by one
    consumer in strict global order -- correct, but with no parallelism at all
    and no way to scale the tracker horizontally. Creating them explicitly makes
    the partition count a decision rather than an accident.
    """
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap_servers)
    await admin.start()
    try:
        await admin.create_topics(
            [NewTopic(name=t, num_partitions=partitions, replication_factor=1) for t in topics]
        )
        _log.info("created topics", extra={"topics": list(topics), "partitions": partitions})
    except TopicAlreadyExistsError:
        _log.debug("topics already present", extra={"topics": list(topics)})
    finally:
        await admin.close()
