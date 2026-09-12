"""Servidor web y API FastAPI para Dédalo Standalone."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dedalo import compiler, history, runtime, scheduler, secrets, store, templates

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = Path("data/config.json")

sched = scheduler.Scheduler()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar directorios de datos y base de datos
    for d in ["data/flows", "data/secrets", "data/state", "data/outputs", "data/logs"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    history.init_db()

    # Cargar configuración persistida si existe
    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("llm_url"):
                os.environ["DEDALO_LLM_URL"] = cfg["llm_url"]
            if cfg.get("llm_model"):
                os.environ["DEDALO_LLM_MODEL"] = cfg["llm_model"]
            if cfg.get("llm_key"):
                os.environ["DEDALO_LLM_KEY"] = cfg["llm_key"]
        except Exception:
            pass

    await sched.start()
    yield
    await sched.stop()


app = FastAPI(title="Dédalo Standalone", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir archivos estáticos
VENDOR_DIR = STATIC_DIR / "vendor"
VENDOR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


# ---------------------------------------------------------------------------
# Modelos Pydantic para la API
# ---------------------------------------------------------------------------


class FlowSaveRequest(BaseModel):
    id: str
    graph: dict[str, Any]
    meta: dict[str, Any] = Field(default_factory=dict)
    force: bool = False


class CompileRequest(BaseModel):
    graph: dict[str, Any]


class RunRequest(BaseModel):
    graph: dict[str, Any]


class SecretSaveRequest(BaseModel):
    name: str
    value: str


class ConfigUpdateRequest(BaseModel):
    llm_url: str = ""
    llm_model: str = ""
    llm_key: str = ""


# ---------------------------------------------------------------------------
# Rutas Principales y Vistas
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
@app.get("/dedalo", response_class=HTMLResponse)
def index_view() -> HTMLResponse:
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        return HTMLResponse("<h1>Dédalo: Error al cargar static/index.html</h1>", status_code=500)
    html = index_file.read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# API: Catálogo, Plantillas y Configuración
# ---------------------------------------------------------------------------


@app.get("/api/catalog")
def get_catalog() -> dict[str, Any]:
    """Devuelve el catálogo de tipos de nodo disponibles y sus opciones dinámicas."""
    types_dict = {}
    for k, spec in compiler.NODE_TYPES.items():
        types_dict[k] = {
            "category": spec.category,
            "label": spec.label,
            "output": spec.output,
            "required": sorted(spec.required),
            "optional": spec.optional,
            "code_field": spec.code_field,
            "choices": spec.choices,
            "choices_from": spec.choices_from,
            "show_when": spec.show_when,
            "help": spec.help,
        }

    return {
        "node_types": types_dict,
        "choices": {
            "secrets": [{"value": s, "label": s} for s in secrets.names()],
        },
    }


@app.get("/api/templates")
def get_templates() -> list[dict]:
    return templates.list_templates()


@app.get("/api/template")
def get_template(name: str = Query(...)) -> dict:
    g = templates.get_template(name)
    if not g:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    return g


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "llm_url": os.environ.get("DEDALO_LLM_URL", "http://localhost:11434/v1"),
        "llm_model": os.environ.get("DEDALO_LLM_MODEL", "llama3"),
        "has_llm_key": bool(os.environ.get("DEDALO_LLM_KEY")),
    }


@app.post("/api/config")
def save_config(req: ConfigUpdateRequest) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    if req.llm_url:
        os.environ["DEDALO_LLM_URL"] = req.llm_url
        cfg["llm_url"] = req.llm_url
    if req.llm_model:
        os.environ["DEDALO_LLM_MODEL"] = req.llm_model
        cfg["llm_model"] = req.llm_model
    if req.llm_key is not None and req.llm_key != "":
        os.environ["DEDALO_LLM_KEY"] = req.llm_key
        cfg["llm_key"] = req.llm_key

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API: Gestión de Secretos
# ---------------------------------------------------------------------------


@app.get("/api/secrets")
def list_secrets() -> list[str]:
    return secrets.names()


@app.post("/api/secrets")
def set_secret(req: SecretSaveRequest) -> dict[str, str]:
    try:
        secrets.set(req.name, req.value)
        return {"status": "ok", "name": req.name}
    except secrets.SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/secrets")
def remove_secret(name: str = Query(...)) -> dict[str, Any]:
    deleted = secrets.delete(name)
    return {"status": "ok", "deleted": deleted}


# ---------------------------------------------------------------------------
# API: Gestión de Flujos
# ---------------------------------------------------------------------------


class ScheduleUpdateRequest(BaseModel):
    id: str
    schedule: str = ""
    enabled: bool = True


@app.get("/api/flows")
def get_flows() -> list[dict[str, Any]]:
    flows = store.list_flows()
    for fl in flows:
        fid = fl["id"]
        if fid in scheduler.RUN_HISTORY:
            fl["last_run"] = scheduler.RUN_HISTORY[fid]
        fl["next_run"] = (
            scheduler.get_next_run(fl.get("schedule", ""))
            if fl.get("enabled")
            else None
        )
    return flows


@app.get("/api/flow")
def get_flow(id: str = Query(...)) -> dict[str, Any]:
    flow_data = store.load_flow(id)
    if not flow_data:
        raise HTTPException(status_code=404, detail=f"Flujo «{id}» no encontrado")
    meta = flow_data.get("meta", {})
    flow_data["next_run"] = (
        scheduler.get_next_run(meta.get("schedule", ""))
        if meta.get("enabled")
        else None
    )
    if id in scheduler.RUN_HISTORY:
        flow_data["last_run"] = scheduler.RUN_HISTORY[id]
    return flow_data


@app.post("/api/flow")
def save_flow_endpoint(req: FlowSaveRequest) -> dict[str, Any]:
    try:
        res = store.save_flow(req.id, req.graph, req.meta, force=req.force)
        return {"status": "ok", **res}
    except store.DriftError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except compiler.GraphError as exc:
        raise HTTPException(status_code=400, detail=exc.errors)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al guardar flujo: {exc}")


@app.post("/api/flow/schedule")
def update_schedule_endpoint(req: ScheduleUpdateRequest) -> dict[str, Any]:
    meta = store.update_meta(req.id, {"schedule": req.schedule.strip(), "enabled": req.enabled})
    return {
        "status": "ok",
        "meta": meta,
        "next_run": scheduler.get_next_run(req.schedule) if req.enabled else None,
    }


@app.post("/api/flow/toggle")
def toggle_flow_endpoint(id: str = Query(...)) -> dict[str, Any]:
    meta = store.toggle_flow(id)
    return {
        "status": "ok",
        "meta": meta,
        "next_run": scheduler.get_next_run(meta.get("schedule", "")) if meta.get("enabled") else None,
    }


@app.post("/api/flow/run_now")
async def run_flow_now_endpoint(id: str = Query(...)) -> dict[str, Any]:
    try:
        res = await scheduler.trigger_flow(id)
        return {"status": "ok", **res}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al ejecutar flujo: {exc}")


@app.delete("/api/flow")
def delete_flow_endpoint(id: str = Query(...)) -> dict[str, Any]:
    deleted = store.delete_flow(id)
    return {"status": "ok", "deleted": deleted}


# ---------------------------------------------------------------------------
# API: Compilación y Ejecución en Pruebas
# ---------------------------------------------------------------------------


@app.post("/api/compile")
def compile_endpoint(req: CompileRequest) -> dict[str, Any]:
    try:
        graph = compiler.Graph.from_dict(req.graph)
        code = compiler.compile(graph)
        return {"status": "ok", "code": code}
    except compiler.GraphError as exc:
        raise HTTPException(status_code=400, detail=exc.errors)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error al compilar: {exc}")


@app.post("/api/run")
def run_preview_endpoint(req: RunRequest) -> dict[str, Any]:
    """Compila y ejecuta el grafo inmediatamente en memoria para depuración visual y registra la ejecución."""
    try:
        graph = compiler.Graph.from_dict(req.graph)
        source = compiler.compile(graph)
    except compiler.GraphError as exc:
        raise HTTPException(status_code=400, detail=exc.errors)

    flow_id = store.slug(graph.name) if graph.name else "prueba"
    flow_name = graph.name or "Prueba rápida"
    run_id = history.record_run_start(flow_id=flow_id, flow_name=flow_name, trigger_type="preview")

    ctx = scheduler.FlowContext(flow_name=flow_id)
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, "<dedalo:preview>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError("El flujo compilado no contiene función run()")
        results = run_fn(ctx)
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="ok",
            duration=duration,
            results=results,
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "duration": round(duration, 3),
            "results": results,
            "code": source,
        }
    except Exception as exc:
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="error",
            duration=duration,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Error durante la ejecución: {exc}")


@app.api_route("/api/webhook/{flow_id}", methods=["GET", "POST", "PUT"])
async def webhook_endpoint(flow_id: str, request: Request) -> dict[str, Any]:
    """Recibe una llamada externa para disparar un flujo por webhook."""
    flow_data = store.load_flow(flow_id)
    if not flow_data:
        raise HTTPException(status_code=404, detail=f"Flujo «{flow_id}» no encontrado")

    raw_body = await request.body()
    body_data: Any = None
    if raw_body:
        try:
            body_data = json.loads(raw_body)
        except Exception:
            body_data = raw_body.decode("utf-8", errors="replace")
    elif request.query_params:
        body_data = dict(request.query_params)
    else:
        body_data = {}

    req_headers = dict(request.headers)

    graph_dict = flow_data.get("graph") or {}
    try:
        graph = compiler.Graph.from_dict(graph_dict)
        source = compiler.compile(graph)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Error al compilar flujo: {exc}")

    flow_name = graph.name or flow_id
    run_id = history.record_run_start(flow_id=flow_id, flow_name=flow_name, trigger_type="webhook")

    ctx = scheduler.FlowContext(
        flow_name=flow_id,
        payload=body_data,
        headers=req_headers,
    )
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, f"<dedalo:webhook:{flow_id}>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError("El flujo compilado no contiene función run()")
        results = run_fn(ctx)
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="ok",
            duration=duration,
            results=results,
        )
        return {
            "status": "ok",
            "flow_id": flow_id,
            "run_id": run_id,
            "duration": round(duration, 3),
            "results": results,
        }
    except Exception as exc:
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="error",
            duration=duration,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"Error durante la ejecución del webhook: {exc}")


# ---------------------------------------------------------------------------
# API: Historial de Ejecuciones
# ---------------------------------------------------------------------------


@app.get("/api/history")
def list_history_endpoint(flow_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Devuelve las últimas ejecuciones registradas."""
    return history.list_runs(flow_id=flow_id, limit=limit)


@app.get("/api/history/run")
def get_history_run_endpoint(id: str = Query(...)) -> dict[str, Any]:
    """Devuelve los detalles y resultados paso a paso de una ejecución concreta."""
    run_data = history.get_run(id)
    if not run_data:
        raise HTTPException(status_code=404, detail=f"Ejecución «{id}» no encontrada")
    return run_data


@app.post("/api/history/purge")
def purge_history_endpoint(days: int = 30) -> dict[str, Any]:
    """Purga ejecuciones con antigüedad superior a los días especificados (por defecto 30)."""
    deleted = history.purge_older_than(days=days)
    return {"status": "ok", "deleted": deleted, "days": days}

