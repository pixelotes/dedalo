#!/usr/bin/env bash
#
# Arranca Dédalo Standalone
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

# Crear venv si no existe
if [ ! -d .venv ]; then
  echo "Creando entorno virtual en .venv..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e .
fi

echo "Iniciando Dédalo en http://$HOST:$PORT ..."
exec .venv/bin/uvicorn dedalo.app:app --host "$HOST" --port "$PORT" --reload
