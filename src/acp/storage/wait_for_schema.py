"""Block until the database schema is at Alembic head.

## Why this exists

Docker Compose can express "do not start until the migration finished"
(`condition: service_completed_successfully`). Kubernetes cannot: a Deployment
has no dependency on a Job, and everything in a directory is applied at once.
Without something to wait on, a rollout carrying a new Alembic revision starts
application pods against the old schema -- which is exactly what an external
review found this stack doing, while `deploy/k8s/README.md` claimed the
opposite.

An init container running this is the standard substitute. **It waits; it does
not migrate.** Having every replica run `alembic upgrade head` on startup would
put concurrent migrations on one database, which is a race with a far worse
failure mode than waiting.

## Why not parse `alembic current`

That would couple this to the display format of a tool with no obligation to
keep it stable. Reading `alembic_version` and comparing it against the head the
committed migration scripts declare asks the same question in the terms both
sides already use.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from acp.common.logging import configure_logging, get_logger

_log = get_logger(__name__)

#: Where the Alembic config lives inside the image, matching the Dockerfile's
#: WORKDIR. Overridable so a developer can run this from a checkout.
DEFAULT_ALEMBIC_INI = Path("alembic.ini")

DEFAULT_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 2.0


def head_revision(config_path: Path) -> str:
    """The revision the committed migration scripts consider current."""
    script = ScriptDirectory.from_config(Config(str(config_path)))
    heads = script.get_heads()
    if len(heads) != 1:
        # More than one head means two migration branches were merged without an
        # Alembic merge revision. There is no single target to wait for, and
        # guessing would be worse than stopping.
        raise SystemExit(f"expected exactly one Alembic head, found {heads!r}")
    return heads[0]


async def applied_revision(dsn: str) -> str | None:
    """What the database says it is at, or None if it cannot say yet.

    A missing table, a refused connection, and a database still starting up are
    deliberately not distinguished: all three mean "not ready", which is the
    condition the caller is waiting to stop being true.
    """
    engine = create_async_engine(dsn, pool_pre_ping=False)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("select version_num from alembic_version"))
            row = result.first()
            return None if row is None else str(row[0])
    except (SQLAlchemyError, OSError):
        return None
    finally:
        await engine.dispose()


async def wait(dsn: str, *, expected: str, timeout_s: float) -> bool:
    """Poll until the schema matches `expected`. Returns whether it did."""
    deadline = time.monotonic() + timeout_s
    while True:
        current = await applied_revision(dsn)
        if current == expected:
            _log.info("schema is at head", extra={"revision": expected})
            return True
        if time.monotonic() >= deadline:
            _log.error(
                "timed out waiting for the schema",
                extra={"expected": expected, "current": current, "timeout_s": timeout_s},
            )
            return False
        await asyncio.sleep(POLL_INTERVAL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acp-wait-for-schema", description=__doc__)
    parser.add_argument("--dsn", help="SQLAlchemy DSN; defaults to ACP_POSTGRES_DSN")
    parser.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--alembic-ini", type=Path, default=DEFAULT_ALEMBIC_INI)
    args = parser.parse_args(argv)

    from acp.common.config import load_settings

    settings = load_settings()
    configure_logging("wait-for-schema", settings.log_level)
    dsn = args.dsn or settings.postgres_dsn

    expected = head_revision(args.alembic_ini)
    ok = asyncio.run(wait(dsn, expected=expected, timeout_s=args.timeout_s))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
