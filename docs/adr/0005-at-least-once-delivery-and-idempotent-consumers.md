# ADR 0005 — At-least-once delivery, with idempotency in the database

Status: accepted · Date: 2026-08-15

## Context

Kafka offers three delivery guarantees in practice, and the choice determines
what every consumer in the system has to cope with.

- **At-most-once.** Commit the offset before handling the message. A crash
  mid-handler loses it silently.
- **At-least-once.** Commit after handling. A crash mid-handler replays the
  message, so a consumer may see it twice.
- **Exactly-once.** Available for Kafka-to-Kafka flows via transactions, but the
  guarantee ends at the broker. Our consumers also write to Postgres and Redis,
  which are not part of the transaction, so "exactly once" would be a claim we
  could not actually make.

The workload is surveillance data. A lost report is a gap in a track — an
aircraft that appears to jump. A duplicated report, if handled badly, inflates
history and could double-count an aircraft.

## Decision

**At-least-once**, and idempotency enforced in the database rather than in
application code.

- `enable_auto_commit=False`; the offset advances only after the handler body
  completes (`MessageSubscriber.stream`).
- `track_points` carries a unique constraint on `(track_id, observed_at)` and
  inserts use `ON CONFLICT DO NOTHING`.
- `tracks` is upserted, so a replay overwrites rather than duplicates.
- Redis writes are last-write-wins by construction.
- Track ids are **derived** from the aircraft address rather than generated, so
  a replayed report produces the same key rather than a new row.

## Consequences

**Why not at-most-once.** Losing a report is worse than seeing one twice. A
duplicate is a no-op against a unique constraint; a gap is a track that jumps,
and a jump is exactly what the conformance monitor is built to treat as a
manoeuvre. Silently dropping data would generate false alerts.

**Why not exactly-once.** The transaction would have to span Kafka, Postgres,
and Redis. It does not, so the guarantee would be marketing. Idempotent writes
give the same observable outcome and the mechanism is one line of DDL that a
reviewer can check.

**The cost, stated plainly.** Every consumer must be idempotent, and "must" is
doing a lot of work in that sentence — it is a property nothing enforces
automatically. Three things reduce the risk:

1. The idempotency lives in a **schema constraint**, not in a code path someone
   could forget. A duplicate insert fails at the database whether or not the
   application remembered to think about it.
2. The constraint is **named explicitly** in the migration and referenced by
   name in the writer's `ON CONFLICT` clause, so a regenerated migration cannot
   silently give it a different name and break the guarantee.
3. A test replays the same reports through the tracker twice and asserts one
   track results.

**What is still exposed.** The conformance service holds in-memory state -- the
airspace picture and the alert lifecycle. Its idempotency is weaker: a replay
re-absorbs track updates, which is harmless because the picture is
last-write-wins, but an alert that was raised, published, and then replayed
after a crash would be re-raised with a new `alert_id`. Consumers key on
`alert_key` and keep the latest state, so the visible outcome is correct, but
the event stream would contain a duplicate NEW. Acceptable for an advisory
system; it is recorded here rather than hidden.

**One deliberate exception.** The conformance service consumes with
`auto_offset_reset="latest"`. Conflicts are a statement about the airspace
*now*; replaying an hour of history on restart would raise alerts about
geometries that resolved long ago. This is the one place in the system where
discarding messages is the correct behaviour, and it is the reason the setting
is per-service rather than global.
