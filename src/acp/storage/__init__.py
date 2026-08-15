"""Shared persistence layer: track history in Postgres, live picture in Redis.

This package exists because two services need the same storage and neither may
import the other. It sits alongside `acp.common` at the bottom of the stack:
it may use the contracts, and nothing above it may be imported from here.

The writer/reader split is deliberate and is recorded in ADR 0004 -- the track
service writes, the API service reads, and the schema is a shared contract in
exactly the way the Kafka message models are.
"""
