"""Planificador de tareas y rutinas en segundo plano para Dédalo.

Permite ejecutar periódicamente los flujos activos que tengan una expresión cron
(ej: `0 8 * * *` o `*/15 * * * *`) o intervalo en minutos (ej: `@every 30m`).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from croniter import croniter

from dedalo import history, store

RUN_HISTORY: dict[str, dict[str, Any]] = {}


@dataclass
class FlowContext:
    flow_name: str
    started_at: float = field(default_factory=time.time)


def _should_run(schedule_expr: str, last_run: float | None) -> bool:
    """Comprueba si el flujo debe dispararse ahora según su schedule y último run."""
    now = datetime.now()
    expr = (schedule_expr or "").strip()
    if not expr:
        return False

    # Intervalo simple: @every 30m, 1h, etc.
    m_int = re.match(r"^(?:@every\s+)?(\d+)\s*([mh])$", expr, re.IGNORECASE)
    if m_int:
        amount = int(m_int.group(1))
        unit = m_int.group(2).lower()
        delta_sec = amount * 60 if unit == "m" else amount * 3600
        if last_run is None:
            return True
        return (time.time() - last_run) >= delta_sec

    # Formato Cron estándar (5 campos)
    try:
        if last_run is None:
            # Si nunca ha corrido, comprobamos si tocó en el último minuto
            base_time = datetime.fromtimestamp(time.time() - 60)
        else:
            base_time = datetime.fromtimestamp(last_run)

        iter_cron = croniter(expr, base_time)
        return next_run <= now
    except Exception:
        return False


def get_next_run(schedule_expr: str) -> str | None:
    """Devuelve la fecha estimada de la próxima ejecución o None si no es válida."""
    expr = (schedule_expr or "").strip()
    if not expr:
        return None

    m_int = re.match(r"^(?:@every\s+)?(\d+)\s*([mh])$", expr, re.IGNORECASE)
    if m_int:
        amount = int(m_int.group(1))
        unit = m_int.group(2).lower()
        delta_sec = amount * 60 if unit == "m" else amount * 3600
        next_dt = datetime.fromtimestamp(time.time() + delta_sec)
        return next_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        iter_cron = croniter(expr, datetime.now())
        return iter_cron.get_next(datetime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def run_flow_sync(flow_id: str, trigger_type: str = "schedule") -> dict[str, Any]:
    """Ejecuta el script .py compilado de un flujo de forma síncrona y almacena el resultado en el historial."""
    code_file = store.FLOWS_DIR / f"{flow_id}.py"
    if not code_file.is_file():
        raise FileNotFoundError(f"No existe el script compilado para «{flow_id}»")

    flow_meta = store.load_meta(flow_id) or {}
    flow_name = flow_meta.get("name") or flow_id

    run_id = history.record_run_start(
        flow_id=flow_id,
        flow_name=flow_name,
        trigger_type=trigger_type,
    )

    source = code_file.read_text(encoding="utf-8")
    ctx = FlowContext(flow_name=flow_id)

    # Espacio de nombres aislado para la ejecución
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, f"<dedalo:{flow_id}>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError(f"El script de «{flow_id}» no define una función run(ctx)")

        res = run_fn(ctx)
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="ok",
            duration=duration,
            results=res,
        )
        return {
            "run_id": run_id,
            "flow_id": flow_id,
            "duration": round(duration, 3),
            "result": res,
        }
    except Exception as exc:
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="error",
            duration=duration,
            error=str(exc),
        )
        raise


async def trigger_flow(flow_id: str, trigger_type: str = "manual") -> dict[str, Any]:
    """Ejecuta un flujo inmediatamente en segundo plano y actualiza el historial."""
    RUN_HISTORY[flow_id] = {
        "last_run": time.time(),
        "status": "running",
    }
    try:
        res = await asyncio.to_thread(run_flow_sync, flow_id, trigger_type)
        RUN_HISTORY[flow_id]["status"] = "ok"
        RUN_HISTORY[flow_id]["duration"] = res["duration"]
        RUN_HISTORY[flow_id]["last_success"] = time.time()
        RUN_HISTORY[flow_id]["result"] = res["result"]
        RUN_HISTORY[flow_id]["run_id"] = res.get("run_id")
        return res
    except Exception as exc:
        RUN_HISTORY[flow_id]["status"] = "error"
        RUN_HISTORY[flow_id]["error"] = str(exc)
        raise


class Scheduler:
    """Motor de planificación periódica en segundo plano."""

    def __init__(self, check_interval: float = 30.0, purge_interval: float = 86400.0):
        self.check_interval = check_interval
        self.purge_interval = purge_interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_purge: float = 0.0

    async def start(self) -> None:
        self._running = True
        # Purga preventiva al iniciar
        try:
            await asyncio.to_thread(history.purge_older_than, 30)
            self._last_purge = time.time()
        except Exception:
            pass
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            # Purga automática de ejecuciones de más de 30 días cada 24 horas (86400 segundos)
            if time.time() - self._last_purge >= self.purge_interval:
                try:
                    await asyncio.to_thread(history.purge_older_than, 30)
                    self._last_purge = time.time()
                except Exception:
                    pass

            try:
                flows = store.list_flows()
                for fl in flows:
                    if not fl.get("enabled"):
                        continue
                    sched = fl.get("schedule", "")
                    if not sched:
                        continue

                    fid = fl["id"]
                    hist = RUN_HISTORY.get(fid, {})
                    last_run = hist.get("last_run")

                    if _should_run(sched, last_run):
                        RUN_HISTORY[fid] = {
                            "last_run": time.time(),
                            "status": "running",
                        }
                        try:
                            res = await asyncio.to_thread(run_flow_sync, fid, "schedule")
                            RUN_HISTORY[fid]["status"] = "ok"
                            RUN_HISTORY[fid]["duration"] = res["duration"]
                            RUN_HISTORY[fid]["last_success"] = time.time()
                            RUN_HISTORY[fid]["run_id"] = res.get("run_id")
                        except Exception as exc:
                            RUN_HISTORY[fid]["status"] = "error"
                            RUN_HISTORY[fid]["error"] = str(exc)
            except Exception:
                pass

            await asyncio.sleep(self.check_interval)
