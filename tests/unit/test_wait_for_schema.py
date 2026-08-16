"""Tests for the Kubernetes schema gate.

This exists because Kubernetes has no `depends_on:
service_completed_successfully`. Every Postgres-touching Deployment runs it as
an init container so it cannot start against a schema that has not caught up --
the ordering compose gets for free and the manifests previously claimed without
having.

The interesting cases are the unhappy ones. A gate that only works when the
database is already correct is not a gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.storage import wait_for_schema
from acp.storage.wait_for_schema import head_revision, main, wait

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def test_head_revision_reads_the_committed_migrations() -> None:
    """Read from the scripts, not from a constant that can drift from them."""
    assert head_revision(ALEMBIC_INI)


def test_head_revision_refuses_multiple_heads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two heads mean two branches merged without a merge revision.

    There is no single target to wait for, so stopping is better than picking
    one and letting pods start against the wrong schema.
    """
    monkeypatch.setattr(
        wait_for_schema.ScriptDirectory,
        "from_config",
        classmethod(lambda cls, config: _FakeScript(("aaa", "bbb"))),
    )
    with pytest.raises(SystemExit, match="exactly one Alembic head"):
        head_revision(ALEMBIC_INI)


class _FakeScript:
    def __init__(self, heads: tuple[str, ...]) -> None:
        self._heads = heads

    def get_heads(self) -> tuple[str, ...]:
        return self._heads


async def test_wait_returns_once_the_schema_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wait_for_schema, "applied_revision", _revisions([None, None, "0007"]))
    monkeypatch.setattr(wait_for_schema, "POLL_INTERVAL_S", 0.0)

    assert await wait("postgresql+asyncpg://x/y", expected="0007", timeout_s=5.0) is True


async def test_wait_gives_up_rather_than_hanging_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pod stuck in Init forever is harder to diagnose than one that fails."""
    monkeypatch.setattr(wait_for_schema, "applied_revision", _revisions([None] * 50))
    monkeypatch.setattr(wait_for_schema, "POLL_INTERVAL_S", 0.0)

    assert await wait("postgresql+asyncpg://x/y", expected="0007", timeout_s=0.05) is False


async def test_wait_keeps_waiting_on_an_older_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the whole thing exists for: migrated, but not far enough.

    An unmigrated database is the obvious failure. A database at the *previous*
    revision is the one that ships broken code, because everything connects
    fine and only the new column is missing.
    """
    monkeypatch.setattr(wait_for_schema, "applied_revision", _revisions(["0006", "0006", "0007"]))
    monkeypatch.setattr(wait_for_schema, "POLL_INTERVAL_S", 0.0)

    assert await wait("postgresql+asyncpg://x/y", expected="0007", timeout_s=5.0) is True


async def test_an_unreachable_database_is_treated_as_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres still starting and a missing table are the same answer here."""
    assert (
        await wait_for_schema.applied_revision("postgresql+asyncpg://nobody@127.0.0.1:1/x") is None
    )


def test_main_prefers_an_explicit_dsn_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_wait(dsn: str, *, expected: str, timeout_s: float) -> bool:
        seen["dsn"] = dsn
        return True

    monkeypatch.setattr(wait_for_schema, "wait", fake_wait)
    assert (
        main(["--dsn", "postgresql+asyncpg://explicit/db", "--alembic-ini", str(ALEMBIC_INI)]) == 0
    )
    assert seen["dsn"] == "postgresql+asyncpg://explicit/db"


def test_main_returns_nonzero_when_the_wait_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The init container must fail the pod, not let it through."""

    async def fake_wait(dsn: str, *, expected: str, timeout_s: float) -> bool:
        return False

    monkeypatch.setattr(wait_for_schema, "wait", fake_wait)
    assert main(["--dsn", "postgresql+asyncpg://x/y", "--alembic-ini", str(ALEMBIC_INI)]) == 1


def _revisions(sequence: list[str | None]):  # type: ignore[no-untyped-def]
    """An `applied_revision` stand-in returning each value in turn."""
    remaining = list(sequence)

    async def _applied(dsn: str) -> str | None:
        return remaining.pop(0) if remaining else None

    return _applied
