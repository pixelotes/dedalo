"""Punto de entrada para ejecutar Dédalo en modo demonio puro sin servidor web.

Este modo consume únicamente ~12-16 MB de memoria RAM al no cargar FastAPI,
Uvicorn ni Starlette. Ejecuta el planificador de flujos en segundo plano
y purga el historial en SQLite.

Uso:
    python -m dedalo.headless
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

from dedalo import history, runtime, scheduler, secrets
from dedalo.i18n import tr


async def main() -> None:
    print(tr('Starting Dédalo in Pure Daemon mode (Headless Scheduler)...'))

    # Inicializar directorios y base de datos
    for d in ["data/flows", "data/secrets", "data/state", "data/outputs", "data/logs"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    history.init_db()
    secrets.migrate()

    sched = scheduler.Scheduler()
    await sched.start()
    print(tr('Dédalo Daemon active. Monitoring scheduled tasks. Press Ctrl+C to exit.'))

    stop_event = asyncio.Event()

    def _sig_handler():
        print(tr('\nStopping Dédalo Daemon...'))
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig_handler)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        await sched.stop()
        runtime.compact_memory()
        print(tr('Dédalo Daemon stopped successfully.'))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
