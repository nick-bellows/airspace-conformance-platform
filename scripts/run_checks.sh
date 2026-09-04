#!/usr/bin/env bash
# The fast local gate: the same five checks CI runs before anything needing
# Docker or a network.
#
#   ./scripts/run_checks.sh
#
# The POSIX twin of run_checks.ps1. Both drive the same tools with the same
# arguments, so a green run means the same thing on either platform.

set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project venv, on either layout, then fall back to whatever python
# is on PATH -- a contributor in a container or CI has no .venv at all.
if   [ -x ".venv/bin/python" ];          then python=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ];  then python=".venv/Scripts/python.exe"
elif command -v python3 > /dev/null 2>&1; then python="python3"
elif command -v python  > /dev/null 2>&1; then python="python"
else
    echo "No Python found. Create a venv with:" >&2
    echo '  python -m venv .venv && .venv/bin/python -m pip install -e ".[all]"' >&2
    exit 1
fi

targets=(src tests scripts migrations eval conftest.py)

echo
echo "[1/5] ruff check"
"$python" -m ruff check "${targets[@]}"

echo
echo "[2/5] ruff format --check"
"$python" -m ruff format --check "${targets[@]}"

echo
echo "[3/5] mypy --strict"
"$python" -m mypy src

echo
echo "[4/5] pytest (unit + contract)"
"$python" -m pytest tests/unit tests/contract --cov=acp --cov-report=term-missing

echo
echo "[5/5] contract drift (Kafka schemas + OpenAPI)"
"$python" scripts/contracts.py --check

echo
echo "All checks passed."
echo
echo "Not run here (Docker, minutes each - CI runs them as separate jobs):"
echo "  pytest tests/integration   real Kafka, Postgres, Redis via testcontainers"
echo "  pytest tests/e2e           the whole stack under docker compose"
echo "  pytest tests/perf -s       the latency budget at 500 aircraft"
