"""Independently deployable services.

A service never imports another service. Cross-service communication is Kafka
or HTTP. Enforced by `tests/unit/test_architecture.py`.
"""
