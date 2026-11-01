"""Servidor web y API FastAPI para Dédalo Standalone."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import io
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dedalo import compiler, history, i18n, llms, runtime, sandbox, scheduler, secrets, store, templates
from dedalo.i18n import tr
from dedalo.security import (
    BodyLimitMiddleware,
    SameOriginMiddleware,
    SecurityHeadersMiddleware,
    webhook_token_ok,
)

log = logging.getLogger("dedalo.app")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = Path("data/config.json")

sched = scheduler.Scheduler()


def _internal_error(msg: str, exc: Exception) -> HTTPException:
    """Registra la excepción en el log y devuelve un 500 sin detalles internos (rutas, trazas)."""
    log.error("%s: %s", msg, exc, exc_info=exc)
    return HTTPException(status_code=500, detail=tr('{msg} (check the server logs)', msg=msg))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializar directorios de datos y base de datos
    for d in ["data/flows", "data/secrets", "data/llms", "data/state", "data/outputs", "data/logs"]:
        Path(d).mkdir(parents=True, exist_ok=True)
    history.init_db()
    secrets.migrate()

    # Cargar configuración persistida si existe
    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if cfg.get("llm_url"):
                os.environ["DEDALO_LLM_URL"] = cfg["llm_url"]
            if cfg.get("llm_model"):
                os.environ["DEDALO_LLM_MODEL"] = cfg["llm_model"]
            if cfg.get("llm_key"):
                os.environ["DEDALO_LLM_KEY"] = secrets.decrypt(cfg["llm_key"])
                if not cfg["llm_key"].startswith(secrets.PREFIX):  # formato antiguo: cifrar en el sitio
                    cfg["llm_key"] = secrets.encrypt(cfg["llm_key"])
                    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                    CONFIG_FILE.chmod(0o600)
        except Exception:
            pass

    await sched.start()
    yield
    await sched.stop()


app = FastAPI(title=tr('Dédalo Standalone'), version="0.1.0", lifespan=lifespan)

# Sin CORS: la interfaz se sirve desde el mismo origen que la API. Para evitar DNS rebinding y CSRF
# se valida la cabecera Host (DEDALO_ALLOWED_HOSTS, separada por comas; admite "*.dominio" y "*").
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DEDALO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if h.strip()
]
MAX_BODY_BYTES = int(float(os.environ.get("DEDALO_MAX_BODY_MB", "25")) * 1024 * 1024)

# Orden: el último añadido es la capa más externa (cabeceras > host > límite de cuerpo > mismo origen).
app.add_middleware(i18n.LangMiddleware)
app.add_middleware(SameOriginMiddleware)
app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(SecurityHeadersMiddleware)

HEADLESS = os.environ.get("DEDALO_HEADLESS", "").strip().lower() in ("1", "true", "yes")

# Servir archivos estáticos únicamente si la interfaz gráfica está habilitada
if not HEADLESS:
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


class NodePreviewRequest(BaseModel):
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    input_data: Any = None
    code: str = ""
    flow_id: str = "preview"


class FlowStepRequest(BaseModel):
    graph: dict[str, Any]
    target_node_id: str
    cached_results: dict[str, Any] = Field(default_factory=dict)
    mode: str = "step"  # "step" | "run_to"


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


@app.get("/health")
def health_endpoint() -> dict[str, Any]:
    """Estado de salud y modo de operación del servidor."""
    return {
        "status": "ok",
        "service": "dedalo",
        "version": "0.1.0",
        "mode": "headless" if HEADLESS else "full",
        "scheduler": sched._running,
    }


@app.get("/")
@app.get("/dedalo")
def index_view():
    """Vista principal o respuesta minimalista si está en modo headless."""
    if HEADLESS:
        return {
            "status": "ok",
            "service": "dedalo",
            "mode": "headless",
            "message": tr('Dédalo running in headless mode. Webhooks and scheduler active.'),
        }
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        return HTMLResponse(tr('<h1>Dédalo: Error loading static/index.html</h1>'), status_code=500)
    html = index_file.read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# API: Catálogo, Plantillas y Configuración
# ---------------------------------------------------------------------------


@app.get("/api/catalog")
def get_catalog() -> dict[str, Any]:
    """Devuelve el catálogo de tipos de nodo disponibles y sus opciones dinámicas."""
    gt = i18n.gettext

    def tr_choice(c: Any) -> Any:
        # Solo se traduce la etiqueta; un valor suelto (str) es a la vez valor y etiqueta y no se toca.
        return {**c, "label": gt(c["label"])} if isinstance(c, dict) and "label" in c else c

    types_dict = {}
    for k, spec in compiler.NODE_TYPES.items():
        types_dict[k] = {
            "category": spec.category,
            "label": gt(spec.label),
            "output": gt(spec.output),
            "required": sorted(spec.required),
            "optional": spec.optional,
            "code_field": spec.code_field,
            "choices": {f: [tr_choice(c) for c in cs] for f, cs in spec.choices.items()},
            "choices_from": spec.choices_from,
            "show_when": spec.show_when,
            "help": {f: gt(h) for f, h in spec.help.items()},
        }

    return {
        "node_types": types_dict,
        "choices": {
            "secrets": [{"value": s, "label": s} for s in secrets.names()],
            "llms": [{"value": m["name"], "label": f"{m['name']} ({m.get('model', '')})"} for m in llms.list_all()],
        },
    }


@app.get("/api/templates")
def get_templates() -> list[dict]:
    return i18n.translate_tree(templates.list_templates())


@app.get("/api/template")
def get_template(name: str = Query(...)) -> dict:
    g = templates.get_template(name)
    if not g:
        raise HTTPException(status_code=404, detail=tr('Template not found'))
    return g


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "llm_url": os.environ.get("DEDALO_LLM_URL", "http://localhost:11434/v1"),
        "llm_model": os.environ.get("DEDALO_LLM_MODEL", "llama3"),
        "has_llm_key": bool(os.environ.get("DEDALO_LLM_KEY")),
        "sandbox": sandbox.status(),
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
        cfg["llm_key"] = secrets.encrypt(req.llm_key)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    CONFIG_FILE.chmod(0o600)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# API: Gestión de Endpoints LLM
# ---------------------------------------------------------------------------


class LLMSaveRequest(BaseModel):
    name: str = Field(..., description=tr('Unique identifier name of the endpoint'))
    provider: str = Field(default="custom", description='Provider (ollama, vllm, openrouter, etc.)')
    url: str = Field(default="http://localhost:11434/v1", description=tr('Base URL of the API'))
    model: str = Field(default="llama3", description=tr('Model name'))
    secret: str = Field(default="", description=tr('Name of the secret holding the API Key'))
    max_context: int = Field(default=0, description=tr('Context window limit (0 for auto-detection)'))
    temperature: float = Field(default=0.7, description=tr('Default temperature'))


@app.get("/api/llms")
def list_llms() -> list[dict[str, Any]]:
    return llms.list_all()


@app.get("/api/llms/{name}")
def get_llm(name: str) -> dict[str, Any]:
    conf = llms.get(name)
    if not conf:
        raise HTTPException(status_code=404, detail=tr("LLM endpoint not found: '{name}'", name=name))
    return conf


@app.post("/api/llms")
def set_llm(req: LLMSaveRequest) -> dict[str, Any]:
    try:
        saved = llms.set_llm(req.name, req.model_dump())
        return {"status": "ok", "llm": saved}
    except llms.LLMConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/api/llms")
def remove_llm_query(name: str = Query(...)) -> dict[str, Any]:
    deleted = llms.delete(name)
    return {"status": "ok", "deleted": deleted}


@app.delete("/api/llms/{name}")
def remove_llm_path(name: str) -> dict[str, Any]:
    deleted = llms.delete(name)
    return {"status": "ok", "deleted": deleted}


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
        raise HTTPException(status_code=404, detail=tr('Flow "{id}" not found', id=id))
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
        raise _internal_error(tr('Error saving flow'), exc)


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
        raise HTTPException(status_code=500, detail=tr('Error running flow: {exc}', exc=exc))


@app.delete("/api/flow")
def delete_flow_endpoint(id: str = Query(...)) -> dict[str, Any]:
    deleted = store.delete_flow(id)
    return {"status": "ok", "deleted": deleted}


@app.get("/api/flow/export")
@app.get("/api/flows/{flow_id}/export")
def export_flow_endpoint(flow_id: str = "", id: str = "") -> Response:
    target_id = flow_id or id
    if not target_id:
        raise HTTPException(status_code=400, detail=tr('The identifier of the flow to export is missing.'))
    try:
        zip_bytes = store.export_flow_zip(target_id)
        filename = f"{store.slug(target_id)}.zip"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=tr('Flow "{target_id}" not found.', target_id=target_id))
    except Exception as exc:
        raise _internal_error(tr('Error exporting flow'), exc)


@app.get("/api/flow/export-py")
@app.get("/api/flows/{flow_id}/export-py")
def export_flow_py_endpoint(flow_id: str = "", id: str = "") -> Response:
    target_id = flow_id or id
    if not target_id:
        raise HTTPException(status_code=400, detail=tr('The identifier of the flow to export is missing.'))
    try:
        py_source = store.export_flow_standalone_py(target_id)
        filename = f"{store.slug(target_id)}.py"
        return Response(
            content=py_source,
            media_type="text/x-python",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=tr('Flow "{target_id}" not found.', target_id=target_id))
    except Exception as exc:
        raise _internal_error(tr('Error exporting flow script'), exc)



@app.get("/api/flows/export-all")
def export_all_flows_endpoint() -> Response:
    try:
        zip_bytes = store.export_all_flows_zip()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"dedalo_backup_{timestamp}.zip"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise _internal_error(tr('Error exporting flow backup'), exc)


@app.post("/api/flows/import")
async def import_flows_endpoint(request: Request) -> dict[str, Any]:
    try:
        content_type = request.headers.get("content-type", "")
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail=tr('No data received to import.'))

        zip_bytes = body
        if "multipart/form-data" in content_type:
            boundary = None
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part.split("boundary=")[-1].strip().strip('"').encode()
            if boundary:
                delimiter = b"--" + boundary
                chunks = body.split(delimiter)
                for chunk in chunks:
                    if b"\r\n\r\n" in chunk:
                        hdr, payload = chunk.split(b"\r\n\r\n", 1)
                        payload = payload.rstrip(b"\r\n").rstrip(b"--")
                        if b"filename=" in hdr or zipfile.is_zipfile(io.BytesIO(payload)):
                            zip_bytes = payload
                            break

        res = store.import_flows_from_zip(zip_bytes, overwrite=True)
        if res.get("status") == "error":
            raise HTTPException(status_code=400, detail=res.get("error", "Error importando flujos."))
        return res
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error(tr('Error importing flows'), exc)


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
        raise _internal_error(tr('Error compiling'), exc)


@app.post("/api/run")
def run_preview_endpoint(req: RunRequest) -> dict[str, Any]:
    """Compila y ejecuta el grafo inmediatamente en memoria para depuración visual y registra la ejecución."""
    try:
        graph = compiler.Graph.from_dict(req.graph)
        source = compiler.compile(graph)
    except compiler.GraphError as exc:
        raise HTTPException(status_code=400, detail=exc.errors)

    flow_id = store.slug(graph.name) if graph.name else "prueba"
    flow_name = graph.name or tr('Quick test')
    run_id = history.record_run_start(flow_id=flow_id, flow_name=flow_name, trigger_type="preview")

    ctx = scheduler.FlowContext(flow_name=flow_id)
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, "<dedalo:preview>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError(tr('The compiled flow does not contain a run() function'))
        raw_results = run_fn(ctx)
        duration = time.time() - t0

        # Enriquecer resultados con las entradas recibidas por cada nodo
        enriched_results = {}
        for node_id, r in raw_results.items():
            incoming_sources = [e.source for e in graph.incoming(node_id)]
            inputs_map = {}
            for s in incoming_sources:
                if s in raw_results and raw_results[s].get("status") == "ok":
                    inputs_map[s] = raw_results[s].get("data")
            node_inputs = inputs_map if len(incoming_sources) > 1 else (
                inputs_map.get(incoming_sources[0]) if incoming_sources else None
            )
            enriched_results[node_id] = {
                **r,
                "inputs": node_inputs,
            }

        history.record_run_finish(
            run_id=run_id,
            status="ok",
            duration=duration,
            results=enriched_results,
            truncate=False,  # Datos completos no truncados para depuración interactiva
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "duration": round(duration, 3),
            "results": enriched_results,
            "code": source,
        }
    except Exception as exc:
        duration = time.time() - t0
        history.record_run_finish(
            run_id=run_id,
            status="error",
            duration=duration,
            error=str(exc),
            truncate=False,
        )
        raise HTTPException(status_code=500, detail=tr('Error during execution: {exc}', exc=exc))
    finally:
        runtime.compact_memory()


@app.post("/api/node/preview")
def node_preview_endpoint(req: NodePreviewRequest) -> dict[str, Any]:
    """Ejecuta un único nodo en memoria con los datos de entrada y configuración recibidos."""
    try:
        res = runtime.execute_single_node(
            node_type=req.type,
            config=req.config,
            inputs=req.input_data,
            code=req.code,
            flow_name=req.flow_id,
        )
        return {
            "status": res.status,
            "data": res.data,
            "error": res.error,
            "duration_ms": res.duration,
        }
    except Exception as exc:
        return {
            "status": "error",
            "data": None,
            "error": tr('Error running node: {exc}', exc=exc),
            "duration_ms": 0.0,
        }
    finally:
        runtime.compact_memory()


@app.post("/api/flow/step")
def flow_step_endpoint(req: FlowStepRequest) -> dict[str, Any]:
    """Ejecuta un solo paso de depuración o corre la cadena de dependencias hasta un nodo específico."""
    try:
        graph = compiler.Graph.from_dict(req.graph)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=tr('Invalid graph: {exc}', exc=exc))

    target_node = graph.node(req.target_node_id)
    if not target_node:
        raise HTTPException(status_code=404, detail=tr('Node "{target_node_id}" not found in the graph.', target_node_id=req.target_node_id))

    flow_id = store.slug(graph.name) if graph.name else "debug"
    ctx = scheduler.FlowContext(flow_name=flow_id)

    try:
        if req.mode == "step":
            incoming_edges = graph.incoming(req.target_node_id)
            parent_results = []
            inputs_map = {}
            for edge in incoming_edges:
                cached = req.cached_results.get(edge.source)
                if cached:
                    parent_results.append(cached)
                    inputs_map[edge.source] = cached.get("data")
                else:
                    parent_results.append({"status": "ok", "data": None})

            node_inputs = inputs_map if len(incoming_edges) > 1 else (
                inputs_map.get(incoming_edges[0].source) if incoming_edges else None
            )

            spec = compiler.NODE_TYPES.get(target_node.type)
            code_field = spec.code_field if spec else ""
            code = target_node.config.get(code_field, "") if code_field else ""

            res = runtime.execute_single_node(
                node_type=target_node.type,
                config=target_node.config,
                inputs=parent_results,
                code=code,
                flow_name=flow_id,
                ctx=ctx,
            )

            step_res = {
                "node_id": req.target_node_id,
                "status": res.status,
                "data": res.data,
                "error": res.error,
                "inputs": node_inputs,
                "duration_ms": res.duration,
            }
            all_results = dict(req.cached_results)
            all_results[req.target_node_id] = step_res

            return {
                "status": "ok",
                "node_id": req.target_node_id,
                "step": step_res,
                "all_results": all_results,
            }

        elif req.mode == "run_to":
            nodes_to_run = compiler.get_upstream_nodes(graph, req.target_node_id)
            if not nodes_to_run:
                raise HTTPException(status_code=400, detail=tr('Could not determine the execution order.'))

            all_results = dict(req.cached_results)
            last_step = None

            for node in nodes_to_run:
                incoming_edges = graph.incoming(node.id)
                parent_results = []
                inputs_map = {}
                for edge in incoming_edges:
                    cached = all_results.get(edge.source)
                    if cached:
                        parent_results.append(cached)
                        inputs_map[edge.source] = cached.get("data")
                    else:
                        parent_results.append({"status": "ok", "data": None})

                node_inputs = inputs_map if len(incoming_edges) > 1 else (
                    inputs_map.get(incoming_edges[0].source) if incoming_edges else None
                )

                spec = compiler.NODE_TYPES.get(node.type)
                code_field = spec.code_field if spec else ""
                code = node.config.get(code_field, "") if code_field else ""

                res = runtime.execute_single_node(
                    node_type=node.type,
                    config=node.config,
                    inputs=parent_results,
                    code=code,
                    flow_name=flow_id,
                    ctx=ctx,
                )

                step_res = {
                    "node_id": node.id,
                    "status": res.status,
                    "data": res.data,
                    "error": res.error,
                    "inputs": node_inputs,
                    "duration_ms": res.duration,
                }
                all_results[node.id] = step_res
                last_step = step_res

                if res.status == "error":
                    break

            return {
                "status": "ok",
                "target_node_id": req.target_node_id,
                "step": last_step,
                "all_results": all_results,
            }
        else:
            raise HTTPException(status_code=400, detail=tr("Unknown mode: '{mode}'", mode=req.mode))
    finally:
        runtime.compact_memory()


def _run_webhook_sync(flow_id: str, graph_dict: dict[str, Any], ctx: scheduler.FlowContext) -> dict[str, Any]:
    """Compila y ejecuta el flujo del webhook. Corre en un hilo para no bloquear el event loop."""
    try:
        graph = compiler.Graph.from_dict(graph_dict)
        source = compiler.compile(graph)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=tr('Error compiling flow: {exc}', exc=exc))

    flow_name = graph.name or flow_id
    run_id = history.record_run_start(flow_id=flow_id, flow_name=flow_name, trigger_type="webhook")
    ns: dict[str, Any] = {}
    t0 = time.time()
    try:
        exec(compile(source, f"<dedalo:webhook:{flow_id}>", "exec"), ns)
        run_fn = ns.get("run")
        if not callable(run_fn):
            raise RuntimeError(tr('The compiled flow does not contain a run() function'))
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
        raise HTTPException(status_code=500, detail=tr('Error during webhook execution: {exc}', exc=exc))
    finally:
        runtime.compact_memory()


@app.api_route("/api/webhook/{flow_id}", methods=["GET", "POST", "PUT"])
async def webhook_endpoint(flow_id: str, request: Request) -> dict[str, Any]:
    """Recibe una llamada externa para disparar un flujo por webhook."""
    req_headers = dict(request.headers)
    query = dict(request.query_params)

    # Token opcional (DEDALO_WEBHOOK_TOKEN): sin él, el comportamiento es el de siempre.
    expected = os.environ.get("DEDALO_WEBHOOK_TOKEN", "")
    if expected:
        ok, source = webhook_token_ok(request.headers, request.query_params, expected)
        if not ok:
            raise HTTPException(status_code=401, detail=tr('Invalid webhook token'), headers={"WWW-Authenticate": "Bearer"})
        # El token no debe llegar al flujo ni al historial
        if source == "query":
            query.pop("token", None)
        else:
            req_headers.pop(source, None)

    flow_data = store.load_flow(flow_id)
    if not flow_data:
        raise HTTPException(status_code=404, detail=tr('Flow "{flow_id}" not found', flow_id=flow_id))

    raw_body = await request.body()
    body_data: Any = None
    if raw_body:
        try:
            body_data = json.loads(raw_body)
        except Exception:
            body_data = raw_body.decode("utf-8", errors="replace")
    elif query:
        body_data = query
    else:
        body_data = {}

    ctx = scheduler.FlowContext(
        flow_name=flow_id,
        payload=body_data,
        headers=req_headers,
    )
    return await asyncio.to_thread(_run_webhook_sync, flow_id, flow_data.get("graph") or {}, ctx)


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
        raise HTTPException(status_code=404, detail=tr('Run "{id}" not found', id=id))
    return run_data


@app.post("/api/history/purge")
def purge_history_endpoint(days: int = Query(30, ge=1, le=3650)) -> dict[str, Any]:
    """Purga ejecuciones con antigüedad superior a los días especificados (por defecto 30)."""
    deleted = history.purge_older_than(days=days)
    return {"status": "ok", "deleted": deleted, "days": days}

