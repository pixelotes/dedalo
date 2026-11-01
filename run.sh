#!/usr/bin/env bash
#
# Starts Dédalo Standalone optimized for low memory usage.
#
# Options:
#   ./run.sh              -> Full server with web UI and memory optimizations
#   ./run.sh --headless   -> Web server without serving the graphical interface (Webhooks and API only)
#   ./run.sh --daemon     -> Pure daemon without an HTTP server (background Scheduler only)
#   ./run.sh --dev        -> Development mode with auto-reload (--reload)
#

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PORT="${DEDALO_PORT:-8080}"
HOST="${DEDALO_HOST:-0.0.0.0}"

# Memory allocator and Python compilation optimizations
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-65536}"
export PYTHONOPTIMIZE="${PYTHONOPTIMIZE:-1}"

# Command-line arguments
IS_HEADLESS="${DEDALO_HEADLESS:-0}"
IS_DAEMON=0
IS_DEV="${DEDALO_DEV:-0}"

for arg in "$@"; do
  case "$arg" in
    --headless)
      IS_HEADLESS=1
      ;;
    --daemon)
      IS_DAEMON=1
      ;;
    --dev)
      IS_DEV=1
      ;;
  esac
done

# Create the venv if it does not exist
if [ ! -d .venv ]; then
  echo "Creating virtual environment in .venv..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e .
fi

if [ "$IS_DAEMON" = "1" ]; then
  echo "Starting Dédalo in Pure Daemon mode (no web server)..."
  exec .venv/bin/python -m dedalo.headless
fi

if [ "$IS_HEADLESS" = "1" ]; then
  export DEDALO_HEADLESS=1
  echo "Starting Dédalo in Headless mode (Webhooks active at http://$HOST:$PORT, UI disabled)..."
else
  echo "Starting Dédalo at http://$HOST:$PORT ..."
fi

EXTRA_FLAGS=""
if [ "$IS_DEV" = "1" ]; then
  EXTRA_FLAGS="--reload"
fi

exec .venv/bin/uvicorn dedalo.app:app \
  --host "$HOST" \
  --port "$PORT" \
  --no-access-log \
  --limit-concurrency 15 \
  --timeout-keep-alive 5 \
  $EXTRA_FLAGS
