"""Backward compatibility of the wire contracts.

A drift gate says "the schema changed without being regenerated". It says
nothing about whether the change was *safe*. These tests encode the versioning
rule the contracts module states, so a breaking change has to be a deliberate
act rather than an accident:

    additive optional fields  -> compatible, same topic
    removing or renaming      -> breaking, needs a new topic and a dual-write
    narrowing a type          -> breaking, same

The check runs against the committed schema in git, so it catches a break in the
*working tree* before it is merged rather than after.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts.contracts import render, schema_path

from acp.common.contracts import TOPIC_MODELS

REPO_ROOT = Path(__file__).resolve().parents[2]


def committed_schema(path: Path) -> dict[str, Any] | None:
    """The schema as of HEAD, or None on a repository with no commits yet."""
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"HEAD:{path.relative_to(REPO_ROOT).as_posix()}"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _required(schema: dict[str, Any]) -> set[str]:
    return set(schema.get("required", ()))


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    return dict(schema.get("properties", {}))


@pytest.mark.parametrize("topic", sorted(TOPIC_MODELS))
def test_no_field_was_removed_from_a_topic(topic: str) -> None:
    """Removing a field breaks every consumer that reads it.

    If this fails and the removal is intended, the change belongs on a new topic
    version with a dual-write window -- not on this one.
    """
    previous = committed_schema(schema_path(topic))
    if previous is None:
        pytest.skip("no committed schema to compare against")

    current = json.loads(render(topic))
    missing = set(_properties(previous)) - set(_properties(current))
    assert not missing, f"{topic}: fields removed without a version bump: {sorted(missing)}"


@pytest.mark.parametrize("topic", sorted(TOPIC_MODELS))
def test_no_optional_field_became_required(topic: str) -> None:
    """A producer that has not been redeployed still omits it."""
    previous = committed_schema(schema_path(topic))
    if previous is None:
        pytest.skip("no committed schema to compare against")

    current = json.loads(render(topic))
    newly_required = _required(current) - _required(previous)
    assert not newly_required, (
        f"{topic}: fields became required without a version bump: {sorted(newly_required)}"
    )


@pytest.mark.parametrize("topic", sorted(TOPIC_MODELS))
def test_field_types_did_not_change(topic: str) -> None:
    """A widened type is compatible; a changed one is not.

    Compares the JSON Schema fragment for each surviving field. Any difference
    is reported rather than trying to decide which direction is safe -- a
    reviewer can tell, and a heuristic that got it wrong would be worse than a
    prompt to look.
    """
    previous = committed_schema(schema_path(topic))
    if previous is None:
        pytest.skip("no committed schema to compare against")

    current = json.loads(render(topic))
    old_properties = _properties(previous)
    new_properties = _properties(current)

    changed = [
        name
        for name, definition in old_properties.items()
        if name in new_properties and new_properties[name] != definition
    ]
    assert not changed, (
        f"{topic}: field definitions changed: {sorted(changed)}. "
        "Widening is compatible and narrowing is not; if this is intended and "
        "safe, regenerate the committed schema in the same commit."
    )


def test_new_topics_are_additive_only() -> None:
    """A topic disappearing strands whatever was consuming it."""
    committed = {
        p.name
        for p in (REPO_ROOT / "contracts").glob("*.schema.json")
        if committed_schema(p) is not None
    }
    current = {schema_path(t).name for t in TOPIC_MODELS}
    removed = committed - current
    assert not removed, f"topics removed: {sorted(removed)}"


def test_the_contracts_version_is_stamped_on_every_schema() -> None:
    """The stamp is how a consumer knows which generation it is holding."""
    for topic in TOPIC_MODELS:
        schema = json.loads(render(topic))
        assert schema["x-contracts-version"]
        assert schema["x-topic"] == topic


def test_schema_generation_is_deterministic() -> None:
    """Non-deterministic output would make the drift gate fire at random."""
    for topic in TOPIC_MODELS:
        assert render(topic) == render(topic)


def test_the_generator_runs_as_a_script() -> None:
    """The command the failure messages tell people to run has to work."""
    result = subprocess.run(
        [sys.executable, "scripts/contracts.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
