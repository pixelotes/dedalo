"""Planificador de tareas y rutinas en segundo plano para Dédalo.

Permite ejecutar periódicamente los flujos activos que tengan una expresión cron
(ej: `0 8 * * *` o `*/15 * * * *`) o intervalo en minutos (ej: `@every 30m`).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

from dedalo import compiler, cron, history, runtime, store
from dedalo.i18n import tr

RUN_HISTORY: dict[str, dict[str, Any]] = {}


@dataclass
class FlowContext:
    flow_name: str
    started_at: float = field(default_factory=time.time)
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    run_id: str = ""  # run en curso en el historial: los sub-flujos lo enlazan como padre
    stack: tuple[str, ...] = ()  # cadena de flujos que han llamado a este (detección de ciclos)


SUBFLOW_MAX_DEPTH = 3


def _should_run(schedule_expr: str, last_run: float | None) -> bool:
    """Comprueba si el flujo debe dispararse ahora según su schedule y último run."""
    return cron.should_run(schedule_expr, last_run)


def get_next_run(schedule_expr: str) -> str | None:
    """Devuelve la fecha estimada de la próxima ejecución o None si no es válida."""
    return cron.get_next_run(schedule_expr)


def run_flow_sync(flow_id: str, trigger_type: str = "schedule") -> dict[str, Any]:
    """Ejecuta el script .py compilado de un flujo de forma síncrona y almacena el resultado en el historial."""
    code_file = store.FLOWS_DIR / f"{flow_id}.py"
    if not code_file.is_file():
        raise FileNotFoundError(tr('The compiled script for "{flow_id}" does not exist', flow_id=flow_id))

    flow_meta = store.load_meta(flow_id) or {}
    flow_name = flow_meta.get("name") or flow_id

    run_id = history.record_run_start(
        flow_id=flow_id,
        flow_name=flow_name,
        trigger_type=trigger_type,
    )

    source = code_file.read_text(encoding="utf-8")
    ctx = FlowContext(flow_name=flow_id, run_id=run_id)

    # Espacio de nombres aislado para la ejecución
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, f"<dedalo:{flow_id}>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError(tr('The script of "{flow_id}" does not define a run(ctx) function', flow_id=flow_id))

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
    finally:
        runtime.compact_memory()


def run_subflow_sync(
    flow_id: str,
    *,
    payload: Any = None,
    parent: Any = None,
    skip_outputs: bool = True,
) -> dict[str, Any]:
    """Ejecuta un flujo como sub-flujo y devuelve {status, data, error, errors, duration, run_id}.

    El resultado son los datos de los nodos sumidero (sin aristas de salida): uno solo si hay uno, o un
    dict por nodo. Con `skip_outputs` se compila sin los nodos `output.*`, así que el flujo no envía
    mensajes ni escribe nada por su cuenta. Nunca lanza: los fallos vuelven con status "error".
    """
    fid = store.slug(flow_id)
    parent_id = getattr(parent, "flow_name", "")
    chain = tuple(getattr(parent, "stack", ())) + ((parent_id,) if parent_id else ())
    if fid in chain:
        return {"status": "error", "error": tr("Flow cycle detected: {chain}", chain=" → ".join(chain + (fid,)))}
    if len(chain) >= SUBFLOW_MAX_DEPTH:
        return {"status": "error", "error": tr("Maximum sub-flow depth ({depth}) exceeded", depth=SUBFLOW_MAX_DEPTH)}

    flow_data = store.load_flow(fid)
    if not flow_data:
        return {"status": "error", "error": tr('Flow "{flow_id}" not found', flow_id=fid)}
    try:
        graph = compiler.Graph.from_dict(flow_data.get("graph") or {})
        if skip_outputs:
            graph = compiler.without_outputs(graph)
        source = compiler.compile(graph)
    except Exception as exc:
        return {"status": "error", "error": tr("Error compiling flow: {exc}", exc=exc)}

    parent_run = getattr(parent, "run_id", "")
    run_id = history.record_run_start(
        flow_id=fid,
        flow_name=graph.name or fid,
        trigger_type=f"subflow:{parent_run}" if parent_run else "subflow",
    )
    ctx = FlowContext(flow_name=fid, payload=payload, run_id=run_id, stack=chain)
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, f"<dedalo:subflow:{fid}>", "exec"), ns)
        res = ns["run"](ctx)
    except Exception as exc:
        duration = time.time() - t0
        history.record_run_finish(run_id=run_id, status="error", duration=duration, error=str(exc))
        return {"status": "error", "error": str(exc), "duration": round(duration, 3), "run_id": run_id}

    duration = time.time() - t0
    history.record_run_finish(run_id=run_id, status="ok", duration=duration, results=res)

    sinks = [n.id for n in graph.nodes if not graph.outgoing(n.id)]
    ok = {s: res[s]["data"] for s in sinks if res.get(s, {}).get("status") == "ok"}
    failed = {n: r["error"] for n, r in res.items() if r.get("status") == "error"}
    out: dict[str, Any] = {"duration": round(duration, 3), "run_id": run_id}
    if failed:
        out["errors"] = failed
    if ok:
        return {**out, "status": "ok", "data": next(iter(ok.values())) if len(ok) == 1 else ok}
    failed_sinks = [failed[s] for s in sinks if s in failed]
    if failed_sinks:
        return {**out, "status": "error", "error": failed_sinks[0]}
    return {**out, "status": "skipped"}  # ningún sumidero produjo datos (p. ej. un umbral sin cumplir)


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

    def __init__(self, check_interval: float = 30.0, purge_interval: float = 86400.0, max_concurrent: int | None = None):
        self.check_interval = check_interval
        self.purge_interval = purge_interval
        if max_concurrent is None:
            try:
                max_concurrent = int(os.environ.get("DEDALO_MAX_CONCURRENT_FLOWS", "2"))
            except ValueError:
                max_concurrent = 2
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._running = False
        self._task: asyncio.Task | None = None
        self._runs: set[asyncio.Task] = set()
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
        for t in list(self._runs):
            t.cancel()
        if self._runs:
            await asyncio.gather(*self._runs, return_exceptions=True)

    async def _run_scheduled(self, fid: str) -> None:
        """Ejecuta un flujo planificado respetando el límite de concurrencia."""
        async with self._sem:
            try:
                res = await asyncio.to_thread(run_flow_sync, fid, "schedule")
                RUN_HISTORY[fid]["status"] = "ok"
                RUN_HISTORY[fid]["duration"] = res["duration"]
                RUN_HISTORY[fid]["last_success"] = time.time()
                RUN_HISTORY[fid]["run_id"] = res.get("run_id")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                RUN_HISTORY[fid]["status"] = "error"
                RUN_HISTORY[fid]["error"] = str(exc)

    async def _loop(self) -> None:
        while self._running:
            # Purga automática de ejecuciones de más de 30 días cada 24 horas (86400 segundos)
            if time.time() - self._last_purge >= self.purge_interval:
                try:
                    await asyncio.to_thread(history.purge_older_than, 30)
                    self._last_purge = time.time()
                    runtime.compact_memory()
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
                    if hist.get("status") == "running":
                        continue  # ya en ejecución: no solapar

                    if _should_run(sched, last_run):
                        RUN_HISTORY[fid] = {
                            "last_run": time.time(),
                            "status": "running",
                        }
                        task = asyncio.create_task(self._run_scheduled(fid))
                        self._runs.add(task)
                        task.add_done_callback(self._runs.discard)
            except Exception:
                pass

            await asyncio.sleep(self.check_interval)
