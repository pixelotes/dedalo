#!/usr/bin/env bash
#
# Para cualquier proceso de Dédalo corriendo en local
#

set -euo pipefail

PORT="${DEDALO_PORT:-8080}"
PIDS=$(lsof -ti :"$PORT" || true)

if [ -n "$PIDS" ]; then
  echo "Deteniendo Dédalo en puerto $PORT (PID: $PIDS)..."
  kill $PIDS
  echo "Dédalo detenido."
else
  echo "No hay ningún proceso escuchando en el puerto $PORT."
fi
