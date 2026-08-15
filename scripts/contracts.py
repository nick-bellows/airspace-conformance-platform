"""Generate and verify the committed JSON Schemas for every Kafka topic.

The schemas in `contracts/` are the artefact other teams would review, so they
are committed rather than generated at runtime. CI runs this with ``--check``:
if a model changes without the schema being regenerated, the build fails. That
is the whole point — a producer cannot quietly alter the wire format.

The HTTP surface is covered the same way. `contracts/openapi.json` is generated
from the running application, and a route or response model that changes without
it being regenerated fails the build. A spec that is allowed to drift is a spec
nobody can generate a client from, and `tests/contract/test_openapi.py` fuzzes
the implementation against this file — so a stale copy would mean testing
against a document that no longer describes the service.

Usage::

    python scripts/contracts.py            # write contracts/
    python scripts/contracts.py --check    # fail if committed files are stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from acp.common.contracts import CONTRACTS_VERSION, TOPIC_MODELS

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"
OPENAPI_PATH = CONTRACTS_DIR / "openapi.json"


def schema_path(topic: str) -> Path:
    """Filename for a topic's schema: dots become dashes for readability."""
    return CONTRACTS_DIR / f"{topic.replace('.', '-')}.schema.json"


def render(topic: str) -> str:
    """Serialise one topic's JSON Schema deterministically."""
    model = TOPIC_MODELS[topic]
    schema = model.model_json_schema(mode="serialization")
    schema["$id"] = f"https://airspace-conformance-platform.invalid/contracts/{topic}"
    schema["x-contracts-version"] = CONTRACTS_VERSION
    schema["x-topic"] = topic
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def render_openapi() -> str:
    """Serialise the API's OpenAPI document deterministically.

    Built from the application object rather than by starting a server, so this
    works in a checkout with no Redis or Postgres available.
    """
    from acp.common.config import Settings
    from acp.services.api.app import create_app

    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def write_all() -> list[Path]:
    """Write every contract artefact to disk and return the paths written."""
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for topic in sorted(TOPIC_MODELS):
        path = schema_path(topic)
        path.write_text(render(topic), encoding="utf-8")
        written.append(path)
    OPENAPI_PATH.write_text(render_openapi(), encoding="utf-8")
    written.append(OPENAPI_PATH)
    return written


def check_all() -> list[str]:
    """Return a list of human-readable drift problems; empty means clean."""
    problems = []
    for topic in sorted(TOPIC_MODELS):
        path = schema_path(topic)
        if not path.exists():
            problems.append(f"{path.name}: missing (run `python scripts/contracts.py`)")
            continue
        if path.read_text(encoding="utf-8") != render(topic):
            problems.append(f"{path.name}: stale (run `python scripts/contracts.py`)")

    expected = {schema_path(t).name for t in TOPIC_MODELS}
    for orphan in sorted(p.name for p in CONTRACTS_DIR.glob("*.schema.json")):
        if orphan not in expected:
            problems.append(f"{orphan}: no matching model in TOPIC_MODELS")

    if not OPENAPI_PATH.exists():
        problems.append(f"{OPENAPI_PATH.name}: missing (run `python scripts/contracts.py`)")
    elif OPENAPI_PATH.read_text(encoding="utf-8") != render_openapi():
        problems.append(f"{OPENAPI_PATH.name}: stale (run `python scripts/contracts.py`)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)

    if args.check:
        problems = check_all()
        for problem in problems:
            print(f"contract drift: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"contracts ok ({len(TOPIC_MODELS)} topics + openapi, {CONTRACTS_VERSION})")
        return 0

    for path in write_all():
        print(f"wrote {path.relative_to(CONTRACTS_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
