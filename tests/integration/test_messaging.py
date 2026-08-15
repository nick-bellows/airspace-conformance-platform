"""The Kafka layer, against a real broker.

These are the claims the unit tests structurally cannot make. A fake publisher
proves the runner calls it; only a broker proves the messages survive
serialisation, that keying puts one aircraft on one partition, that a restarted
consumer resumes rather than replays from the start, and that a consumer group
splits work the way scaling out assumes it does.

`common/messaging.py` was excluded from unit coverage on exactly this argument.
This suite is the other half of that bargain.
"""

from __future__ import annotations

import asyncio

import pytest

from acp.common.contracts import SurveillanceReport
from acp.common.logging import trace_id_var
from acp.common.messaging import TRACE_HEADER, MessagePublisher, MessageSubscriber, ensure_topics
from tests.integration.conftest import unique_suffix
from tests.integration.helpers import a_report, seconds_apart

pytestmark = pytest.mark.integration


async def _drain(
    subscriber: MessageSubscriber[SurveillanceReport], count: int, *, timeout_s: float = 30.0
) -> list[SurveillanceReport]:
    """Read exactly `count` messages, or fail with a useful message."""
    received: list[SurveillanceReport] = []

    async def collect() -> None:
        async for envelope in subscriber.stream():
            received.append(envelope.message)
            if len(received) >= count:
                return

    try:
        async with asyncio.timeout(timeout_s):
            await collect()
    except TimeoutError:
        raise AssertionError(
            f"expected {count} messages, received {len(received)} in {timeout_s}s"
        ) from None
    return received


async def test_a_published_message_round_trips_through_a_real_broker(redpanda: str) -> None:
    topic = f"itg.roundtrip.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=1)
    sent = a_report()

    async with MessagePublisher(redpanda) as publisher:
        await publisher.publish(topic, key=sent.icao24, message=sent)

    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        received = await _drain(subscriber, 1)

    assert received[0] == sent


async def test_the_trace_id_survives_the_broker(redpanda: str) -> None:
    """No stack trace crosses a topic, so this header is how one report is
    followed across four services."""
    topic = f"itg.trace.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=1)

    token = trace_id_var.set("trace-under-test")
    try:
        async with MessagePublisher(redpanda) as publisher:
            await publisher.publish(topic, key="a1b2c3", message=a_report())
    finally:
        trace_id_var.reset(token)

    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        async with asyncio.timeout(30.0):
            async for envelope in subscriber.stream():
                assert envelope.trace_id == "trace-under-test"
                break


async def test_one_aircraft_lands_on_one_partition(redpanda: str) -> None:
    """The guarantee the tracker depends on.

    Reports for a given aircraft must arrive in order. Kafka orders within a
    partition, and keying by aircraft address is what puts them all on one.
    Without it the tracker would read a turn out of sequence and compute
    nonsense -- silently.
    """
    topic = f"itg.partition.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=6)
    moments = seconds_apart(30)

    async with MessagePublisher(redpanda) as publisher:
        for index, moment in enumerate(moments):
            await publisher.publish(
                topic, key="a1b2c3", message=a_report(at=moment, sequence=index)
            )

    partitions = set()
    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        received = []
        async with asyncio.timeout(45.0):
            async for envelope in subscriber.stream():
                partitions.add(envelope.partition)
                received.append(envelope.message)
                if len(received) >= len(moments):
                    break

    assert partitions == {next(iter(partitions))}, "one key spread across partitions"
    assert [r.observed_at for r in received] == moments, "order was not preserved"


async def test_different_aircraft_spread_across_partitions(redpanda: str) -> None:
    """The other half of the keying decision: parallelism where ordering is
    not required. If every aircraft landed on one partition the tracker could
    never be scaled out."""
    topic = f"itg.spread.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=6)
    addresses = [f"a1b2{index:02x}" for index in range(24)]

    async with MessagePublisher(redpanda) as publisher:
        for address in addresses:
            await publisher.publish(topic, key=address, message=a_report(icao24=address))

    partitions = set()
    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        seen = 0
        async with asyncio.timeout(45.0):
            async for envelope in subscriber.stream():
                partitions.add(envelope.partition)
                seen += 1
                if seen >= len(addresses):
                    break

    assert len(partitions) > 1, "every aircraft landed on one partition"


