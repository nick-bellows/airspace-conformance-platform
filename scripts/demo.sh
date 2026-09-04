#!/usr/bin/env bash
# Bring up the whole stack and open the display.
#
#   ./scripts/demo.sh                            # head-on conflict (default)
#   ./scripts/demo.sh --scenario quiet-cruise    # the false-alarm control
#   ./scripts/demo.sh --tools                    # plus the Redpanda console
#   ./scripts/demo.sh --down                     # tear everything down
#
# The POSIX twin of demo.ps1. The services and images were always Linux; only
# the developer entry point was Windows-only, which made the quickstart
# unrunnable for anyone on Linux or macOS.

set -euo pipefail

scenario="head-on-conflict"
tools=0
down=0

while [ $# -gt 0 ]; do
    case "$1" in
        --scenario) scenario="${2:?--scenario needs a value}"; shift 2 ;;
        --tools)    tools=1; shift ;;
        --down)     down=1; shift ;;
        -h|--help)  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)          echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."
compose="deploy/compose.yml"

if [ "$down" -eq 1 ]; then
    echo "Tearing down (including volumes)..."
    exec docker compose -f "$compose" --profile tools down -v
fi

scenario_file="scenarios/${scenario}.yaml"
if [ ! -f "$scenario_file" ]; then
    echo "No such scenario: $scenario_file" >&2
    echo "Available:" >&2
    for f in scenarios/*.yaml; do echo "  $(basename "$f" .yaml)" >&2; done
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo "Docker is not running. Start Docker and try again." >&2
    exit 1
fi

profile_args=()
[ "$tools" -eq 1 ] && profile_args=(--profile tools)

echo "Building images..."
docker compose -f "$compose" "${profile_args[@]+"${profile_args[@]}"}" build

echo "Starting stack with scenario '$scenario'..."
ACP_SCENARIO="$scenario_file" \
    docker compose -f "$compose" "${profile_args[@]+"${profile_args[@]}"}" up -d

# The API healthcheck is the readiness signal for the whole stack: it only
# passes once Redis and the migrations are done.
printf 'Waiting for the API'
for _ in $(seq 1 60); do
    if curl -fsS -m 2 http://localhost:8000/health > /dev/null 2>&1; then break; fi
    printf '.'
    sleep 1
done
printf '\n'

ready=$(curl -fsS http://localhost:8000/ready 2>/dev/null || echo '{}')
case "$ready" in
    *'"ready":true'*) ;;
    *)
        echo "API is up but not ready: $ready" >&2
        echo "Check logs with: docker compose -f $compose logs" >&2
        exit 1 ;;
esac

echo
echo "Stack is up."
echo "  Display        http://localhost:8000"
echo "  API docs       http://localhost:8000/docs"
echo "  Live tracks    http://localhost:8000/v1/tracks"
[ "$tools" -eq 1 ] && echo "  Kafka console  http://localhost:8080"
echo
echo "Tear down with: ./scripts/demo.sh --down"

for opener in xdg-open open; do
    command -v "$opener" > /dev/null 2>&1 && "$opener" http://localhost:8000 > /dev/null 2>&1 && break
done || true
