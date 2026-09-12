#!/usr/bin/env bash
#
# Arranca Dédalo Standalone optimizado para bajo consumo de memoria.
#
# Opciones:
#   ./run.sh              -> Servidor completo con UI web y optimizaciones de memoria
#   ./run.sh --headless   -> Servidor web sin servir interfaz gráfica (solo Webhooks y API)
#   ./run.sh --daemon     -> Demonio puro sin servidor HTTP (solo Scheduler en segundo plano)
#   ./run.sh --dev        -> Modo desarrollo con auto-recarga (--reload)
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

# Optimizaciones de asignador de memoria y compilación de Python
export MALLOC_TRIM_THRESHOLD_="${MALLOC_TRIM_THRESHOLD_:-65536}"
export PYTHONOPTIMIZE="${PYTHONOPTIMIZE:-1}"

# Argumentos de línea de comandos
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

# Crear venv si no existe
if [ ! -d .venv ]; then
  echo "Creando entorno virtual en .venv..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -e .
fi

if [ "$IS_DAEMON" = "1" ]; then
  echo "Iniciando Dédalo en modo Demonio Puro (sin servidor web)..."
  exec .venv/bin/python -m dedalo.headless
fi

if [ "$IS_HEADLESS" = "1" ]; then
  export DEDALO_HEADLESS=1
  echo "Iniciando Dédalo en modo Headless (Webhooks activos en http://$HOST:$PORT, UI desactivada)..."
else
  echo "Iniciando Dédalo en http://$HOST:$PORT ..."
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