async def test_a_restarted_consumer_loses_nothing_and_replays_at_most_one(
    redpanda: str,
) -> None:
    """The precise at-least-once guarantee, demonstrated end to end.

    A restarted consumer must not skip anything, and must not start over from
    the beginning. It *may* redeliver the message it was holding when it
    stopped, because the offset commits only after the handler returns -- that
    is the whole reason handlers are required to be idempotent (ADR 0005).

    An earlier version of this test asserted the sequence was exactly
    0..9 with no repeats, which is at-*most*-once. It failed, correctly: the
    consumer stopped after taking message 3 and before its offset was
    committed, so message 3 arrived again. The system was right and the test
    was wrong.
    """
    topic = f"itg.resume.{unique_suffix()}"
    group = f"g-{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=1)
    moments = seconds_apart(10)

    async with MessagePublisher(redpanda) as publisher:
        for index, moment in enumerate(moments):
            await publisher.publish(
                topic, key="a1b2c3", message=a_report(at=moment, sequence=index)
            )

    async with MessageSubscriber(redpanda, topic, SurveillanceReport, group_id=group) as subscriber:
        before = await _drain(subscriber, 4)

    # Drained by condition, not by count. A redelivery means the second pass
    # needs one more message than naive arithmetic suggests, and a fixed count
    # would either stop short or block until the timeout depending on whether
    # the redelivery happened -- flaky in both directions.
    wanted = {f"r-a1b2c3-{i}" for i in range(10)}
    after: list[SurveillanceReport] = []
    async with MessageSubscriber(redpanda, topic, SurveillanceReport, group_id=group) as subscriber:
        try:
            async with asyncio.timeout(30.0):
                async for envelope in subscriber.stream():
                    after.append(envelope.message)
                    if {r.report_id for r in before + after} >= wanted:
                        break
        except TimeoutError:
            pass

    seen = [r.report_id for r in before + after]
    expected = {f"r-a1b2c3-{i}" for i in range(10)}

    assert set(seen) == expected, "the restart lost messages"
    assert seen == sorted(seen, key=lambda name: int(name.rsplit("-", 1)[1])), (
        "messages arrived out of order"
    )
    # At most the single in-flight message repeats, and it must be the boundary
    # one. Anything more means offsets are committing later than they should.
    repeats = len(seen) - len(set(seen))
    assert repeats <= 1, f"{repeats} messages redelivered; expected at most one in flight"


async def test_a_consumer_group_divides_the_partitions(redpanda: str) -> None:
    """What `--scale track=3` actually relies on.

    Two consumers in one group must each be assigned a subset of the partitions,
    not both receive everything.
    """
    topic = f"itg.group.{unique_suffix()}"
    group = f"g-{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=4)

    async with MessagePublisher(redpanda) as publisher:
        for index in range(40):
            await publisher.publish(
                topic, key=f"a1b2{index:02x}", message=a_report(icao24=f"a1b2{index:02x}")
            )

    async with (
        MessageSubscriber(redpanda, topic, SurveillanceReport, group_id=group) as first,
        MessageSubscriber(redpanda, topic, SurveillanceReport, group_id=group) as second,
    ):

        async def read(subscriber: MessageSubscriber[SurveillanceReport]) -> set[int]:
            partitions: set[int] = set()
            try:
                async with asyncio.timeout(20.0):
                    async for envelope in subscriber.stream():
                        partitions.add(envelope.partition)
            except TimeoutError:
                pass
            return partitions

        assignments = await asyncio.gather(read(first), read(second))

    assert assignments[0] and assignments[1], "one consumer received nothing"
    assert not (assignments[0] & assignments[1]), "both consumers read the same partition"


async def test_a_malformed_message_is_skipped_rather_than_stalling_the_consumer(
    redpanda: str,
) -> None:
    """One bad record must not stop the airspace picture updating for every
    aircraft. The offset advances past it and the next message is delivered."""
    topic = f"itg.poison.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=1)

    async with MessagePublisher(redpanda) as publisher:
        producer = publisher._producer
        assert producer is not None
        await producer.send_and_wait(topic, key=b"a1b2c3", value=b'{"not":"a report"}')
        await publisher.publish(topic, key="a1b2c3", message=a_report(sequence=99))

    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        received = await _drain(subscriber, 1)

    assert received[0].report_id == "r-a1b2c3-99"


async def test_creating_existing_topics_is_harmless(redpanda: str) -> None:
    """Every service calls this on startup; a restart must not fail."""
    topic = f"itg.idempotent.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=2)
    await ensure_topics(redpanda, [topic], partitions=2)


async def test_publishing_before_start_fails_loudly() -> None:
    """A silent no-op here would look like a broker problem for hours."""
    publisher = MessagePublisher("127.0.0.1:1")
    with pytest.raises(RuntimeError, match="not started"):
        await publisher.publish("t", key="k", message=a_report())


async def test_the_trace_header_is_always_present(redpanda: str) -> None:
    """Even with no ambient trace id, one is minted rather than omitted."""
    topic = f"itg.header.{unique_suffix()}"
    await ensure_topics(redpanda, [topic], partitions=1)

    trace_id_var.set(None)
    async with MessagePublisher(redpanda) as publisher:
        await publisher.publish(topic, key="a1b2c3", message=a_report())

    async with MessageSubscriber(
        redpanda, topic, SurveillanceReport, group_id=f"g-{unique_suffix()}"
    ) as subscriber:
        async with asyncio.timeout(30.0):
            async for envelope in subscriber.stream():
                assert envelope.trace_id
                assert TRACE_HEADER
                break
