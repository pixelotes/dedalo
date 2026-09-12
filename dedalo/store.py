"""Almacenamiento de flujos de Dédalo en disco.

Cada flujo guarda:
- `<slug>.json`: El grafo canónico en formato JSON.
- `<slug>.py`: El script generado por `compiler.compile()`, sellado con un hash.
- `<slug>.meta.json`: Metadatos de programación (schedule), estado activo y descripción.
- `data/state/<slug>.state.json`: Estado persistente entre ejecuciones (GUIDs de RSS, etc.).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
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

    state_p = STATE_DIR / f"{s}.state.json"
    if state_p.is_file():
        try:
            state_p.unlink()
        except OSError:
            pass

    return deleted
