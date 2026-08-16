# ADR 0011 — Partition ownership is state, and must be released explicitly

Status: accepted · Date: 2026-08-16

## Context

The tracker keeps a Kalman filter per aircraft, in memory, keyed by aircraft
address. It scales horizontally by running several instances in one Kafka
consumer group: reports are keyed by address, so each aircraft's reports land on
one partition, and Kafka assigns partitions across the group. The README called
this "the one service that genuinely scales out".

That was true only while the assignment never changed.

Kafka reassigns partitions whenever group membership does — a replica joining, a
replica leaving, a rolling restart, a pod eviction. Nothing told the tracker when
that happened. It kept every filter it had ever created, and its sweep timer kept
ageing all of them.

The failure that produces is not a crash and not a stack trace:

1. Replica A owns partition 3 and holds a confirmed track for `trk-a1b2c3`.
2. Replica B joins. Kafka moves partition 3 to B.
3. B receives the next report for that aircraft, sees no local state, and
   initiates a fresh track — with the *same* deterministic `track_id`.
4. A receives nothing more for that aircraft. Thirty seconds later its sweep
   declares the track stale, publishes `TERMINATED`, and deletes the Redis
   entry.
5. An aircraft that is reporting normally disappears from the shared picture and
   off the display, because someone added a replica.

Two independent external reviews found this in M5. It had been latent since M1,
and the M1 notes had already recorded that mid-stream rebalance was untested —
the gap was known and the consequence was not.

## Decision

`MessageSubscriber` accepts an `on_revoke` hook and registers a
`ConsumerRebalanceListener` with Kafka. On revocation the tracker:

1. flushes buffered history, because those offsets are already committed and
   have no redelivery to recover them;
2. **drops the estimators for the revoked partitions without publishing
   anything**;
3. corrects the live-track gauge;
4. clears the consumer-lag series for those partitions.

Which aircraft to drop is decided by **recording** the partition each report
arrived on, not by recomputing it. Kafka picks the partition from a hash of the
key; reimplementing that hash here would be a second copy of the broker's
partitioner, free to diverge silently. An observation of where a report actually
came from cannot disagree with the broker, because it *is* the broker's answer.

## Consequences

**Zero is the correct number of `TERMINATED` messages to publish on a
rebalance.** Ownership moving is not an aircraft disappearing. This is the whole
decision, and `test_releasing_a_partition_publishes_no_termination` pins it.

**The new owner starts a fresh filter.** Filter state is not handed over. That
costs a few seconds of convergence for the affected aircraft — the track briefly
reads as newly initiated with a wide covariance. Transferring state would mean a
side channel between replicas, a serialisation format for filter internals, and
a new consistency problem, in exchange for avoiding a few seconds of degraded
accuracy on a subset of aircraft during an event that should be rare. The
cheaper failure is the right one to accept, and it is *visible* — a track that
re-initiates is legible on the display, where a track that vanishes is not.

**Redis entries are deliberately left alone.** They are the shared picture, the
new owner overwrites them within a second, and they carry a TTL that cleans up
if it does not. Deleting them on revocation would reintroduce the original bug
from the other direction.

**The sweep still terminates aircraft that really stop reporting.** That is the
behaviour this must not break, so it has its own test
(`test_an_aircraft_that_really_stops_reporting_is_still_terminated`) whose only
job is to fail if `release()` is ever confused with `sweep()`.

**Consumer-lag series are cleared at the same point.** A Prometheus gauge is only
ever *set*, so a replica that loses a partition would otherwise keep exporting
that partition's last lag value forever, and a `sum` across replicas would report
a backlog nobody has. Revocation is the only moment that can know the series has
become meaningless.

**The conformance service is unaffected**, because it is pinned to one replica
and does not shard. If it is ever sector-partitioned, it will need the same
treatment and will be a harder case, since its state is the whole picture rather
than one aircraft.

## Alternatives rejected

**Compute the partition from the key.** Requires reimplementing Kafka's
murmur2-based default partitioner. Correct until the broker changes it or a
producer sets a custom one, at which point the tracker silently releases the
wrong aircraft. Recording what the broker actually did has none of that risk.

**Hand filter state to the new owner.** A state-transfer protocol between
replicas — over a compacted topic, or a shared store — is a real design used by
real systems. It is also several times the size of this fix, adds a new
consistency question, and buys a few seconds of convergence on an event that
happens during deployments.

**Use a static partition assignment instead of a consumer group.** Removes
rebalancing entirely and with it the ability to scale elastically, which is the
property the service exists to demonstrate.

**Terminate the tracks on revocation and let the new owner re-initiate.** The
original behaviour, arrived at by accident rather than choice. It is honest about
the state being dropped and dishonest about what that means to a consumer: a
`TERMINATED` message says the aircraft is gone, and downstream consumers have no
way to distinguish "gone" from "moved to another replica".

## The second defect, found reviewing the first fix

The above is necessary and was not sufficient. A rebalance does not wait for a
convenient moment: Kafka's coordinator invokes the listener from its own task,
so `release_partitions` runs **concurrently** with report handling and with the
sweep timer. A third external review reproduced the consequence:

1. A handler updates its estimator and suspends inside `publisher.publish()`.
2. The revocation lands. It flushes, drops the estimator, and returns.
3. The handler resumes and appends its update to the write buffer.
4. The next flush writes the released aircraft back into Redis — on top of
   whatever its new owner has since written there.

Two more things were needed:

**A lock** serialising handling, sweeping, and releasing. All three mutate the
same state from three different tasks, and none of them was synchronised.

**A split between the two stores on revocation.** This is the part that is not
obvious, and a lock alone does not fix: the revocation's *own* flush is what
writes the in-flight update. History and the live picture answer different
questions. History is a record of what this instance observed — true regardless
of who owns the aircraft now, append-only, idempotent under the unique
constraint, and unrecoverable if dropped because the offsets are already
committed. The live picture is a claim about the present, and after revocation
this instance is not entitled to make it. So `_flush(exclude_from_live=...)`
writes the history and withholds the claim.

A report that arrives *after* revocation is discarded rather than processed, for
the same reason in the other direction: the offset was never committed, so the
new owner replays it. `claim_partitions` clears the discard set when Kafka gives
a partition back — without it, a returned partition would be silently dropped
for the life of the process.

## What this cost

The original defect was invisible to 512 passing tests, because every one of
them ran a single consumer. The concurrency defect was then invisible to the
seven tests written for the fix, because every one of them called
`release_partitions` after the consumer loop had already finished. A test that
cannot express the interleaving cannot find a bug in it.

`tests/unit/test_rebalance.py` now covers both orderings and was checked by
reverting each mechanism separately: three tests fail without `release()`, and
the concurrency test fails without the lock.
