#!/usr/bin/env bash
#
# Stops any Dédalo process running locally
#

set -euo pipefail

PORT="${DEDALO_PORT:-8080}"
PIDS=$(lsof -ti :"$PORT" || true)

if [ -n "$PIDS" ]; then
  echo "Stopping Dédalo on port $PORT (PID: $PIDS)..."
  kill $PIDS
  echo "Dédalo stopped."
else
  echo "No process is listening on port $PORT."
fi
