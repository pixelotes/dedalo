"""Almacenamiento de flujos de Dédalo en disco.

Cada flujo guarda:
- `<slug>.json`: El grafo canónico en formato JSON.
- `<slug>.py`: El script generado por `compiler.compile()`, sellado con un hash.
- `<slug>.meta.json`: Metadatos de programación (schedule), estado activo y descripción.
- `data/state/<slug>.state.json`: Estado persistente entre ejecuciones (GUIDs de RSS, etc.).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

from dedalo import compiler

FLOWS_DIR = Path("data/flows")
STATE_DIR = Path("data/state")

_HASH_LINE_RE = re.compile(r"^# dedalo\.graph_hash = ([0-9a-f]+)\s*$", re.MULTILINE)


class DriftError(Exception):
    """Lanzada cuando el archivo .py en disco ha sido modificado a mano tras la última compilación."""


def slug(name: str) -> str:
    """Normaliza un nombre a un identificador seguro para el sistema de ficheros."""
    s = (name or "").lower().strip()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    return s.strip("-") or "flujo"


def _graph_hash(graph: compiler.Graph) -> str:
    canonical = json.dumps(graph.to_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _stamp(source: str, graph_hash: str) -> str:
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# dedalo.VERSION"):
            lines.insert(i + 1, f"# dedalo.graph_hash = {graph_hash}")
            break
    else:
        lines.insert(0, f"# dedalo.graph_hash = {graph_hash}")
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _stamped_hash(source: str) -> str | None:
    match = _HASH_LINE_RE.search(source)
    return match.group(1) if match else None


def list_flows() -> list[dict[str, Any]]:
    """Devuelve la lista de todos los flujos guardados con sus metadatos."""
    if not FLOWS_DIR.is_dir():
        return []

    flows = []
    for p in sorted(FLOWS_DIR.glob("*.json")):
        if p.name.endswith(".meta.json"):
            continue
        flow_id = p.stem
        meta_file = FLOWS_DIR / f"{flow_id}.meta.json"
        meta = {}
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        flows.append({
            "id": flow_id,
            "name": meta.get("name", flow_id),
            "description": meta.get("description", ""),
            "schedule": meta.get("schedule", ""),
            "enabled": meta.get("enabled", True),
            "updated_at": meta.get("updated_at", int(p.stat().st_mtime)),
        })
    return flows


def load_flow(flow_id: str) -> dict[str, Any] | None:
    """Carga un flujo completo (grafo + metadatos + código compilado)."""
    s = slug(flow_id)
    graph_path = FLOWS_DIR / f"{s}.json"
    if not graph_path.is_file():
        return None

    try:
        graph_dict = json.loads(graph_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    meta_path = FLOWS_DIR / f"{s}.meta.json"
    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    code_path = FLOWS_DIR / f"{s}.py"
    code = ""
    if code_path.is_file():
        try:
            code = code_path.read_text(encoding="utf-8")
        except Exception:
            pass

    return {
        "id": s,
        "graph": graph_dict,
        "meta": meta,
        "code": code,
    }


def load_meta(flow_id: str) -> dict[str, Any]:
    """Carga los metadatos de un flujo si existen."""
    s = slug(flow_id)
    meta_path = FLOWS_DIR / f"{s}.meta.json"
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"name": s, "description": "", "schedule": "", "enabled": True}


def save_flow(
    flow_id: str,
    graph_dict: dict[str, Any],
    meta_dict: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Compila y guarda un flujo en disco."""
    s = slug(flow_id)
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)

    graph = compiler.Graph.from_dict(graph_dict)
    graph.name = s

    # Comprobación de drift si el .py ya existía
    code_file = FLOWS_DIR / f"{s}.py"
    if code_file.is_file() and not force:
        old_source = code_file.read_text(encoding="utf-8")
        stamped = _stamped_hash(old_source)
        if stamped:
            old_graph_file = FLOWS_DIR / f"{s}.json"
            if old_graph_file.is_file():
                try:
                    old_graph = compiler.Graph.from_dict(
                        json.loads(old_graph_file.read_text(encoding="utf-8"))
                    )
                    expected_stamped = _graph_hash(old_graph)
                    if stamped != expected_stamped:
                        raise DriftError(
                            f"El fichero «{s}.py» fue modificado a mano fuera de Dédalo. "
                            f"Guarda con force=True para sobreescribirlo."
                        )
                except Exception:
                    pass

    # Compilar código
    source = compiler.compile(graph)
    ghash = _graph_hash(graph)
    stamped_source = _stamp(source, ghash)

    # Guardar grafo JSON y código Python
    graph_file = FLOWS_DIR / f"{s}.json"
    graph_file.write_text(json.dumps(graph.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    code_file.write_text(stamped_source, encoding="utf-8")

    # Guardar metadatos
    current_meta = {
        "name": (meta_dict or {}).get("name", s),
        "description": (meta_dict or {}).get("description", ""),
        "schedule": (meta_dict or {}).get("schedule", ""),
        "enabled": (meta_dict or {}).get("enabled", True),
        "updated_at": int(time.time()),
    }
    meta_file = FLOWS_DIR / f"{s}.meta.json"
    meta_file.write_text(json.dumps(current_meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "id": s,
        "source": stamped_source,
        "meta": current_meta,
    }


def delete_flow(flow_id: str) -> bool:
    """Elimina todos los ficheros asociados a un flujo."""
    s = slug(flow_id)
    deleted = False
    for suffix in [".json", ".py", ".meta.json"]:
        p = FLOWS_DIR / f"{s}{suffix}"
        if p.is_file():
            try:
                p.unlink()
                deleted = True
            except OSError:
                pass

    return deleted


def update_meta(flow_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Actualiza metadatos de un flujo (schedule, enabled, etc.) sin recompilar el grafo."""
    s = slug(flow_id)
    meta_file = FLOWS_DIR / f"{s}.meta.json"
    meta = {
        "name": s,
        "description": "",
        "schedule": "",
        "enabled": True,
        "updated_at": int(time.time()),
    }
    if meta_file.is_file():
        try:
            meta.update(json.loads(meta_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    meta.update(updates)
    meta["updated_at"] = int(time.time())
    FLOWS_DIR.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta


def toggle_flow(flow_id: str) -> dict[str, Any]:
    """Alterna el estado enabled de un flujo."""
    s = slug(flow_id)
    meta_file = FLOWS_DIR / f"{s}.meta.json"
    enabled = True
    if meta_file.is_file():
        try:
            m = json.loads(meta_file.read_text(encoding="utf-8"))
            enabled = not m.get("enabled", True)
        except Exception:
            pass
    return update_meta(s, {"enabled": enabled})


def _extract_required_secrets(graph_dict: dict[str, Any]) -> list[str]:
    """Extrae los nombres de secretos requeridos por los nodos de un grafo."""
    secrets_set = set()
    for n in graph_dict.get("nodes", []):
        cfg = n.get("config", {})
        for k in ("secret", "token_secret"):
            val = cfg.get(k)
            if val and isinstance(val, str) and val.strip():
                secrets_set.add(val.strip())
    return sorted(secrets_set)


def export_flow_standalone_py(flow_id: str) -> str:
    """Exporta un flujo a un script Python autónomo y ejecutable por CLI."""
    s = slug(flow_id)
    flow_data = load_flow(s)
    if not flow_data:
        raise FileNotFoundError(f"Flujo «{flow_id}» no encontrado.")

    graph_dict = flow_data.get("graph", {})
    graph = compiler.Graph.from_dict(graph_dict)
    if not graph.name:
        graph.name = s

    return compiler.compile_standalone(graph)


def export_flow_zip(flow_id: str) -> bytes:
    """Exporta un flujo a un archivo ZIP en memoria con su grafo, metadatos y manifiesto."""
    s = slug(flow_id)
    flow_data = load_flow(s)
    if not flow_data:
        raise FileNotFoundError(f"Flujo «{flow_id}» no encontrado.")

    graph_dict = flow_data.get("graph", {})
    meta_dict = flow_data.get("meta", {})
    required_secrets = _extract_required_secrets(graph_dict)

    manifest = {
        "format": "dedalo_flow_bundle",
        "version": 1,
        "exported_at": int(time.time()),
        "flows": [
            {
                "id": s,
                "name": meta_dict.get("name", s),
                "required_secrets": required_secrets,
            }
        ],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        zf.writestr(f"{s}.json", json.dumps(graph_dict, indent=2, ensure_ascii=False))
        if meta_dict:
            zf.writestr(f"{s}.meta.json", json.dumps(meta_dict, indent=2, ensure_ascii=False))

    return buf.getvalue()


def export_all_flows_zip() -> bytes:
    """Exporta todos los flujos existentes a un archivo ZIP en memoria como backup completo."""
    flows = list_flows()
    buf = io.BytesIO()
    manifest_flows = []

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in flows:
            fid = f["id"]
            fdata = load_flow(fid)
            if not fdata:
                continue
            graph_dict = fdata.get("graph", {})
            meta_dict = fdata.get("meta", {})
            required_secrets = _extract_required_secrets(graph_dict)
            manifest_flows.append({
                "id": fid,
                "name": meta_dict.get("name", fid),
                "required_secrets": required_secrets,
            })
            zf.writestr(f"{fid}.json", json.dumps(graph_dict, indent=2, ensure_ascii=False))
            if meta_dict:
                zf.writestr(f"{fid}.meta.json", json.dumps(meta_dict, indent=2, ensure_ascii=False))

        manifest = {
            "format": "dedalo_flows_backup",
            "version": 1,
            "exported_at": int(time.time()),
            "total_flows": len(manifest_flows),
            "flows": manifest_flows,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

    return buf.getvalue()


def import_flows_from_zip(zip_bytes: bytes, overwrite: bool = True) -> dict[str, Any]:
    """Importa uno o varios flujos desde un archivo ZIP (o JSON individual) en memoria.

    Aplica defensas estrictas contra Zip-Slip (path traversal) y límites de descompresión.
    """
    if not zip_bytes:
        return {"status": "error", "error": "El archivo recibido está vacío.", "imported": []}

    # 1. ¿Es un JSON directo en lugar de ZIP?
    if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
        try:
            data = json.loads(zip_bytes.decode("utf-8"))
            if isinstance(data, dict):
                graph_dict = data.get("graph") if "graph" in data else data
                meta_dict = data.get("meta") if "meta" in data else {}
                if isinstance(graph_dict, dict) and "nodes" in graph_dict:
                    flow_name = meta_dict.get("name") or graph_dict.get("name") or "flujo-importado"
                    fid = slug(flow_name)
                    save_flow(fid, graph_dict, meta_dict, force=overwrite)
                    sec = _extract_required_secrets(graph_dict)
                    return {
                        "status": "ok",
                        "imported_count": 1,
                        "imported": [{"id": fid, "name": flow_name}],
                        "required_secrets": sec,
                        "errors": [],
                    }
        except Exception as exc:
            return {"status": "error", "error": f"Archivo no es un ZIP ni un JSON válido: {exc}", "imported": []}
        return {"status": "error", "error": "El archivo JSON no contiene un grafo válido.", "imported": []}

    # 2. Descompresión segura del ZIP
    MAX_TOTAL_BYTES = 25 * 1024 * 1024  # 25 MB
    MAX_FILES = 500

    extracted_files: dict[str, bytes] = {}
    total_bytes = 0

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            namelist = zf.namelist()
            if len(namelist) > MAX_FILES:
                return {"status": "error", "error": "El ZIP contiene demasiados archivos (máx. 500).", "imported": []}

            for name in namelist:
                # Comprobación contra Zip-Slip / Path Traversal
                norm_name = Path(name).as_posix()
                if (
                    norm_name.startswith("/")
                    or norm_name.startswith("\\")
                    or ".." in Path(norm_name).parts
                    or ":" in norm_name
                ):
                    return {"status": "error", "error": f"Archivo sospechoso detectado en ZIP: {name}", "imported": []}

                info = zf.getinfo(name)
                if info.is_dir():
                    continue

                total_bytes += info.file_size
                if total_bytes > MAX_TOTAL_BYTES:
                    return {"status": "error", "error": "El contenido del ZIP excede el límite de 25 MB.", "imported": []}

                basename = Path(norm_name).name
                if basename.endswith(".json"):
                    extracted_files[basename] = zf.read(name)
    except Exception as exc:
        return {"status": "error", "error": f"Error al abrir el archivo ZIP: {exc}", "imported": []}

    # 3. Emparejar grafos y metadatos
    imported = []
    errors = []
    all_required_secrets = set()

    # Identificar metadatos: *.meta.json
    meta_files: dict[str, dict[str, Any]] = {}
    for fname, content in extracted_files.items():
        if fname.endswith(".meta.json"):
            stem = fname[:-10]
            try:
                meta_files[stem] = json.loads(content.decode("utf-8"))
            except Exception:
                pass

    # Procesar grafos: *.json
    for fname, content in extracted_files.items():
        if fname == "manifest.json" or fname.endswith(".meta.json"):
            continue
        stem = fname[:-5]
        try:
            raw_obj = json.loads(content.decode("utf-8"))
            if not isinstance(raw_obj, dict):
                continue
            graph_dict = raw_obj.get("graph") if "graph" in raw_obj else raw_obj
            meta_dict = meta_files.get(stem) or raw_obj.get("meta") or {}

            if not isinstance(graph_dict, dict) or "nodes" not in graph_dict:
                continue

            flow_name = meta_dict.get("name") or graph_dict.get("name") or stem
            fid = slug(stem or flow_name)

            save_flow(fid, graph_dict, meta_dict, force=overwrite)
            sec = _extract_required_secrets(graph_dict)
            all_required_secrets.update(sec)
            imported.append({"id": fid, "name": flow_name})
        except Exception as exc:
            errors.append({"file": fname, "error": str(exc)})

    return {
        "status": "ok",
        "imported_count": len(imported),
        "imported": imported,
        "required_secrets": sorted(all_required_secrets),
        "errors": errors,
    }

