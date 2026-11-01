"""Funciones de ejecución para los nodos de Dédalo.

Cada función sigue la signatura: `(ctx, inputs, **config) -> NodeResult`.
Los scripts compilados invocan directamente estas funciones sin sobrecarga de runtime.
"""

from __future__ import annotations

import base64
import ctypes
import datetime
import email
import email.header
import gc
import gzip
import hashlib
import html
import json
import math
import os
import random
import re
import socket
import ssl
from collections import Counter, defaultdict
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from html.parser import HTMLParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

# csv, difflib, imaplib, smtplib, statistics, xml.etree y email.mime se importan al usarse (~80 ms y ~5 MB de arranque)
from dedalo import llms, secrets
from dedalo.i18n import tr
from dedalo import sandbox as sbx


_malloc_trim: Any = None


def compact_memory() -> None:
    """Fuerza la recolección de basura de Python y solicita a glibc liberar heap al SO."""
    global _malloc_trim
    gc.collect()
    if _malloc_trim is None:
        _malloc_trim = False
        if sys.platform.startswith("linux"):
            try:
                _malloc_trim = ctypes.CDLL("libc.so.6").malloc_trim
            except Exception:
                pass
    if _malloc_trim:
        _malloc_trim(0)

DEFAULT_TIMEOUT = 20.0
DEFAULT_AGENT_TOKENS = 2048
MAX_ITEMS = 40
UNBOUNDED = 10**9


@dataclass
class NodeResult:
    """Resultado de ejecución de un nodo."""

    status: str  # "ok" | "error" | "skipped"
    data: Any = None
    error: str = ""
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def failed(self) -> bool:
        return self.status == "error"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


SKIPPED = NodeResult(status="skipped")


class Inputs:
    """Envuelve la lista de entradas de un nodo permitiendo indexar por ID de origen."""

    def __init__(self, items: list[NodeResult], sources: list[str] | None = None):
        self._items = items
        self._by_source = {}
        if sources and len(sources) == len(items):
            for s, res in zip(sources, items):
                self._by_source[s] = res

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, str):
            if key in self._by_source:
                res = self._by_source[key]
                return res.data if res.ok else None
            raise KeyError(tr('There is no connected input with id "{key}"', key=key))
        return self._items[key].data if self._items[key].ok else None

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = self[key]
            return default if val is None else val
        except (KeyError, IndexError):
            return default

    def text(self) -> str:
        """Devuelve el contenido en texto combinado de todas las entradas válidas."""
        texts = []
        for it in self._items:
            if it.ok and it.data is not None:
                if isinstance(it.data, str):
                    texts.append(it.data)
                elif isinstance(it.data, (dict, list)):
                    texts.append(json.dumps(it.data, ensure_ascii=False, indent=2))
                else:
                    texts.append(str(it.data))
        return "\n\n".join(texts)

    def first_data(self) -> Any:
        """Devuelve los datos del primer nodo exitoso."""
        for it in self._items:
            if it.ok:
                return it.data
        return None

    def first(self) -> Any:
        """Alias de first_data()."""
        return self.first_data()

    def all_ok(self) -> list[Any]:
        """Devuelve una lista con los datos de todas las entradas exitosas."""
        return [it.data for it in self._items if it.ok]


# ---------------------------------------------------------------------------
# Cliente LLM externo configurable (OpenAI compatible)
# ---------------------------------------------------------------------------


def call_llm(
    prompt: str,
    system: str = "",
    *,
    llm: str = "",
    url: str = "",
    model: str = "",
    secret_name: str = "",
    tokens: int = DEFAULT_AGENT_TOKENS,
    temperature: float | None = None,
    json_mode: bool = False,
) -> str:
    """Llama a un endpoint compatible con OpenAI (/v1/chat/completions)."""
    overrides: dict[str, Any] = {
        "url": url,
        "model": model,
        "secret": secret_name,
    }
    if temperature is not None:
        overrides["temperature"] = temperature

    resolved = llms.resolve(name=llm, overrides=overrides)
    base_url = resolved["url"].rstrip("/")
    target_model = resolved["model"]
    api_key = resolved["api_key"]
    model_temp = resolved["temperature"]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": target_model,
        "messages": messages,
        "max_tokens": tokens,
        "temperature": model_temp,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    endpoint = f"{base_url}/chat/completions"
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code >= 400:
                raise RuntimeError(
                    tr('LLM API returned HTTP {status_code}: {text}', status_code=resp.status_code, text=resp.text[:200])
                )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise RuntimeError(tr('Error connecting to the LLM at {endpoint}: {exc}', endpoint=endpoint, exc=exc)) from exc


# ---------------------------------------------------------------------------
# Nodos de Entrada
# ---------------------------------------------------------------------------

USER_AGENT_PRESETS = {
    "chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "bot": "Dedalo/0.1 (+https://github.com/raul/dedalo)",
}


def _resolve_user_agent(user_agent: str) -> str:
    """Devuelve el User-Agent correspondiente al preset o la cadena personalizada."""
    ua = (user_agent or "").strip()
    if not ua:
        return ""
    return USER_AGENT_PRESETS.get(ua.lower(), ua)


def _has_curl_cffi() -> bool:
    """Comprueba si la librería curl_cffi está disponible en el entorno."""
    try:
        from curl_cffi import requests  # noqa: F401
        return True
    except ImportError:
        return False


def http_call(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    method: str = "GET",
    headers: str = "",
    body: str = "",
    secret: str = "",
    auth_header: str = "Authorization",
    timeout: float = DEFAULT_TIMEOUT,
    engine: str = "auto",
    impersonate: str = "chrome",
    user_agent: str = "",
) -> NodeResult:
    """Petición HTTP a una API REST o página web con soporte de curl_cffi y suplantación TLS."""
    req_headers: dict[str, str] = {}
    if headers:
        try:
            extra = json.loads(headers) if isinstance(headers, str) else headers
            if isinstance(extra, dict):
                req_headers.update({str(k): str(v) for k, v in extra.items()})
        except json.JSONDecodeError:
            pass

    if secret:
        token = secrets.get(secret)
        if token:
            req_headers[auth_header] = (
                f"Bearer {token}" if auth_header == "Authorization" and not token.startswith("Bearer ") else token
            )

    # Determinar motor
    engine_choice = (engine or "auto").strip().lower()
    has_curl = _has_curl_cffi()

    if engine_choice == "curl_cffi":
        if not has_curl:
            return NodeResult("error", error=tr("The 'curl_cffi' engine is not installed in this environment."))
        use_curl = True
    elif engine_choice == "httpx":
        use_curl = False
    else:  # "auto"
        use_curl = has_curl

    # Resolver User-Agent
    custom_ua = _resolve_user_agent(user_agent)
    if custom_ua:
        req_headers["User-Agent"] = custom_ua
    elif not use_curl or (impersonate or "").lower() in ("", "none"):
        target_preset = (impersonate or "chrome").lower()
        req_headers["User-Agent"] = USER_AGENT_PRESETS.get(target_preset, USER_AGENT_PRESETS["chrome"])

    kwargs: dict[str, Any] = {}
    if body:
        inp = Inputs(inputs)
        rendered_body = body.replace("{entrada}", inp.text())
        try:
            kwargs["json"] = json.loads(rendered_body)
        except Exception:
            kwargs["content"] = rendered_body.encode("utf-8")

    if use_curl:
        try:
            from curl_cffi import requests as curl
            curl_kwargs: dict[str, Any] = {
                "headers": req_headers,
                "timeout": float(timeout),
                "allow_redirects": True,
            }
            imp = (impersonate or "").lower()
            if imp and imp != "none":
                curl_kwargs["impersonate"] = imp

            if "json" in kwargs:
                curl_kwargs["json"] = kwargs["json"]
            elif "content" in kwargs:
                curl_kwargs["data"] = kwargs["content"]

            resp = curl.request(method.upper(), url, **curl_kwargs)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"HTTP {resp.status_code}: {resp.text[:150]}")
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    return NodeResult("ok", data=resp.json())
                except Exception:
                    return NodeResult("ok", data=resp.text)
            return NodeResult("ok", data=resp.text)
        except Exception as exc:
            if engine_choice != "auto":
                return NodeResult("error", error=f"Fallo HTTP (curl_cffi): {exc}")

    try:
        with httpx.Client(timeout=float(timeout), follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=req_headers, **kwargs)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"HTTP {resp.status_code}: {resp.text[:150]}")
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                try:
                    return NodeResult("ok", data=resp.json())
                except Exception:
                    return NodeResult("ok", data=resp.text)
            return NodeResult("ok", data=resp.text)
    except Exception as exc:
        return NodeResult("error", error=f"Fallo HTTP (httpx): {exc}")


def rss(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    only_new: bool = False,
    corte: str = "completo",
    max_items: int = 20,
    filter_keyword: str = "",
    engine: str = "auto",
    impersonate: str = "chrome",
    user_agent: str = "",
) -> NodeResult:
    """Lee y parsea un feed RSS con soporte de curl_cffi."""
    fetch_res = http_call(
        ctx,
        inputs,
        url=url,
        method="GET",
        timeout=DEFAULT_TIMEOUT,
        engine=engine,
        impersonate=impersonate,
        user_agent=user_agent,
    )
    if not fetch_res.ok:
        return NodeResult("error", error=tr('Could not download the feed: {error}', error=fetch_res.error))

    xml_text = fetch_res.data if isinstance(fetch_res.data, str) else json.dumps(fetch_res.data)

    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_text)
    except Exception as exc:
        return NodeResult("error", error=tr('Invalid RSS XML: {exc}', exc=exc))

    items = []
    # Soporta RSS estándar <channel><item> y Atom <entry>
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for elem in entries:
        title = elem.findtext("title") or elem.findtext("{http://www.w3.org/2005/Atom}title") or ""
        link = elem.findtext("link") or ""
        if not link:
            link_elem = elem.find("{http://www.w3.org/2005/Atom}link")
            if link_elem is not None:
                link = link_elem.attrib.get("href", "")
        guid = (
            elem.findtext("guid")
            or elem.findtext("{http://www.w3.org/2005/Atom}id")
            or link
            or title
        )
        date = (
            elem.findtext("pubDate")
            or elem.findtext("{http://www.w3.org/2005/Atom}updated")
            or ""
        )
        desc = (
            elem.findtext("description")
            or elem.findtext("{http://www.w3.org/2005/Atom}summary")
            or ""
        )

        items.append({
            "title": title.strip(),
            "link": link.strip(),
            "guid": guid.strip(),
            "date": date.strip(),
            "description": desc.strip(),
        })

    flow_name = getattr(ctx, "flow_name", "default")
    state_file = Path("data/state") / f"{flow_name}_rss.json"

    if only_new:
        seen = set()
        if state_file.is_file():
            try:
                seen = set(json.loads(state_file.read_text(encoding="utf-8")))
            except Exception:
                pass
        current_guids = [it["guid"] for it in items]
        items = [it for it in items if it["guid"] not in seen]
        # Guardar nuevos
        state_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            state_file.write_text(json.dumps(current_guids[:500]), encoding="utf-8")
        except Exception:
            pass

    if filter_keyword.strip():
        kw = filter_keyword.strip().lower()
        items = [
            it for it in items
            if kw in it["title"].lower() or kw in it["description"].lower()
        ]

    limit = int(max_items) if str(max_items).isdigit() else 20
    items = items[:limit]

    if corte.isdigit():
        max_chars = int(corte)
        for it in items:
            if len(it.get("description", "")) > max_chars:
                it["description"] = it["description"][:max_chars] + "..."

    return NodeResult("ok", data=items)


def csv_file(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str,
    delimiter: str = ",",
    encoding: str = "utf-8",
) -> NodeResult:
    """Lee un fichero CSV local y devuelve lista de diccionarios."""
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=tr('File not found: {path}', path=path))
    try:
        delim = delimiter.replace("\\t", "\t") if delimiter else ","
        with p.open(encoding=encoding or "utf-8", newline="") as f:
            import csv

            reader = csv.DictReader(f, delimiter=delim)
            return NodeResult("ok", data=list(reader))
    except Exception as exc:
        return NodeResult("error", error=tr('Error reading CSV: {exc}', exc=exc))


def webhook_trigger(ctx: Any, inputs: list[NodeResult], **config) -> NodeResult:
    """Recibe datos desde un webhook entrante HTTP."""
    payload = getattr(ctx, "payload", None)
    if payload is None:
        return NodeResult("ok", data={"event": "manual_preview", "message": tr('Triggered manually without payload')})
    return NodeResult("ok", data=payload)


def sqlite_query(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str = "data/store.db",
    query: str = "",
) -> NodeResult:
    """Ejecuta una consulta SELECT sobre una base de datos SQLite local."""
    if not query.strip():
        return NodeResult("error", error=tr('Missing SQL query'))
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=tr('SQLite database not found: {path}', path=path))
    try:
        with sqlite3.connect(p) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(query)
            rows = [dict(r) for r in cur.fetchall()]
            return NodeResult("ok", data=rows)
    except Exception as exc:
        return NodeResult("error", error=f"Error en consulta SQLite: {exc}")


def command_input(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    cmd: str,
    timeout: float = 15.0,
    cwd: str = "",
    sandbox: str = "",
) -> NodeResult:
    """Ejecuta un comando en la shell del sistema y captura su salida."""
    if not cmd.strip():
        return NodeResult("error", error=tr('Missing command to run'))
    inp = Inputs(inputs)
    rendered_cmd = cmd.replace("{entrada}", inp.text())
    work_dir = Path(cwd) if cwd.strip() else None
    try:
        res = sbx.run_shell(rendered_cmd, kind="command", timeout=float(timeout), cwd=work_dir, mode=sandbox)
        data = {
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip(),
            "exit_code": res.returncode,
        }
        if res.returncode != 0:
            return NodeResult("error", data=data, error=tr('Command returned code {returncode}: {stderr}', returncode=res.returncode, stderr=res.stderr[:200]))
        return NodeResult("ok", data=data)
    except subprocess.TimeoutExpired:
        return NodeResult("error", error=tr('Execution time exceeded ({timeout}s) for the command', timeout=timeout))
    except sbx.SandboxUnavailable as exc:
        return NodeResult("error", error=str(exc))
    except Exception as exc:
        return NodeResult("error", error=tr('Error running command: {exc}', exc=exc))


def pi_health(ctx: Any, inputs: list[NodeResult], **config) -> NodeResult:
    """Lee métricas del sistema directamente del kernel Linux (Raspberry Pi)."""
    metrics: dict[str, Any] = {
        "timestamp": time.time(),
        "cpu_temp_c": None,
        "memory_total_mb": None,
        "memory_free_mb": None,
        "memory_percent_used": None,
        "load_1m": None,
        "load_5m": None,
        "load_15m": None,
    }
    # 1. Carga de CPU
    try:
        load = os.getloadavg()
        metrics["load_1m"], metrics["load_5m"], metrics["load_15m"] = [round(x, 2) for x in load]
    except Exception:
        pass

    # 2. Temperatura CPU (Raspberry Pi / Linux thermal zone)
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal_path.is_file():
        try:
            raw_temp = thermal_path.read_text().strip()
            metrics["cpu_temp_c"] = round(float(raw_temp) / 1000.0, 1)
        except Exception:
            pass

    # 3. Memoria desde /proc/meminfo
    meminfo_path = Path("/proc/meminfo")
    if meminfo_path.is_file():
        try:
            mem_data = {}
            for line in meminfo_path.read_text().splitlines():
                parts = line.split(":")
                if len(parts) == 2:
                    k = parts[0].strip()
                    v = parts[1].strip().split()[0]
                    if v.isdigit():
                        mem_data[k] = int(v)
            total_kb = mem_data.get("MemTotal", 0)
            avail_kb = mem_data.get("MemAvailable", mem_data.get("MemFree", 0))
            if total_kb > 0:
                metrics["memory_total_mb"] = round(total_kb / 1024, 1)
                metrics["memory_free_mb"] = round(avail_kb / 1024, 1)
                metrics["memory_percent_used"] = round((1.0 - (avail_kb / total_kb)) * 100, 1)
        except Exception:
            pass

    # 4. Uso de disco
    try:
        du = shutil.disk_usage("/")
        metrics["disk_percent_used"] = round((du.used / du.total) * 100, 1)
        metrics["disk_free_gb"] = round(du.free / (1024**3), 2)
    except Exception:
        pass

    return NodeResult("ok", data=metrics)


def directory_scan(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_items: int = 50,
) -> NodeResult:
    """Escanea un directorio buscando ficheros que cumplan un patrón glob."""
    base = Path(path)
    if not base.exists() or not base.is_dir():
        return NodeResult("error", error=tr('The directory does not exist: {path}', path=path))
    try:
        iterator = base.rglob(pattern) if recursive else base.glob(pattern)
        files = []
        limit = int(max_items) if str(max_items).isdigit() else 50
        for p in iterator:
            if p.is_file():
                stat = p.stat()
                files.append({
                    "name": p.name,
                    "path": str(p),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
                if len(files) >= limit:
                    break
        return NodeResult("ok", data=files)
    except Exception as exc:
        return NodeResult("error", error=tr('Error scanning directory: {exc}', exc=exc))


def telemetry_input(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    incident_id: str = "",
    resource: str = "",
    telemetry_json: str = "",
    file_path: str = "",
) -> NodeResult:
    """Recolecta telemetría de incidente desde JSON directo, archivo local o entradas."""
    inp = Inputs(inputs)
    payload_data: Any = None

    # 1. Prioridad: archivo si se especifica
    if file_path.strip():
        p = Path(file_path.strip())
        if not p.exists() or not p.is_file():
            return NodeResult("error", error=tr('Telemetry file not found: {file_path}', file_path=file_path))
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            try:
                payload_data = json.loads(content)
            except Exception:
                payload_data = content
        except Exception as exc:
            return NodeResult("error", error=tr('Error reading telemetry from {file_path}: {exc}', file_path=file_path, exc=exc))

    # 2. JSON inline si no hubo archivo
    elif telemetry_json.strip():
        raw_txt = telemetry_json.strip()
        try:
            payload_data = json.loads(raw_txt)
        except Exception:
            payload_data = raw_txt

    # 3. Datos provenientes de nodos previos si no hay ni archivo ni JSON
    else:
        first = inp.first_data()
        payload_data = first if first is not None else inp.text()

    if payload_data is None:
        payload_data = {}

    inc_id = incident_id.strip()
    if not inc_id:
        if isinstance(payload_data, dict) and payload_data.get("incident_id"):
            inc_id = str(payload_data["incident_id"])
        elif isinstance(payload_data, dict) and payload_data.get("id"):
            inc_id = str(payload_data["id"])
        else:
            inc_id = f"inc-{int(time.time())}"

    res_name = resource.strip()
    if not res_name and isinstance(payload_data, dict):
        res_name = str(payload_data.get("resource") or payload_data.get("target") or "")

    result_bundle = {
        "incident_id": inc_id,
        "resource": res_name,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "telemetry": payload_data,
    }
    return NodeResult("ok", data=result_bundle)


def _build_atlassian_auth_header(email: str, secret_name: str) -> dict[str, str]:
    """Construye cabeceras HTTP con Basic Auth (email:api_token) para Atlassian Cloud."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    clean_email = email.strip()
    token = secrets.get(secret_name) if secret_name else ""
    if clean_email and token:
        cred = f"{clean_email}:{token}"
        b64_cred = base64.b64encode(cred.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {b64_cred}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def jira_issues_input(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    instance_url: str,
    email: str = "",
    secret: str = "",
    jql: str = "",
    fields: str = "summary,status,priority,assignee,created,updated,description",
    max_results: int = 20,
    timeout: float = DEFAULT_TIMEOUT,
) -> NodeResult:
    """Busca y extrae issues de Jira Cloud mediante JQL usando la REST API oficial."""
    base_url = instance_url.strip().rstrip("/")
    if not base_url:
        return NodeResult("error", error=tr('Missing Jira instance URL (e.g. https://my-domain.atlassian.net)'))

    headers = _build_atlassian_auth_header(email, secret)
    endpoint = f"{base_url}/rest/api/3/search"

    field_list = [f.strip() for f in fields.split(",") if f.strip()] if fields else ["summary", "status", "priority"]
    clean_jql = jql.strip()
    if not clean_jql:
        clean_jql = "order by created DESC"

    payload: dict[str, Any] = {
        "jql": clean_jql,
        "fields": field_list,
        "maxResults": min(max(1, int(max_results or 20)), 100),
    }

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Jira API error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            raw_issues = data.get("issues", [])
            total = data.get("total", len(raw_issues))

            simplified_issues = []
            for issue in raw_issues:
                f = issue.get("fields") or {}
                simplified_issues.append({
                    "key": issue.get("key"),
                    "id": issue.get("id"),
                    "url": f"{base_url}/browse/{issue.get('key')}",
                    "summary": f.get("summary"),
                    "status": (f.get("status") or {}).get("name"),
                    "priority": (f.get("priority") or {}).get("name"),
                    "assignee": (f.get("assignee") or {}).get("displayName"),
                    "created": f.get("created"),
                    "updated": f.get("updated"),
                    "raw_fields": f,
                })
            return NodeResult("ok", data={
                "total": total,
                "count": len(simplified_issues),
                "jql": clean_jql,
                "issues": simplified_issues,
            })
    except Exception as exc:
        return NodeResult("error", error=f"Error consultando Jira: {exc}")


def confluence_pages_input(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    instance_url: str,
    email: str = "",
    secret: str = "",
    cql: str = "",
    space_key: str = "",
    title: str = "",
    limit: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
) -> NodeResult:
    """Busca y lee páginas de Confluence Cloud mediante CQL o parámetros de espacio/título."""
    base_url = instance_url.strip().rstrip("/")
    if not base_url:
        return NodeResult("error", error=tr('Missing Confluence instance URL (e.g. https://my-domain.atlassian.net/wiki)'))

    # Asegurar sufijo /wiki si no se incluyó en la URL de Atlassian
    wiki_base = base_url if base_url.endswith("/wiki") else f"{base_url}/wiki"
    headers = _build_atlassian_auth_header(email, secret)
    endpoint = f"{wiki_base}/rest/api/content/search"

    query_parts = []
    if cql.strip():
        query_parts.append(cql.strip())
    else:
        query_parts.append("type=page")
        if space_key.strip():
            query_parts.append(f'space="{space_key.strip()}"')
        if title.strip():
            query_parts.append(f'title ~ "{title.strip()}"')

    final_cql = " AND ".join(query_parts)
    params = {
        "cql": final_cql,
        "limit": min(max(1, int(limit or 10)), 50),
        "expand": "body.storage,version,space",
    }

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            resp = client.get(endpoint, headers=headers, params=params)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Confluence API error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            raw_results = data.get("results", [])

            pages = []
            for item in raw_results:
                body_val = ((item.get("body") or {}).get("storage") or {}).get("value", "")
                # Limpieza rápida de etiquetas XML de almacenamiento Confluence
                clean_body = re.sub(r"<[^>]+>", " ", body_val)
                clean_body = re.sub(r"\s+", " ", clean_body).strip()
                pages.append({
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "space": (item.get("space") or {}).get("key"),
                    "url": f"{wiki_base}{((item.get('_links') or {}).get('webui') or '')}",
                    "version": (item.get("version") or {}).get("number"),
                    "body_storage": body_val,
                    "text_preview": clean_body[:2000],
                })
            return NodeResult("ok", data={
                "total": data.get("size", len(pages)),
                "cql": final_cql,
                "pages": pages,
            })
    except Exception as exc:
        return NodeResult("error", error=f"Error consultando Confluence: {exc}")


def http_health(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    timeout: float = 5.0,
    expected_status: int = 200,
) -> NodeResult:
    """Comprueba la disponibilidad y latencia de una URL."""
    if not url.strip():
        return NodeResult("error", error=tr('Missing URL for health check'))
    t0 = time.time()
    try:
        with httpx.Client(timeout=float(timeout), follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Dedalo-HealthCheck/1.0"})
            latency_ms = round((time.time() - t0) * 1000, 1)
            is_healthy = resp.status_code == int(expected_status)
            data = {
                "url": url,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "is_healthy": is_healthy,
            }
            status = "ok" if is_healthy else "error"
            err = "" if is_healthy else tr('HTTP status {status_code} != expected {expected_status}', status_code=resp.status_code, expected_status=expected_status)
            return NodeResult(status, data=data, error=err)
    except Exception as exc:
        latency_ms = round((time.time() - t0) * 1000, 1)
        return NodeResult(
            "error",
            data={"url": url, "status_code": 0, "latency_ms": latency_ms, "is_healthy": False},
            error=tr('Connection failure in health check: {exc}', exc=exc),
        )


def json_file(ctx: Any, inputs: list[NodeResult], *, path: str) -> NodeResult:
    """Lee un fichero JSON local."""
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=tr('File not found: {path}', path=path))
    try:
        return NodeResult("ok", data=json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        return NodeResult("error", error=tr('Error reading JSON: {exc}', exc=exc))


def disk_free(ctx: Any, inputs: list[NodeResult], *, path: str = "") -> NodeResult:
    """Devuelve el porcentaje de espacio libre en disco (float)."""
    target = path.strip() or os.path.expanduser("~")
    try:
        total, used, free = shutil.disk_usage(target)
        pct = (free / total) * 100.0 if total > 0 else 0.0
        return NodeResult("ok", data=round(pct, 2))
    except Exception as exc:
        return NodeResult("error", error=tr('Error measuring disk: {exc}', exc=exc))


def cpu_load(ctx: Any, inputs: list[NodeResult], *, seconds: float = 1.0) -> NodeResult:
    """Devuelve el uso de CPU aproximado."""
    try:
        # Medición simple mediante lectura de carga del sistema
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        pct = min(100.0, (load1 / cpu_count) * 100.0)
        return NodeResult("ok", data=round(pct, 2))
    except Exception as exc:
        return NodeResult("error", error=tr('Error measuring CPU: {exc}', exc=exc))


# ---------------------------------------------------------------------------
# Nodos de Transformación
# ---------------------------------------------------------------------------


def threshold(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    comparison: str,
    limit: Any = "",
) -> NodeResult:
    """Filtra según una condición numérica o textual. Si no se cumple, devuelve SKIPPED."""
    inp = Inputs(inputs)
    val = inp.first_data()
    if val is None:
        return SKIPPED

    # Comparaciones textuales y de presencia
    if comparison == "no_vacio":
        return NodeResult("ok", data=val) if bool(val) else SKIPPED
    if comparison == "es_vacio":
        return NodeResult("ok", data=val) if not bool(val) else SKIPPED

    str_val = str(val)
    str_limit = str(limit)

    if comparison == "contiene":
        return NodeResult("ok", data=val) if str_limit.lower() in str_val.lower() else SKIPPED
    if comparison == "no_contiene":
        return NodeResult("ok", data=val) if str_limit.lower() not in str_val.lower() else SKIPPED
    if comparison == "empieza_por":
        return NodeResult("ok", data=val) if str_val.startswith(str_limit) else SKIPPED
    if comparison == "termina_por":
        return NodeResult("ok", data=val) if str_val.endswith(str_limit) else SKIPPED
    if comparison == "regex":
        return NodeResult("ok", data=val) if re.search(str_limit, str_val) else SKIPPED
    if comparison == "igual_texto":
        return NodeResult("ok", data=val) if str_val == str_limit else SKIPPED

    # Comparaciones numéricas
    try:
        num = float(val)
        lim = float(limit)
    except (ValueError, TypeError):
        return NodeResult("error", error=tr('The input or limit value is not numeric: {val!r}, {limit!r}', val=val, limit=limit))

    ok = False
    if comparison == "menor_que":
        ok = num < lim
    elif comparison == "menor_o_igual":
        ok = num <= lim
    elif comparison == "mayor_que":
        ok = num > lim
    elif comparison == "mayor_o_igual":
        ok = num >= lim
    elif comparison in ("igual", "igual_num"):
        ok = num == lim
    elif comparison in ("distinto", "distinto_num"):
        ok = num != lim

    return NodeResult("ok", data=num) if ok else SKIPPED


def filter_items(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    contains: str,
    field: str = "",
    mode: str = "contains",
) -> NodeResult:
    """Filtra una lista de diccionarios o strings."""
    inp = Inputs(inputs)
    data = inp.first_data()
    if not isinstance(data, list):
        return NodeResult("error", error=tr('The input to filter must be a list'))

    term = (contains or "").lower()
    out = []
    for elem in data:
        target = ""
        raw_val = None
        if isinstance(elem, dict):
            raw_val = elem.get(field) if field else elem
            target = str(raw_val if raw_val is not None else "")
        else:
            raw_val = elem
            target = str(elem)

        matched = False
        if mode == "regex":
            matched = bool(re.search(contains, target, re.IGNORECASE))
        elif mode == "gt":
            try:
                matched = float(raw_val) > float(contains)
            except Exception:
                matched = False
        elif mode == "lt":
            try:
                matched = float(raw_val) < float(contains)
            except Exception:
                matched = False
        elif mode == "not_contains":
            matched = term not in target.lower()
        else:  # contains
            matched = term in target.lower()

        if matched:
            out.append(elem)

    return NodeResult("ok", data=out)


def clean_html(ctx: Any, inputs: list[NodeResult], *, url: str = "") -> NodeResult:
    """Elimina etiquetas HTML y limpia espacios."""
    inp = Inputs(inputs)
    text = inp.text()
    if not text:
        return NodeResult("ok", data="")

    # Limpieza simple por regex
    clean = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<style[^>]*>.*?</style>", "", clean, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return NodeResult("ok", data=clean)


class _SimpleDOMElement:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]], parent: _SimpleDOMElement | None = None):
        self.tag = tag.lower()
        self.attrs = {k.lower(): v or "" for k, v in attrs}
        self.parent = parent
        self.children: list[_SimpleDOMElement] = []
        self.text_chunks: list[str] = []

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def text(self) -> str:
        texts = list(self.text_chunks)
        for c in self.children:
            texts.append(c.text)
        return " ".join("".join(texts).split())


class _SimpleDOMParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = _SimpleDOMElement("root", [])
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        elem = _SimpleDOMElement(tag, attrs, self.current)
        self.current.children.append(elem)
        if tag.lower() not in ("meta", "link", "br", "hr", "img", "input", "source", "wbr", "area", "col"):
            self.current = elem

    def handle_endtag(self, tag: str):
        curr = self.current
        while curr and curr.tag != tag.lower() and curr.parent:
            curr = curr.parent
        if curr and curr.parent:
            self.current = curr.parent

    def handle_data(self, data: str):
        self.current.text_chunks.append(data)


def _match_dom_token(elem: _SimpleDOMElement, token: str) -> bool:
    if not token or elem.tag == "root":
        return False
    if "#" in token:
        parts = token.split("#", 1)
        tag, eid = parts[0], parts[1]
        if tag and elem.tag != tag.lower():
            return False
        return elem.id.lower() == eid.lower()
    if "." in token:
        parts = token.split(".")
        tag = parts[0]
        classes = parts[1:]
        if tag and elem.tag != tag.lower():
            return False
        return all(c.lower() in [x.lower() for x in elem.classes] for c in classes)
    return elem.tag == token.lower()


def _query_selector_all(root: _SimpleDOMElement, selector: str) -> list[_SimpleDOMElement]:
    tokens = selector.strip().split()
    if not tokens:
        return []
    current = [root]
    for token in tokens:
        next_gen = []
        for p in current:
            stack = list(p.children)
            while stack:
                node = stack.pop(0)
                if _match_dom_token(node, token):
                    next_gen.append(node)
                stack = list(node.children) + stack
        current = next_gen
    return current


def html_extract(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    selector: str,
    fields: str = "",
    output_format: str = "records",
    item_template: str = "",
    header_text: str = "",
    base_url: str = "",
    limit: int = 10,
) -> NodeResult:
    """Extrae datos de HTML utilizando selectores CSS ligeros sin dependencias externas."""
    inp = Inputs(inputs)
    html_content = inp.text()
    if not html_content:
        return NodeResult("ok", data=[] if output_format != "text" else "")

    parser = _SimpleDOMParser()
    try:
        parser.feed(html_content)
    except Exception as exc:
        return NodeResult("error", error=tr('Error processing HTML: {exc}', exc=exc))

    matched_elements = _query_selector_all(parser.root, selector)
    if limit and int(limit) > 0:
        matched_elements = matched_elements[: int(limit)]

    # Parsear fields si se proporcionaron
    fields_map: dict[str, str] = {}
    if fields:
        if isinstance(fields, dict):
            fields_map = fields
        else:
            try:
                fields_map = json.loads(fields)
            except Exception:
                for item in fields.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        fields_map[k.strip()] = v.strip()

    # Si no hay fields_map, extracción directa del selector
    if not fields_map:
        attr_name = None
        if "@" in selector:
            sel_clean, attr_name = selector.split("@", 1)
            matched_elements = _query_selector_all(parser.root, sel_clean)
            if limit and int(limit) > 0:
                matched_elements = matched_elements[: int(limit)]

        results_list = []
        for el in matched_elements:
            val = el.attrs.get(attr_name, "") if attr_name else el.text
            if attr_name == "href" and base_url and val:
                val = urllib.parse.urljoin(base_url, val)
            results_list.append(val)

        if output_format == "text":
            return NodeResult("ok", data="\n".join(results_list))
        return NodeResult("ok", data=results_list)

    # Con fields_map, extraer un registro (dict) por cada elemento contenedor
    records = []
    for container in matched_elements:
        rec = {}
        for f_name, f_sel in fields_map.items():
            attr_target = None
            sel_part = f_sel
            if "@" in f_sel:
                sel_part, attr_target = f_sel.split("@", 1)
            subs = _query_selector_all(container, sel_part)
            if subs:
                val = subs[0].attrs.get(attr_target, "") if attr_target else subs[0].text
                if attr_target == "href" and base_url and val:
                    val = urllib.parse.urljoin(base_url, val)
                rec[f_name] = val
            else:
                rec[f_name] = ""
        records.append(rec)

    if output_format == "text":
        tmpl = item_template or "• " + " | ".join(f"{{{k}}}" for k in fields_map)
        lines = []
        if header_text:
            h = header_text.replace("{fecha}", time.strftime("%Y-%m-%d")).replace("{hora}", time.strftime("%H:%M:%S"))
            lines.append(h.rstrip() + "\n")
        for rec in records:
            item_rendered = tmpl
            for k, v in rec.items():
                item_rendered = item_rendered.replace(f"{{{k}}}", str(v))
            lines.append(item_rendered.rstrip())
        return NodeResult("ok", data="\n\n".join(lines).strip())

    return NodeResult("ok", data=records)


def _extract_field_value(data: Any, field_path: str) -> Any:
    """Extrae un valor anidado usando notación punto o índice numérico (ej: query.pages.0.extract)."""
    if not field_path:
        return data
    curr = data
    for part in field_path.split("."):
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        elif isinstance(curr, (list, tuple)) and part.isdigit() and int(part) < len(curr):
            curr = curr[int(part)]
        else:
            return None
    return curr


def _find_field_recursive(data: Any, field_name: str) -> Any:
    """Busca recursivamente la primera coincidencia de una clave en un diccionario o lista."""
    if isinstance(data, dict):
        if field_name in data:
            return data[field_name]
        for v in data.values():
            found = _find_field_recursive(v, field_name)
            if found is not None:
                return found
    elif isinstance(data, (list, tuple)):
        for it in data:
            found = _find_field_recursive(it, field_name)
            if found is not None:
                return found
    return None


def template(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    template: str = "",
) -> NodeResult:
    """Interpola variables en una plantilla de texto."""
    inp = Inputs(inputs)
    context_vars: dict[str, Any] = {}
    text_parts: list[str] = []

    for it in inputs:
        if it.ok and it.data is not None:
            if isinstance(it.data, dict):
                context_vars.update(it.data)
            elif isinstance(it.data, str):
                text_parts.append(it.data)

    context_vars["entrada"] = "\n\n".join(text_parts) if text_parts else inp.text()
    context_vars["now"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    context_vars["fecha"] = time.strftime("%Y-%m-%d")
    context_vars["hora"] = time.strftime("%H:%M:%S")

    # Interpolación segura {clave}, {sub.clave} y búsqueda recursiva de respaldo
    def repl(match):
        expr = match.group(1).strip()
        parts = expr.split(".")
        curr = context_vars
        found_parts = True
        for p in parts:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            elif isinstance(curr, (list, tuple)) and p.isdigit() and int(p) < len(curr):
                curr = curr[int(p)]
            else:
                found_parts = False
                break
        if found_parts:
            return str(curr)

        if len(parts) == 1:
            fallback = _find_field_recursive(context_vars, expr)
            if fallback is not None:
                return str(fallback)

        return match.group(0)

    result = re.sub(r"\{([a-zA-Z0-9_.]+)\}", repl, template)
    return NodeResult("ok", data=result)


def array_ops(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    operation: str = "head",
    count: int = 10,
    field: str = "",
    order: str = "asc",
    join_sep: str = "\\n",
) -> NodeResult:
    """Operaciones comunes sobre listas (head, tail, sort, dedup, join)."""
    inp = Inputs(inputs)
    data = inp.first_data()
    if not isinstance(data, list):
        return NodeResult("error", error=tr('The array_ops input must be a list'))
    items = list(data)
    if operation == "head":
        items = items[:int(count)]
    elif operation == "tail":
        items = items[-int(count):]
    elif operation == "sort":
        reverse = order.lower() == "desc"
        if field:
            items.sort(key=lambda x: x.get(field, "") if isinstance(x, dict) else str(x), reverse=reverse)
        else:
            items.sort(reverse=reverse)
    elif operation == "dedup":
        seen = set()
        deduped = []
        for it in items:
            key = it.get(field, json.dumps(it)) if (isinstance(it, dict) and field) else (json.dumps(it) if isinstance(it, (dict, list)) else it)
            if key not in seen:
                seen.add(key)
                deduped.append(it)
        items = deduped
    elif operation == "join":
        sep = join_sep.replace("\\n", "\n").replace("\\t", "\t")
        joined = sep.join(str(it) if not isinstance(it, (dict, list)) else json.dumps(it, ensure_ascii=False) for it in items)
        return NodeResult("ok", data=joined)
    return NodeResult("ok", data=items)


def regex(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    pattern: str = "",
    action: str = "find_all",
    replace_with: str = "",
    field: str = "",
) -> NodeResult:
    """Aplica expresiones regulares para buscar o sustituir texto."""
    if not pattern:
        return NodeResult("error", error=tr('Missing regular expression pattern'))
    inp = Inputs(inputs)
    first = inp.first_data()
    target_text = ""
    if isinstance(first, dict) and field:
        target_text = str(first.get(field, ""))
    else:
        target_text = inp.text()
    try:
        rx = re.compile(pattern)
        if action == "find_all":
            matches = rx.findall(target_text)
            return NodeResult("ok", data=matches)
        elif action == "search":
            m = rx.search(target_text)
            return NodeResult("ok", data=m.group(0) if m else None)
        elif action == "sub":
            replaced = rx.sub(replace_with, target_text)
            return NodeResult("ok", data=replaced)
        return NodeResult("error", error=tr('Unknown regex action: {action}', action=action))
    except Exception as exc:
        return NodeResult("error", error=tr('Error evaluating regex: {exc}', exc=exc))


def router(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    condition: str = "truthy",
    field: str = "",
    value: str = "",
) -> NodeResult:
    """Enruta el flujo: si la condición se cumple emite por OK, si no emite por ERROR."""
    inp = Inputs(inputs)
    data = inp.first_data()
    target = data
    if isinstance(data, dict) and field:
        target = data.get(field)

    ok = False
    if condition == "truthy":
        ok = bool(target)
    elif condition == "falsy":
        ok = not bool(target)
    elif condition == "equals":
        ok = str(target) == str(value)
    elif condition == "not_equals":
        ok = str(target) != str(value)
    elif condition == "contains":
        ok = str(value).lower() in str(target).lower()

    if ok:
        return NodeResult("ok", data=data)
    else:
        return NodeResult("error", data=data, error=tr('Router condition not met ({condition})', condition=condition))


def agent(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt: str,
    llm: str = "",
    per_item: bool = False,
    as_field: str = "",
    tokens: int = DEFAULT_AGENT_TOKENS,
    max_context: int = 0,
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float | None = None,
    system_prompt: str = "",
    json_mode: bool = False,
) -> NodeResult:
    """Invoca al LLM con un prompt y los datos de entrada."""
    inp = Inputs(inputs)

    overrides: dict[str, Any] = {
        "url": url,
        "model": model,
        "secret": secret,
        "max_context": max_context,
    }
    if temperature is not None:
        overrides["temperature"] = temperature

    resolved = llms.resolve(name=llm, overrides=overrides)
    effective_url = resolved["url"]
    effective_model = resolved["model"]
    effective_secret = resolved["secret"]
    effective_temp = resolved["temperature"]
    effective_ctx = max_context if (max_context and int(max_context) > 0) else resolved["max_context"]

    def _compact_if_needed(content_text: str) -> str:
        ctx_limit = int(effective_ctx) if effective_ctx else 0
        if ctx_limit <= 0 or not content_text:
            return content_text
        threshold = int(ctx_limit * 0.90)
        overhead_tokens = int((len(system_prompt or "") + len(prompt or "")) / 3.5) + int(tokens or DEFAULT_AGENT_TOKENS)
        avail_content_tokens = max(30, threshold - overhead_tokens)
        current_content_tokens = int(len(content_text) / 3.5)
        if current_content_tokens > avail_content_tokens:
            target_chars = int(avail_content_tokens * 3.5)
            target_sentences = max(2, int(target_chars / 100))
            compacted = _run_luhn_plus(content_text, target_count=target_sentences, output_format="paragraph")
            if len(compacted) > target_chars:
                compacted = compacted[:target_chars] + "..."
            return tr((
                '[Content compacted with Luhn+ due to context limit ({current_content_tokens} -> {compacted} tokens)]:\n'
                '{compacted2}'
            ), current_content_tokens=current_content_tokens, compacted=int(len(compacted) / 3.5), compacted2=compacted)
        return content_text

    if per_item:
        first = inp.first_data()
        if not isinstance(first, list):
            return NodeResult("error", error=tr('The per_item mode requires an input list'))
        results = []
        field_name = as_field.strip()
        for item in first[:MAX_ITEMS]:
            item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
            if effective_ctx and int(effective_ctx) > 0:
                item_str = _compact_if_needed(item_str)
            user_msg = tr('{prompt}\n\nItem:\n{item_str}', prompt=prompt, item_str=item_str)
            try:
                ans = call_llm(
                    user_msg,
                    system=system_prompt,
                    llm=llm,
                    model=effective_model,
                    url=effective_url,
                    secret_name=effective_secret,
                    tokens=tokens,
                    temperature=float(effective_temp),
                    json_mode=bool(json_mode),
                )
                if field_name:
                    if isinstance(item, dict):
                        enriched = dict(item)
                        enriched[field_name] = ans
                    else:
                        enriched = {"item": item, field_name: ans}
                    results.append(enriched)
                else:
                    results.append(ans)
            except Exception as exc:
                return NodeResult("error", error=f"Error en llamada LLM per_item: {exc}")
        return NodeResult("ok", data=results)

    input_text = inp.text()
    if input_text and effective_ctx and int(effective_ctx) > 0:
        input_text = _compact_if_needed(input_text)
    user_msg = tr('{prompt}\n\nInput:\n{input_text}', prompt=prompt, input_text=input_text) if input_text else prompt
    try:
        ans = call_llm(
            user_msg,
            system=system_prompt,
            llm=llm,
            model=effective_model,
            url=effective_url,
            secret_name=effective_secret,
            tokens=tokens,
            temperature=float(effective_temp),
            json_mode=bool(json_mode),
        )
        return NodeResult("ok", data=ans)
    except Exception as exc:
        return NodeResult("error", error=f"Error en llamada LLM: {exc}")


_DEFAULT_ANALYZER_PROMPT = tr((
    'Analyze the provided telemetry, events or logs.\n'
    'Identify the root cause of the problem, classify its severity (critical, high, medium, low or false positive),\n'
    'indicate the affected components and summarize the finding clearly and technically.\n'
    'Return a structured report.'
))

_DEFAULT_PLANNER_PROMPT = tr((
    'Based on the provided diagnosis and root cause,\n'
    'generate a step-by-step action plan to resolve or mitigate the incident.\n'
    'Include preventive steps, verifications and suggested commands or configurations (declarative or safe).'
))

_SECRET_MASK_PATTERNS = [
    re.compile(r'(?i)(bearer\s+)[A-Za-z0-9_\-\.]{15,}'),
    re.compile(r'(?i)(password|passwd|pwd|secret|api[_-]?key|token)\s*[:=]\s*["\']?([^"\'\s,;]{4,})["\']?'),
]


def _mask_secrets_in_text(text: str) -> str:
    """Enmascara tokens, contraseñas y claves privadas en texto plano."""
    if not text:
        return ""
    result = text
    for pat in _SECRET_MASK_PATTERNS:
        result = pat.sub(r'\1 [REDACTED]', result)
    return result


def analyzer_agent(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt: str = "",
    system_prompt: str = "",
    llm: str = "",
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float = 0.2,
    tokens: int = DEFAULT_AGENT_TOKENS,
    sanitize_secrets: bool = True,
    per_item: bool = False,
    as_field: str = "",
    json_mode: bool = False,
) -> NodeResult:
    """Nodo Agente Analizador (diagnóstico y causa raíz sobre telemetría, alertas o incidencias)."""
    inp = Inputs(inputs)
    raw_text = inp.text()
    first = inp.first_data()

    if not raw_text and first is None:
        return NodeResult("error", error=tr('The analyzer node requires input data (telemetry or incident)'))

    analysis_instruction = prompt.strip() or _DEFAULT_ANALYZER_PROMPT
    sys_instruction = system_prompt.strip() or (
        tr('You are a senior cybersecurity and infrastructure analyst (SecOps / SRE). Your goal is to diagnose incidents, identify root causes precisely and assess impact without inventing facts not backed by the evidence.')
    )

    overrides: dict[str, Any] = {
        "url": url,
        "model": model,
        "secret": secret,
        "temperature": temperature,
    }
    resolved = llms.resolve(name=llm, overrides=overrides)

    # Extraer lista si viene de un nodo tipo input.jira_issues (dict con "issues")
    items_to_process = None
    if per_item:
        if isinstance(first, list):
            items_to_process = first
        elif isinstance(first, dict) and isinstance(first.get("issues"), list):
            items_to_process = first["issues"]
        elif isinstance(first, dict) and isinstance(first.get("pages"), list):
            items_to_process = first["pages"]

    if per_item and items_to_process is not None:
        results = []
        field_name = as_field.strip() or "analysis"
        for item in items_to_process[:MAX_ITEMS]:
            item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
            if sanitize_secrets:
                item_str = _mask_secrets_in_text(item_str)
            item_msg = tr('{analysis_instruction}\n\n=== ITEM / TICKET ===\n{item_str}', analysis_instruction=analysis_instruction, item_str=item_str)
            try:
                ans = call_llm(
                    item_msg,
                    system=sys_instruction,
                    llm=llm,
                    model=resolved["model"],
                    url=resolved["url"],
                    secret_name=resolved["secret"],
                    tokens=tokens,
                    temperature=float(resolved["temperature"]),
                    json_mode=bool(json_mode),
                )
                if isinstance(item, dict):
                    enriched = dict(item)
                    enriched[field_name] = ans
                    results.append(enriched)
                else:
                    results.append({"item": item, field_name: ans})
            except Exception as exc:
                return NodeResult("error", error=f"Error en llamada LLM per_item: {exc}")
        return NodeResult("ok", data=results)

    if sanitize_secrets:
        raw_text = _mask_secrets_in_text(raw_text)

    full_user_msg = tr('{analysis_instruction}\n\n=== EVIDENCE / TELEMETRY ===\n{raw_text}', analysis_instruction=analysis_instruction, raw_text=raw_text)

    try:
        ans = call_llm(
            full_user_msg,
            system=sys_instruction,
            llm=llm,
            model=resolved["model"],
            url=resolved["url"],
            secret_name=resolved["secret"],
            tokens=tokens,
            temperature=float(resolved["temperature"]),
            json_mode=bool(json_mode),
        )

        incident_meta = {}
        if isinstance(first, dict):
            incident_meta = {
                "incident_id": first.get("incident_id") or first.get("key"),
                "resource": first.get("resource"),
                "evidence_file": first.get("evidence_file") or first.get("vault_path"),
                "evidence_sha256": first.get("evidence_sha256") or first.get("sha256"),
            }

        parsed_json = None
        if json_mode:
            try:
                parsed_json = json.loads(ans)
            except Exception:
                pass

        return NodeResult("ok", data={
            "analysis": ans,
            "parsed": parsed_json,
            "meta": incident_meta,
            "sanitized": sanitize_secrets,
        })
    except Exception as exc:
        return NodeResult("error", error=f"Error en agente analizador: {exc}")


def planner_agent(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt: str = "",
    system_prompt: str = "",
    llm: str = "",
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float = 0.2,
    tokens: int = DEFAULT_AGENT_TOKENS,
    output_format: str = "markdown",
    json_mode: bool = False,
) -> NodeResult:
    """Nodo Agente Planificador (genera plan de acción, mitigación o remediación)."""
    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    if not raw_text and not first:
        return NodeResult("error", error=tr('The planner node requires input data (previous diagnosis)'))

    diagnosis_context = ""
    if isinstance(first, dict) and "analysis" in first:
        diagnosis_context = str(first["analysis"])
    else:
        diagnosis_context = raw_text

    plan_instruction = prompt.strip() or _DEFAULT_PLANNER_PROMPT
    sys_instruction = system_prompt.strip() or (
        tr('You are an infrastructure and security operations and remediation architect. You design clear, safe, verifiable, step-by-step mitigation plans. Always distinguish between immediate containment actions and definitive solutions.')
    )

    user_msg = tr('{plan_instruction}\n\n=== DIAGNOSIS / ROOT CAUSE ===\n{diagnosis_context}', plan_instruction=plan_instruction, diagnosis_context=diagnosis_context)
    if output_format == "json" or json_mode:
        user_msg += tr((
            '\n'
            '\n'
            "Respond in structured JSON format with the fields 'summary', 'immediate_steps', 'long_term_steps' and 'verification'."
        ))
        json_mode = True

    overrides: dict[str, Any] = {
        "url": url,
        "model": model,
        "secret": secret,
        "temperature": temperature,
    }
    resolved = llms.resolve(name=llm, overrides=overrides)

    try:
        ans = call_llm(
            user_msg,
            system=sys_instruction,
            llm=llm,
            model=resolved["model"],
            url=resolved["url"],
            secret_name=resolved["secret"],
            tokens=tokens,
            temperature=float(resolved["temperature"]),
            json_mode=bool(json_mode),
        )

        parsed_plan = None
        if json_mode:
            try:
                parsed_plan = json.loads(ans)
            except Exception:
                pass

        return NodeResult("ok", data={
            "plan": ans,
            "parsed": parsed_plan,
            "format": output_format,
        })
    except Exception as exc:
        return NodeResult("error", error=f"Error en agente planificador: {exc}")


# ---------------------------------------------------------------------------
# Algoritmo Luhn+ y Resumen Léxico (Compartido)
# ---------------------------------------------------------------------------

_SUMMARIZE_STOPWORDS: frozenset[str] = frozenset({
    # Español
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "a", "al", "en", "para", "por",
    "con", "sin", "sobre", "entre", "hasta", "desde", "hacia", "tras", "durante", "mediante", "contra",
    "y", "e", "ni", "o", "u", "pero", "sino", "aunque", "porque", "pues", "que", "como", "cuando",
    "donde", "quien", "quienes", "cual", "cuales", "cuyo", "cuya", "cuyos", "cuyas",
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas", "aquel", "aquella", "aquellos", "aquellas",
    "su", "sus", "mi", "mis", "tu", "tus", "nuestro", "nuestra", "nuestros", "nuestras",
    "ser", "es", "son", "era", "eran", "fue", "fueron", "sido", "estar", "esta", "estan", "estaba", "estaban",
    "haber", "hay", "habia", "habian", "hubo", "hacer", "hace", "hizo", "hicieron",
    "tener", "tiene", "tienen", "tenia", "tenian", "tuvo", "tuvieron",
    "mas", "menos", "muy", "mucho", "mucha", "muchos", "muchas", "poco", "poca", "pocos", "pocas",
    "tambien", "tampoco", "ya", "no", "si", "todo", "toda", "todos", "todas", "otro", "otra", "otros", "otras",
    "mismo", "misma", "mismos", "mismas", "segun", "ademas", "asi", "cada", "tanto", "tanta", "tan",
    # English
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "with", "by", "from", "up",
    "about", "into", "over", "after", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "can", "could", "shall", "should", "will", "would", "may", "might",
    "must", "it", "its", "they", "them", "their", "this", "that", "these", "those", "which", "who",
    "whom", "what", "there", "where", "when", "why", "how", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "than", "too", "very", "also", "only"
})


def _format_summary_output(sentences: list[str], output_format: str = "paragraph") -> str:
    fmt = (output_format or "paragraph").lower().strip()
    if fmt in ("bullets", "bullet", "viñetas", "vineta", "list", "lista"):
        return "\n".join(f"- {s}" for s in sentences)
    return " ".join(sentences)


def _run_luhn_plus(
    text: str,
    target_count: int = 6,
    output_format: str = "paragraph",
    sentences: list[str] | None = None,
) -> str:
    """Ejecuta el algoritmo Luhn+ (filtro de stopwords, normalización por longitud, sesgo posicional y MMR)."""
    if not text or not text.strip():
        return ""

    if sentences is not None and len(sentences) > 0:
        sentence_list = list(sentences)
    else:
        cleaned_text = re.sub(r"(?m)^\s*=+[^\n=]+=+\s*$", "", text)
        cleaned_text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", cleaned_text)

        protected = re.sub(r"\b(Dr|Dra|Sr|Sra|Srta|Prof|D|Dña)\.", r"\1§DOT§", cleaned_text, flags=re.IGNORECASE)
        protected = re.sub(r"\b(p)\.\s*(ej)\.", r"\1§DOT§ \2§DOT§", protected, flags=re.IGNORECASE)
        protected = re.sub(r"\b(art|núm|apdo|cap|vol|vs|fig)\.", r"\1§DOT§", protected, flags=re.IGNORECASE)
        protected = re.sub(r"\b([aAdD])\.\s*([cC])\.", r"\1§DOT§ \2.", protected)
        protected = re.sub(r"\b([eE][eE])\.\s*([uU][uU])\.", r"\1§DOT§ \2§DOT§", protected)
        protected = re.sub(r"\b([cC][aA]?)\.", r"\1§DOT§", protected)

        raw_parts = [p.strip() for p in re.split(r"\n+", protected) if p.strip()]
        sentence_list = []
        for part in raw_parts:
            for s in re.split(r"(?<=[.?!])\s+", part):
                s = s.replace("§DOT§", ".").strip()
                if len(s) >= 15 and len(s.split()) >= 4:
                    sentence_list.append(s)

        if not sentence_list:
            sentence_list = [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]

    if not sentence_list:
        return text

    target_count = min(max(1, int(target_count)), len(sentence_list))

    sig_words_all = [
        w for w in re.findall(r"\b\w{3,}\b", text.lower())
        if w not in _SUMMARIZE_STOPWORDS and not w.isdigit()
    ]
    freq = Counter(sig_words_all)
    sent_sig_words = [
        [w for w in re.findall(r"\b\w{3,}\b", s.lower()) if w not in _SUMMARIZE_STOPWORDS and not w.isdigit()]
        for s in sentence_list
    ]

    selected_indices: list[int] = []
    current_freq = dict(freq)

    for _ in range(target_count):
        best_score = -1.0
        best_idx = -1
        for i, (s, w_list) in enumerate(zip(sentence_list, sent_sig_words)):
            if i in selected_indices:
                continue
            raw_score = sum(current_freq.get(w, 0) for w in w_list) if w_list else 0.1
            score = raw_score / math.sqrt(max(1, len(w_list)))

            # Sesgo de posición (Lead Bias)
            if i == 0:
                score *= 1.5
            elif i == 1:
                score *= 1.25
            elif i == 2:
                score *= 1.1

            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx != -1:
            selected_indices.append(best_idx)
            for w in sent_sig_words[best_idx]:
                current_freq[w] = current_freq.get(w, 0) * 0.5

    selected_indices.sort()
    chosen = [sentence_list[i] for i in selected_indices]
    return _format_summary_output(chosen, output_format)


def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estima de forma ultraligera el número de tokens en la lista de mensajes (~3.5 caracteres/token)."""
    total_chars = 0
    for m in messages:
        content = m.get("content")
        if content:
            total_chars += len(str(content))
        tool_calls = m.get("tool_calls")
        if tool_calls:
            total_chars += len(json.dumps(tool_calls))
    return max(1, int(total_chars / 3.5))


def _is_tool_failure(tool_name: str, result_str: str) -> bool:
    """Determina deterministamente si el resultado de una herramienta representa un fallo o error."""
    if not result_str:
        return False
    r = result_str.strip()
    if r.startswith("Error:") or r.startswith("Error ") or r.startswith("Error\n"):
        return True
    if "Security error" in r or "Error de seguridad" in r:
        return True
    if "'old_str' not found" in r or "No se encontró 'old_str'" in r or "aparece repetido" in r or ("aparece" in r and "veces en" in r):
        return True
    if "is not available or is not authorized" in r or "no está disponible o no está autorizada" in r:
        return True
    if tool_name == "bash":
        match = re.search(r"\[(?:Exit code|Código de salida):\s*(-?\d+)\]", r)
        if match and match.group(1) != "0":
            return True
        if "command not found" in r.lower() or "traceback (most recent call last)" in r.lower():
            return True
    return False


def _extract_blackhole_summary(
    middle: list[dict[str, Any]],
    user_goal: str = "",
) -> str:
    """Extrae de forma 100% determinista un resumen estructurado (VCC) a partir de los turnos intermedios."""
    files_modified: list[str] = []
    files_read: list[str] = []
    files_explored: list[str] = []
    actions: list[str] = []
    errors: list[str] = []
    user_notes: list[str] = []

    for m in middle:
        role = m.get("role")
        content = str(m.get("content") or "").strip()

        if role == "user" and content:
            user_notes.append(content[:160])

        elif role == "assistant":
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                fn_name = fn.get("name", "")
                raw_args = fn.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except Exception:
                        args = {}
                else:
                    args = raw_args or {}

                if fn_name in ("write", "edit"):
                    path = str(args.get("path", "")).strip()
                    if path and path not in files_modified:
                        files_modified.append(path)
                    actions.append(f"{fn_name}('{path}')")
                elif fn_name == "read":
                    path = str(args.get("path", "")).strip()
                    if path and path not in files_read:
                        files_read.append(path)
                    lines_info = f" L{args.get('start_line', 1)}-{args.get('end_line')}" if args.get("end_line") else ""
                    actions.append(f"read('{path}'{lines_info})")
                elif fn_name in ("grep", "find", "ls"):
                    p = str(args.get("pattern") or args.get("path") or ".").strip()
                    if p and p not in files_explored:
                        files_explored.append(p)
                    actions.append(f"{fn_name}('{p}')")
                elif fn_name == "bash":
                    cmd = str(args.get("command", "")).strip()
                    actions.append(f"bash('{cmd[:80]}')")

        elif role == "tool":
            fn_name = m.get("name", "tool")
            if _is_tool_failure(fn_name, content):
                first_err_line = next((line.strip() for line in content.splitlines() if line.strip()), content[:120])
                if first_err_line and first_err_line not in errors:
                    errors.append(f"{fn_name}: {first_err_line[:140]}")

    sections = [tr('[Compacted session memory (Blackhole Mode)]:')]

    # 1. Objetivo
    goal_line = user_goal.strip() if user_goal else ""
    if goal_line:
        primary_goal = goal_line.splitlines()[0][:200]
        sections.append(f"### Objetivo Principal\n- {primary_goal}")
    if user_notes:
        sections.append(tr('### User Intermediate Instructions\n') + "\n".join(f"- {note}" for note in user_notes[-3:]))

    # 2. Archivos intervenidos
    files_sec = []
    if files_modified:
        files_sec.append(tr('- Modified / Created: {files_modified}', files_modified=', '.join(files_modified)))
    if files_read:
        files_sec.append(tr('- Read: {files_read}', files_read=', '.join(files_read)))
    if files_explored:
        files_sec.append(tr('- Explored / Listed: {files_explored}', files_explored=', '.join(files_explored[:6])))
    if files_sec:
        sections.append(tr('### Files and Paths\n') + "\n".join(files_sec))

    # 3. Acciones ejecutadas (últimas 10 acciones)
    if actions:
        recent_actions = actions[-10:] if len(actions) > 10 else actions
        prefix = tr('(... {actions} earlier actions omitted ...)\n', actions=len(actions) - 10) if len(actions) > 10 else ""
        sections.append(tr('### Recent Action Log\n') + prefix + "\n".join(f"- {act}" for act in recent_actions))

    # 4. Errores detectados
    if errors:
        recent_errors = errors[-4:]
        sections.append(tr('### Errors and Incidents Detected\n') + "\n".join(f"- {err}" for err in recent_errors))
    else:
        sections.append(tr('### Error Status\n- No persistent errors recorded.'))

    return "\n\n".join(sections)


def _synthesize_with_local_llm(
    client: Any,
    endpoint: str,
    headers: dict[str, str],
    target_model: str,
    temperature: float,
    blackhole_summary: str,
) -> str:
    """Realiza una única llamada al LLM local ya cargado para sintetizar 2-3 reflexiones clave."""
    prompt = (
        tr((
            "You are the assistant of Dédalo's compaction engine. From this structured session record, synthesize in at most 3 brief bullets (1 or 2 lines each) the key architectural decisions made, the confirmed findings and the current state in order to continue the task:\n"
            '\n'
            '{blackhole_summary}'
        ), blackhole_summary=blackhole_summary)
    )
    payload = {
        "model": target_model,
        "messages": [
            {"role": "system", "content": tr('Be extremely concise and factual. Return only bullets with dashes.')},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 250,
    }
    resp = client.post(endpoint, headers=headers, json=payload, timeout=15.0)
    if resp.status_code == 200:
        data = resp.json()
        content = (data["choices"][0]["message"].get("content") or "").strip()
        if content:
            return content
    return ""


def _compact_conversation_history(
    messages: list[dict[str, Any]],
    context_window: int,
    preserve_recent_turns: int = 1,
    engine: str = "luhn+",
    client: Any = None,
    endpoint: str = "",
    headers: dict[str, str] | None = None,
    target_model: str = "",
    temperature: float = 0.1,
) -> tuple[list[dict[str, Any]], bool]:
    """Si los mensajes superan el 90% del contexto soportado, compacta el historial previo según el motor configurado."""
    threshold = int(context_window * 0.90)
    current_tokens = _estimate_messages_tokens(messages)
    if current_tokens < threshold:
        return messages, False

    did_compact = False
    new_msgs = list(messages)
    eng = (engine or "luhn+").lower().strip()

    # 1. Compactar mensajes de herramientas que individualmente superen el umbral o sean desproporcionados
    for idx, m in enumerate(new_msgs):
        if m.get("role") == "tool" and m.get("content"):
            c_text = str(m["content"])
            # Si el contenido de la tool supera el 40% del umbral de contexto
            if len(c_text) > int(threshold * 1.5):
                if eng in ("luhn", "luhn+", "luhn_plus", "luhnplus"):
                    summary = _run_luhn_plus(c_text, target_count=8, output_format="bullets")
                    compacted_m = dict(m)
                    compacted_m["content"] = tr('[Tool output compacted with Luhn+ (90% context exceeded)]:\n{summary}', summary=summary)
                    new_msgs[idx] = compacted_m
                    did_compact = True
                else:
                    lines = c_text.splitlines()
                    if len(lines) > 30:
                        folded = "\n".join(lines[:10]) + tr('\n\n[... {lines} lines omitted by Blackhole compactor ...]\n\n', lines=len(lines) - 20) + "\n".join(lines[-10:])
                    else:
                        folded = c_text[:1000] + tr('\n\n[... {c_text} characters omitted by Blackhole compactor ...]\n\n', c_text=max(0, len(c_text) - 1500)) + c_text[-500:]
                    compacted_m = dict(m)
                    compacted_m["content"] = tr('[Tool output collapsed by Blackhole (context exceeded)]:\n{folded}', folded=folded)
                    new_msgs[idx] = compacted_m
                    did_compact = True

    # Si tras compactar herramientas ya bajamos del 90%, retornamos
    if did_compact and _estimate_messages_tokens(new_msgs) < threshold:
        return new_msgs, True

    # 2. Compactar turnos intermedios si el historial se ha acumulado
    if len(new_msgs) > 4:
        head = new_msgs[:2]  # system + user prompt original
        user_goal = str(head[1].get("content") or "") if len(head) > 1 else ""
        tail_len = min(len(new_msgs) - 2, max(2, preserve_recent_turns * 2))
        tail = new_msgs[-tail_len:]
        middle = new_msgs[2:-tail_len]
        if middle:
            if eng in ("luhn", "luhn+", "luhn_plus", "luhnplus"):
                chunks = []
                for m in middle:
                    role = m.get("role")
                    content = m.get("content")
                    name = m.get("name")
                    if role == "assistant":
                        if content:
                            chunks.append(f"Asistente: {content}")
                        if m.get("tool_calls"):
                            fn_names = [c.get("function", {}).get("name", "") for c in m["tool_calls"]]
                            chunks.append(f"Llamadas a herramientas: {', '.join(filter(None, fn_names))}")
                    elif role == "tool":
                        chunks.append(tr('Tool {tool}:\n{content}', tool=name or 'tool', content=content))
                    elif role == "user":
                        chunks.append(tr('User: {content}', content=content))

                raw_middle = "\n\n".join(chunks)
                summary_bullets = _run_luhn_plus(raw_middle, target_count=8, output_format="bullets")
                summary_content = (
                    tr((
                        '[Summarized memory of previous progress via Luhn+ (90% of context reached: {current_tokens}/{context_window} tokens)]:\n'
                        '{summary_bullets}'
                    ), current_tokens=current_tokens, context_window=context_window, summary_bullets=summary_bullets)
                )
            elif eng in ("blackhole_llm", "blackhole+llm", "mixed"):
                bh_summary = _extract_blackhole_summary(middle, user_goal=user_goal)
                reflections = ""
                if client and endpoint and target_model:
                    try:
                        reflections = _synthesize_with_local_llm(
                            client=client,
                            endpoint=endpoint,
                            headers=headers or {},
                            target_model=target_model,
                            temperature=temperature,
                            blackhole_summary=bh_summary,
                        )
                    except Exception:
                        reflections = ""
                if reflections:
                    bh_summary += tr('\n\n### Key Reflections and Decisions (local LLM)\n{reflections}', reflections=reflections)

                summary_content = (
                    tr((
                        '[Summarized memory of previous progress via LLM Blackhole (90% of context reached: {current_tokens}/{context_window} tokens)]:\n'
                        '{bh_summary}'
                    ), current_tokens=current_tokens, context_window=context_window, bh_summary=bh_summary)
                )
            else:  # "blackhole" por defecto
                bh_summary = _extract_blackhole_summary(middle, user_goal=user_goal)
                summary_content = (
                    tr((
                        '[Summarized memory of previous progress via Blackhole (90% of context reached: {current_tokens}/{context_window} tokens)]:\n'
                        '{bh_summary}'
                    ), current_tokens=current_tokens, context_window=context_window, bh_summary=bh_summary)
                )

            summary_msg = {
                "role": "user",
                "content": summary_content,
            }
            ack_msg = {
                "role": "assistant",
                "content": tr('Understood. I keep the compacted memory of the previous actions and continue.'),
            }

            new_msgs = [*head, summary_msg, ack_msg, *tail]
            did_compact = True

    return new_msgs, did_compact


def _detect_context_window(
    base_url: str,
    target_model: str,
    api_key: str = "",
    explicit_ctx: int = 0,
    timeout: float = 3.0,
) -> int:
    """Detecta el tamaño de la ventana de contexto que soporta el servidor de inferencia."""
    if explicit_ctx and explicit_ctx > 0:
        return int(explicit_ctx)

    # 1. Probar endpoint de Ollama (/api/show)
    ollama_root = re.sub(r"/v1/?$", "", base_url)
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{ollama_root}/api/show", json={"name": target_model})
            if resp.status_code == 200:
                data = resp.json()
                params = data.get("parameters", "")
                m = re.search(r"num_ctx\s+(\d+)", params)
                if m:
                    return int(m.group(1))
                model_info = data.get("model_info", {})
                for k, v in model_info.items():
                    if k.endswith(".context_length") and isinstance(v, (int, float)):
                        return int(v)
    except Exception:
        pass

    # 2. Probar endpoint OpenAI / OpenRouter (/models)
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url}/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                models_list = data.get("data") if isinstance(data, dict) else data
                if isinstance(models_list, list):
                    for item in models_list:
                        if isinstance(item, dict):
                            model_id = str(item.get("id", ""))
                            if model_id == target_model or target_model in model_id:
                                ctx = item.get("context_length") or item.get("max_model_len") or item.get("context_window")
                                if ctx and isinstance(ctx, (int, float)):
                                    return int(ctx)
    except Exception:
        pass

    # 3. Heurística según familia de modelo
    m_lower = target_model.lower()
    if any(k in m_lower for k in ("gpt-4o", "gpt-4-turbo", "o1", "o3")):
        return 128000
    if "gpt-4" in m_lower:
        return 8192
    if "gpt-3.5" in m_lower:
        return 16385
    if "claude" in m_lower:
        return 200000
    if "deepseek" in m_lower:
        return 64000
    if "qwen2.5" in m_lower or "qwen-2.5" in m_lower:
        return 32768
    if any(k in m_lower for k in ("llama-3.1", "llama3.1", "llama-3.2", "llama3.2", "llama-3.3", "llama3.3")):
        return 131072
    if "llama3" in m_lower or "llama-3" in m_lower:
        return 8192
    if "mistral" in m_lower or "mixtral" in m_lower:
        return 32768
    if "phi3" in m_lower or "phi-3" in m_lower or "phi4" in m_lower:
        return 128000

    # 4. Fallback estándar conservador
    return 8192


# ---------------------------------------------------------------------------
# Microarnés de Agente Programador (transform.coder) y Herramientas
# ---------------------------------------------------------------------------

ALL_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write", "grep", "find", "ls", "web_search")
CODING_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write")
READ_ONLY_TOOL_NAMES: tuple[str, ...] = ("read", "grep", "find", "ls")


def _resolve_safe_path(base_dir: Path, rel_path: str) -> Path:
    """Resuelve una ruta y garantiza que no escape del directorio base (anti path-traversal)."""
    clean_rel = rel_path.strip() if rel_path else "."
    target = (base_dir / clean_rel).resolve()
    if target != base_dir and not target.is_relative_to(base_dir):
        raise PermissionError(tr("Path outside the allowed workspace: '{rel_path}'", rel_path=rel_path))
    return target


def _clip_tool_output(text: str, max_chars: int = 4000) -> str:
    """Trunca el texto de salida preservando la cabecera y el final del mensaje."""
    s = str(text)
    if len(s) <= max_chars:
        return s
    head_len = (max_chars - 60) // 2
    tail_len = max_chars - 60 - head_len
    return tr('{head_len}\n... [truncated {max_chars} characters] ...\n{tail_len}', head_len=s[:head_len], max_chars=len(s) - max_chars, tail_len=s[-tail_len:])


def _tool_read(base_dir: Path, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Lee el contenido de un archivo dentro de base_dir con numeración de líneas opcional."""
    if not path or not path.strip():
        return tr("Error: You must specify the 'path' parameter.")
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    if not target.exists():
        return tr("Error: The file '{path}' does not exist.", path=path)
    if target.is_dir():
        return tr("Error: '{path}' is a directory. Use the 'ls' tool to see its contents.", path=path)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return tr("Error reading '{path}': {exc}", path=path, exc=exc)

    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return tr("File '{path}' (empty, 0 lines).", path=path)

    start = max(1, int(start_line or 1))
    end = min(total_lines, int(end_line or total_lines)) if end_line is not None else total_lines
    if start > total_lines:
        return tr('File has {total_lines} lines. Requested range ({start}-{end}) is out of bounds.', total_lines=total_lines, start=start, end=end)
    selected = lines[start - 1 : end]
    numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start=start)]
    header = tr('File: {path} (lines {start}-{end} of {total_lines})', path=path, start=start, end=end, total_lines=total_lines)
    return header + "\n" + "\n".join(numbered)


def _tool_bash(base_dir: Path, command: str, timeout: float = 30.0) -> str:
    """Ejecuta un comando en la consola dentro de base_dir y retorna la salida combinada."""
    if not command or not command.strip():
        return tr('Error: Empty command.')
    try:
        proc = sbx.run_shell(command, kind="coder", timeout=timeout, cwd=base_dir)
        parts = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            parts.append(f"[STDERR]\n{proc.stderr}")
        parts.append(f"[Exit code: {proc.returncode}]")  # marcador fijo: _is_tool_failure lo interpreta
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return tr('Error: The command exceeded the time limit of {timeout} seconds.', timeout=timeout)
    except sbx.SandboxUnavailable as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return tr('Error running command: {exc}', exc=exc)


def _tool_edit(base_dir: Path, path: str, old_str: str, new_str: str) -> str:
    """Edita un archivo reemplazando una única ocurrencia exacta de old_str por new_str."""
    if not path or not path.strip():
        return tr("Error: You must specify the 'path' parameter.")
    if not old_str:
        return tr("Error: 'old_str' cannot be empty.")
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    if not target.exists():
        return tr("Error: The file '{path}' does not exist.", path=path)
    if target.is_dir():
        return tr("Error: '{path}' is a directory.", path=path)
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return tr("Error reading '{path}': {exc}", path=path, exc=exc)

    count = content.count(old_str)
    if count == 0:
        return tr("Error: 'old_str' not found in '{path}'. Check characters, line breaks and indentation.", path=path)
    if count > 1:
        return tr("Error: 'old_str' appears {count} times in '{path}'. It must be unique to edit safely. Add more surrounding context.", count=count, path=path)

    updated = content.replace(old_str, new_str, 1)
    try:
        target.write_text(updated, encoding="utf-8")
        return tr("Success: File '{path}' edited successfully.", path=path)
    except Exception as exc:
        return tr("Error saving changes to '{path}': {exc}", path=path, exc=exc)


def _tool_write(base_dir: Path, path: str, content: str) -> str:
    """Crea o sobreescribe un archivo en base_dir."""
    if not path or not path.strip():
        return tr("Error: You must specify the 'path' parameter.")
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return tr("Success: File '{path}' saved ({content} characters).", path=path, content=len(content))
    except Exception as exc:
        return tr("Error writing to '{path}': {exc}", path=path, exc=exc)


def _tool_grep(base_dir: Path, pattern: str, path: str = ".", case_insensitive: bool = False) -> str:
    """Busca un patrón de texto o expresión regular dentro de base_dir omitiendo carpetas binarias o caches."""
    if not pattern or not pattern.strip():
        return tr("Error: You must specify the 'pattern' parameter.")
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    if not target.exists():
        return tr("Error: The path '{path}' does not exist.", path=path)

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return tr("Error in regular expression syntax '{pattern}': {exc}", pattern=pattern, exc=exc)

    ignored_names = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "dist", "build"}
    matches = []
    max_matches = 50

    if target.is_file():
        files_to_scan = [target]
    else:
        files_to_scan = []
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in ignored_names and not d.startswith(".")]
            for f in sorted(files):
                if f.startswith("."):
                    continue
                files_to_scan.append(Path(root) / f)
                if len(files_to_scan) > 1000:
                    break

    for fpath in files_to_scan:
        try:
            rel = fpath.relative_to(base_dir)
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            for idx, line in enumerate(text.splitlines(), start=1):
                if rx.search(line):
                    matches.append(f"{rel}:{idx}: {line.strip()[:200]}")
                    if len(matches) >= max_matches:
                        return "\n".join(matches) + tr('\n... (limit of {max_matches} matches reached)', max_matches=max_matches)
        except Exception:
            continue

    if not matches:
        return tr("No matches for '{pattern}' in '{path}'.", pattern=pattern, path=path)
    return "\n".join(matches)


def _tool_find(base_dir: Path, pattern: str = "*", path: str = ".") -> str:
    """Busca archivos o carpetas por patrón glob en base_dir."""
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    if not target.exists():
        return tr("Error: The path '{path}' does not exist.", path=path)
    if not target.is_dir():
        return tr("Error: '{path}' is not a directory.", path=path)

    ignored_names = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", "dist", "build"}
    pat = pattern.strip() if pattern and pattern.strip() else "*"
    matches = []
    max_results = 100

    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if d not in ignored_names and not d.startswith(".")]
        p_root = Path(root)
        for d in sorted(dirs):
            if Path(d).match(pat):
                try:
                    matches.append(str((p_root / d).relative_to(base_dir)) + "/")
                except ValueError:
                    pass
        for f in sorted(files):
            if f.startswith("."):
                continue
            if Path(f).match(pat):
                try:
                    matches.append(str((p_root / f).relative_to(base_dir)))
                except ValueError:
                    pass
            if len(matches) >= max_results:
                return "\n".join(matches) + tr('\n... ({max_results} item limit reached)', max_results=max_results)

    if not matches:
        return tr("No files found matching the pattern '{pat}' in '{path}'.", pat=pat, path=path)
    return "\n".join(matches)


def _tool_ls(base_dir: Path, path: str = ".") -> str:
    """Lista el contenido de un directorio en base_dir con tamaños."""
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return tr('Security error: {pe}', pe=pe)
    if not target.exists():
        return tr("Error: The path '{path}' does not exist.", path=path)
    if target.is_file():
        size = target.stat().st_size
        return f"[FILE] {path} ({size} bytes)"

    ignored_names = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    entries = []
    try:
        items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for item in items:
            if item.name in ignored_names:
                continue
            if item.is_dir():
                entries.append(f"[DIR]  {item.name}/")
            else:
                entries.append(f"[FILE] {item.name} ({item.stat().st_size} bytes)")
            if len(entries) >= 150:
                entries.append(tr('... (more items truncated)'))
                break
    except Exception as exc:
        return tr("Error listing '{path}': {exc}", path=path, exc=exc)

    return "\n".join(entries) if entries else tr('(empty directory)')


def _tool_web_search(query: str, limit: int = 5, timeout: float = 30.0) -> str:
    """Busca en internet usando DuckDuckGo (sin API key) y devuelve los resultados formateados en Markdown."""
    if not query or not query.strip():
        return tr("Error: You must specify the 'query' parameter.")
    try:
        max_l = max(1, min(int(limit or 5), 10))
        res = web_search(None, [], query=query.strip(), engine="duckduckgo", limit=max_l, output_format="markdown", timeout=timeout)
        if res.ok:
            return str(res.data) if res.data else tr('No results found for the search.')
        return tr('Web search error: {error}', error=res.error)
    except Exception as exc:
        return tr('Error running web search: {exc}', exc=exc)


CODER_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "read": {
        "type": "function",
        "function": {
            "name": "read",
            "description": tr('Reads the contents of a file within the workspace with optional line numbering.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": tr('Relative path of the file to read.'),
                    },
                    "start_line": {
                        "type": "integer",
                        "description": tr('Starting line to read (1-indexed, optional).'),
                    },
                    "end_line": {
                        "type": "integer",
                        "description": tr('Ending line to read (1-indexed, optional).'),
                    },
                },
                "required": ["path"],
            },
        },
    },
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": tr('Runs a command in the console within the workspace and returns the combined output and exit code.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": tr("Shell command to run (e.g. 'pytest tests/', 'ruff check .')."),
                    },
                },
                "required": ["command"],
            },
        },
    },
    "edit": {
        "type": "function",
        "function": {
            "name": "edit",
            "description": tr("Edits a file by replacing one exact, unique occurrence of 'old_str' with 'new_str'."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": tr('Relative path of the file to edit.'),
                    },
                    "old_str": {
                        "type": "string",
                        "description": tr('Exact existing text string to be replaced.'),
                    },
                    "new_str": {
                        "type": "string",
                        "description": tr("New text string that will replace 'old_str'."),
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    "write": {
        "type": "function",
        "function": {
            "name": "write",
            "description": tr('Creates or overwrites an entire file with the given content in the workspace.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": tr('Relative path of the file to write.'),
                    },
                    "content": {
                        "type": "string",
                        "description": tr('Full content of the file to save.'),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": tr('Searches for a text pattern or regular expression in project files, skipping binary or dependency folders.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": tr('Pattern or regular expression to search for.'),
                    },
                    "path": {
                        "type": "string",
                        "description": tr("Relative path to search in (default '.')."),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": tr('Whether the search should ignore case (default False).'),
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    "find": {
        "type": "function",
        "function": {
            "name": "find",
            "description": tr('Searches for files and directories matching a glob pattern in the workspace.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": tr("Glob pattern (e.g. '*.py', '*test*'). Default '*'."),
                    },
                    "path": {
                        "type": "string",
                        "description": tr("Initial relative search path (default '.')."),
                    },
                },
            },
        },
    },
    "ls": {
        "type": "function",
        "function": {
            "name": "ls",
            "description": tr('Lists the contents of a directory in the workspace, indicating whether each is a file or folder and its size.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": tr("Relative path to list (default '.')."),
                    },
                },
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": tr('Searches the internet for technical information or documentation via DuckDuckGo without needing an API key. Returns titles, URLs and result summaries.'),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": tr('Query or search terms to look up on the web.'),
                    },
                    "limit": {
                        "type": "integer",
                        "description": tr('Maximum number of results to retrieve (default 5, maximum 10).'),
                    },
                },
                "required": ["query"],
            },
        },
    },
}


def coder_agent(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt: str,
    llm: str = "",
    workspace_dir: str = ".",
    tools_preset: str = "coding",
    custom_tools: str = "",
    max_turns: int = 5,
    max_tool_chars: int = 4000,
    max_context: int = 0,
    compaction_engine: str = "blackhole",
    output_format: str = "full",
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float | None = None,
    system_prompt: str = "",
    timeout: float = 120.0,
) -> NodeResult:
    """Microarnés de agente programador con bucle ReAct multi-turno, detección de bucles y compactación Blackhole / Luhn+."""
    inp = Inputs(inputs)
    input_text = inp.text()
    user_msg = tr('{prompt}\n\nContext / Previous input:\n{input_text}', prompt=prompt, input_text=input_text) if input_text else prompt

    base_path = Path(workspace_dir or ".").expanduser().resolve()
    if not base_path.exists():
        return NodeResult("error", error=tr("The working directory does not exist: '{workspace_dir}'", workspace_dir=workspace_dir))

    preset = (tools_preset or "coding").lower().strip()
    if preset == "all":
        active_tool_names = list(ALL_TOOL_NAMES)
    elif preset == "read_only":
        active_tool_names = list(READ_ONLY_TOOL_NAMES)
    elif preset == "custom":
        if isinstance(custom_tools, (list, tuple, set)):
            custom_list = [str(t).strip().lower() for t in custom_tools if str(t).strip()]
        else:
            custom_list = [t.strip().lower() for t in str(custom_tools or "").split(",") if t.strip()]
        active_tool_names = [t for t in custom_list if t in CODER_TOOL_DEFINITIONS]
    else:  # coding
        active_tool_names = list(CODING_TOOL_NAMES)

    tools_spec = [CODER_TOOL_DEFINITIONS[name] for name in active_tool_names if name in CODER_TOOL_DEFINITIONS]

    overrides: dict[str, Any] = {
        "url": url,
        "model": model,
        "secret": secret,
        "max_context": max_context,
    }
    if temperature is not None:
        overrides["temperature"] = temperature

    resolved = llms.resolve(name=llm, overrides=overrides)
    base_url = resolved["url"].rstrip("/")
    target_model = resolved["model"]
    api_key = resolved["api_key"]
    if not llm and temperature is None:
        model_temp = 0.1
    else:
        model_temp = resolved["temperature"]
    explicit_ctx = max_context if (max_context and int(max_context) > 0) else resolved["max_context"]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Detección del tamaño de la ventana de contexto del servidor
    context_window = _detect_context_window(
        base_url=base_url,
        target_model=target_model,
        api_key=api_key,
        explicit_ctx=explicit_ctx,
        timeout=min(15.0, timeout),
    )
    compactions_count = 0

    default_system = (
        tr('You are an autonomous programming and code inspection agent. You have access to tools to explore, read, edit and run tests in the project. Be rigorous, objective and concise. When you complete the requested task, summarize your conclusions or modifications.')
    )
    sys_content = system_prompt.strip() or default_system

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": user_msg},
    ]

    tool_handlers = {
        "read": lambda args: _tool_read(base_path, args.get("path", ""), args.get("start_line", 1), args.get("end_line")),
        "bash": lambda args: _tool_bash(base_path, args.get("command", "")),
        "edit": lambda args: _tool_edit(base_path, args.get("path", ""), args.get("old_str", ""), args.get("new_str", "")),
        "write": lambda args: _tool_write(base_path, args.get("path", ""), args.get("content", "")),
        "grep": lambda args: _tool_grep(base_path, args.get("pattern", ""), args.get("path", "."), args.get("case_insensitive", False)),
        "find": lambda args: _tool_find(base_path, args.get("pattern", "*"), args.get("path", ".")),
        "ls": lambda args: _tool_ls(base_path, args.get("path", ".")),
        "web_search": lambda args: _tool_web_search(args.get("query", ""), args.get("limit", 5), timeout=min(30.0, timeout)),
    }

    endpoint = f"{base_url}/chat/completions"
    turns = 0
    modified_files: set[str] = set()
    tool_calls_log: list[dict[str, Any]] = []
    consecutive_tool_failures: dict[str, int] = {}
    final_response = ""
    completed = False
    turn_limit = max(1, min(int(max_turns or 5), 20))

    try:
        with httpx.Client(timeout=float(timeout or 120.0)) as client:
            for _ in range(turn_limit):
                turns += 1

                # Comprobación de ventana de contexto: compactar según el motor configurado al alcanzar el 90%
                messages, did_compact = _compact_conversation_history(
                    messages,
                    context_window=context_window,
                    engine=compaction_engine,
                    client=client,
                    endpoint=endpoint,
                    headers=headers,
                    target_model=target_model,
                    temperature=float(model_temp),
                )
                if did_compact:
                    compactions_count += 1
                    eng_clean = (compaction_engine or "blackhole").lower().strip()
                    comp_tool_name = "luhn+_compact" if eng_clean in ("luhn", "luhn+", "luhn_plus", "luhnplus") else f"{eng_clean}_compact"
                    tool_calls_log.append({
                        "turn": turns,
                        "tool": comp_tool_name,
                        "args": {
                            "context_window": context_window,
                            "threshold": int(context_window * 0.90),
                            "engine": eng_clean,
                        },
                        "output_preview": tr('Context compacted to 90% with {eng_clean} ({messages} estimated tokens)', eng_clean=eng_clean, messages=_estimate_messages_tokens(messages)),
                    })

                payload: dict[str, Any] = {
                    "model": target_model,
                    "messages": messages,
                    "temperature": float(model_temp),
                }
                if tools_spec:
                    payload["tools"] = tools_spec

                resp = client.post(endpoint, headers=headers, json=payload)
                if resp.status_code >= 400:
                    return NodeResult("error", error=tr('LLM API returned HTTP {status_code}: {text}', status_code=resp.status_code, text=resp.text[:300]))

                data = resp.json()
                choice = data["choices"][0]
                msg = choice["message"]
                messages.append(msg)

                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    final_response = (msg.get("content") or "").strip()
                    completed = True
                    break

                for call in tool_calls:
                    call_id = call.get("id") or f"call_{len(tool_calls_log)}"
                    fn = call.get("function", {})
                    fn_name = fn.get("name", "")
                    raw_args = fn.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except Exception:
                            args = {}
                    else:
                        args = raw_args or {}

                    if fn_name not in active_tool_names or fn_name not in tool_handlers:
                        result_str = tr("Error: The tool '{fn_name}' is not available or is not authorized on this node.", fn_name=fn_name)
                    else:
                        try:
                            result_str = tool_handlers[fn_name](args)
                            if fn_name in ("write", "edit") and "path" in args:
                                modified_files.add(str(args["path"]))
                        except Exception as err:
                            result_str = tr("Error running '{fn_name}': {err}", fn_name=fn_name, err=err)

                    # Detección de fallos para control de bucles
                    is_fail = _is_tool_failure(fn_name, result_str)
                    if is_fail:
                        consecutive_tool_failures[fn_name] = consecutive_tool_failures.get(fn_name, 0) + 1
                    else:
                        consecutive_tool_failures[fn_name] = 0

                    clipped = _clip_tool_output(result_str, max_chars=int(max_tool_chars or 4000))
                    tool_calls_log.append({
                        "turn": turns,
                        "tool": fn_name,
                        "args": args,
                        "output_preview": clipped[:200],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": fn_name,
                        "content": clipped,
                    })

                # Detección de bucles: comprobar si alguna herramienta ha fallado consecutivamente >= 3 veces
                loop_warnings = []
                for t_name, failures in list(consecutive_tool_failures.items()):
                    if failures >= 3:
                        loop_warnings.append(tr("Tool '{t_name}' has failed {failures} consecutive times.", t_name=t_name, failures=failures))
                        consecutive_tool_failures[t_name] = 0  # Reiniciar tras advertir para evitar repeticiones en cada turno

                if loop_warnings:
                    warning_msg = (
                        tr('[System loop warning]: ')
                        + " ".join(loop_warnings)
                        + tr(' Stop repeating attempts with the same parameters. Analyze the root cause of the error, inspect the state of the files, or change strategy before continuing.')
                    )
                    messages.append({
                        "role": "user",
                        "content": warning_msg,
                    })
                    tool_calls_log.append({
                        "turn": turns,
                        "tool": "loop_detected_alert",
                        "args": {"warnings": loop_warnings},
                        "output_preview": warning_msg[:200],
                    })

            if not completed:
                # Comprobar compactación antes del resumen final
                messages, _ = _compact_conversation_history(
                    messages,
                    context_window=context_window,
                    engine=compaction_engine,
                    client=client,
                    endpoint=endpoint,
                    headers=headers,
                    target_model=target_model,
                    temperature=float(model_temp),
                )
                messages.append({
                    "role": "user",
                    "content": tr('The turn limit has been reached. Briefly summarize the actions performed and the current state.'),
                })
                try:
                    final_resp = client.post(
                        endpoint,
                        headers=headers,
                        json={
                            "model": target_model,
                            "messages": messages,
                            "temperature": float(model_temp),
                        },
                    )
                    if final_resp.status_code == 200:
                        final_data = final_resp.json()
                        final_response = (final_data["choices"][0]["message"].get("content") or "").strip()
                except Exception:
                    final_response = (messages[-2].get("content") or tr('Turn limit reached.')).strip()

    except Exception as exc:
        return NodeResult("error", error=tr('Error in programming agent loop: {exc}', exc=exc))

    res_data = {
        "response": final_response,
        "turns_used": turns,
        "completed": completed,
        "context_window": context_window,
        "compactions_count": compactions_count,
        "compaction_engine": compaction_engine,
        "modified_files": sorted(list(modified_files)),
        "tool_calls_count": len([
            c for c in tool_calls_log
            if not c["tool"].endswith("_compact") and c["tool"] != "loop_detected_alert"
        ]),
        "tool_history": tool_calls_log,
    }

    if (output_format or "").lower().strip() == "text":
        return NodeResult("ok", data=final_response)
    return NodeResult("ok", data=res_data)


def web_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    length: str = "medium",
    engine: str = "auto",
    impersonate: str = "chrome",
    user_agent: str = "",
) -> NodeResult:
    """Descarga y limpia una página web con soporte de curl_cffi y suplantación TLS."""
    res = http_call(
        ctx,
        inputs,
        url=url,
        engine=engine,
        impersonate=impersonate,
        user_agent=user_agent,
    )
    if not res.ok:
        return res
    cleaned = clean_html(ctx, [res], url=url)
    txt = str(cleaned.data or "")

    limits = {"short": 800, "medium": 2500, "long": 6000, "full": len(txt)}
    max_c = limits.get(length, 2500)
    if len(txt) > max_c:
        txt = txt[:max_c] + "..."
    return NodeResult("ok", data=txt)


def web_search(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    query: str,
    engine: str = "duckduckgo",
    limit: int = 5,
    secret: str = "",
    output_format: str = "json",
    timeout: float = DEFAULT_TIMEOUT,
) -> NodeResult:
    """Busca en internet usando DuckDuckGo (gratuito), Tavily Search API o Exa Search API."""
    inp = Inputs(inputs)
    rendered_query = (query or "").strip()
    if "{entrada}" in rendered_query:
        rendered_query = rendered_query.replace("{entrada}", inp.text()).strip()
    elif not rendered_query and inp.text():
        rendered_query = inp.text().strip()

    if not rendered_query:
        return NodeResult("error", error=tr("A search term is required in 'query'."))

    eng = (engine or "duckduckgo").lower().strip()
    max_items = max(1, int(limit or 5))
    results: list[dict[str, Any]] = []

    if eng in ("duckduckgo", "ddg"):
        q_encoded = urllib.parse.quote_plus(rendered_query)
        url = f"https://html.duckduckgo.com/html/?q={q_encoded}"
        resp_text = ""
        use_curl = _has_curl_cffi()
        if use_curl:
            try:
                from curl_cffi import requests as curl
                r = curl.get(url, impersonate="chrome", timeout=float(timeout))
                if r.status_code == 200:
                    resp_text = r.text
            except Exception:
                pass

        if not resp_text:
            try:
                headers = {"User-Agent": USER_AGENT_PRESETS["chrome"]}
                with httpx.Client(timeout=float(timeout), follow_redirects=True) as client:
                    r = client.get(url, headers=headers)
                    if r.status_code == 200:
                        resp_text = r.text
                    elif r.status_code == 202:
                        r_post = client.post("https://html.duckduckgo.com/html/", data={"q": rendered_query}, headers=headers)
                        if r_post.status_code == 200:
                            resp_text = r_post.text
            except Exception as exc:
                return NodeResult("error", error=tr('DuckDuckGo search failed: {exc}', exc=exc))

        if not resp_text:
            return NodeResult("error", error=tr('Could not get a response from DuckDuckGo.'))

        blocks = re.split(r"<div[^>]+class=[\"\'][^\"\']*web-result", resp_text)[1:]
        for b in blocks[:max_items]:
            title_m = re.search(r'<a[^>]*class=[\"\'][^\"\']*result__a[^\"\']*[\"\'][^>]*>(.*?)</a>', b, re.DOTALL)
            url_m = re.search(r'<a[^>]*class=[\"\'][^\"\']*result__a[^\"\']*[\"\'][^>]*href=[\"\']([^\"\']+)[\"\']', b)
            snippet_m = re.search(r'<a[^>]*class=[\"\'][^\"\']*result__snippet[^\"\']*[\"\'][^>]*>(.*?)</a>', b, re.DOTALL)

            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            raw_url = url_m.group(1) if url_m else ""
            actual_url = raw_url
            if "uddg=" in raw_url:
                parsed = urllib.parse.urlparse(raw_url if raw_url.startswith("http") else "https:" + raw_url)
                qs = urllib.parse.parse_qs(parsed.query)
                if "uddg" in qs:
                    actual_url = qs["uddg"][0]

            snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip() if snippet_m else ""
            snippet = html.unescape(" ".join(snippet.split()))
            title = html.unescape(" ".join(title.split()))
            if actual_url:
                results.append({
                    "title": title or actual_url,
                    "url": actual_url,
                    "snippet": snippet,
                })

    elif eng == "tavily":
        token = secrets.get(secret) if secret else ""
        if not token:
            return NodeResult("error", error=tr("The 'tavily' engine requires configuring a secret with the API Key."))

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": token,
            "query": rendered_query,
            "max_results": max_items,
            "include_answer": False,
        }
        try:
            with httpx.Client(timeout=float(timeout)) as client:
                r = client.post(url, json=payload)
                if r.status_code >= 400:
                    return NodeResult("error", error=f"Tavily API HTTP {r.status_code}: {r.text[:150]}")
                data_json = r.json()
                for item in data_json.get("results", [])[:max_items]:
                    results.append({
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("url") or ""),
                        "snippet": str(item.get("content") or ""),
                        "score": item.get("score"),
                    })
        except Exception as exc:
            return NodeResult("error", error=tr('Tavily search failed: {exc}', exc=exc))

    elif eng in ("exa", "metaphor"):
        token = secrets.get(secret) if secret else ""
        if not token:
            return NodeResult("error", error=tr("The 'exa' engine requires configuring a secret with the API Key."))

        url = "https://api.exa.ai/search"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": token,
        }
        payload = {
            "query": rendered_query,
            "numResults": max_items,
            "contents": {"text": {"maxCharacters": 350}},
        }
        try:
            with httpx.Client(timeout=float(timeout)) as client:
                r = client.post(url, json=payload, headers=headers)
                if r.status_code >= 400:
                    return NodeResult("error", error=f"Exa API HTTP {r.status_code}: {r.text[:150]}")
                data_json = r.json()
                for item in data_json.get("results", [])[:max_items]:
                    snippet = ""
                    if isinstance(item.get("text"), str):
                        snippet = item["text"]
                    elif isinstance(item.get("highlights"), list) and item["highlights"]:
                        snippet = " ".join(item["highlights"])
                    results.append({
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("url") or ""),
                        "snippet": snippet,
                        "score": item.get("score"),
                        "published_date": item.get("publishedDate"),
                    })
        except Exception as exc:
            return NodeResult("error", error=tr('Exa search failed: {exc}', exc=exc))

    else:
        return NodeResult("error", error=tr("Unknown search engine: '{engine}'. Use 'duckduckgo', 'tavily' or 'exa'.", engine=engine))

    fmt = (output_format or "json").lower().strip()
    if fmt in ("markdown", "text", "texto"):
        lines = []
        for r in results:
            t = r.get("title") or r.get("url")
            u = r.get("url")
            s = r.get("snippet")
            if s:
                lines.append(f"- [{t}]({u}): {s}")
            else:
                lines.append(f"- [{t}]({u})")
        return NodeResult("ok", data="\n".join(lines))

    return NodeResult("ok", data=results)


def summarize_text(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    engine: str = "luhn+",
    length: str = "medium",
    prompt: str = "",
    field: str = "",
    sentences: int = 0,
    ratio: float = 0.0,
    format: str = "paragraph",
    llm: str = "",
) -> NodeResult:
    """Resume un texto usando Luhn, Luhn+, TextRank o LLM."""
    inp = Inputs(inputs)
    first = inp.first_data()
    text = ""
    if isinstance(first, (dict, list)):
        if field:
            val = _extract_field_value(first, field)
            if val is None:
                val = _find_field_recursive(first, field)
            text = str(val or "")
        else:
            for candidate in ("extract", "content", "text", "body", "description"):
                val = _find_field_recursive(first, candidate)
                if val:
                    text = str(val)
                    break
            if not text:
                text = inp.text()
    else:
        text = inp.text()

    if not text:
        return NodeResult("ok", data="")

    eng = (engine or "luhn+").lower().strip()

    if eng in ("luhn", "luhn+", "luhn_plus", "luhnplus", "textrank", "text_rank"):
        # Limpiar encabezados de secciones comunes (ej. == Encabezado == en Wikipedia o markdown ###)
        cleaned_text = re.sub(r"(?m)^\s*=+[^\n=]+=+\s*$", "", text)
        cleaned_text = re.sub(r"(?m)^\s*#{1,6}\s+.*$", "", cleaned_text)

        # Proteger abreviaturas y títulos comunes para evitar divisiones erróneas
        protected = re.sub(r"\b(Dr|Dra|Sr|Sra|Srta|Prof|D|Dña)\.", r"\1§DOT§", cleaned_text, flags=re.IGNORECASE)
        protected = re.sub(r"\b(p)\.\s*(ej)\.", r"\1§DOT§ \2§DOT§", protected, flags=re.IGNORECASE)
        protected = re.sub(r"\b(art|núm|apdo|cap|vol|vs|fig)\.", r"\1§DOT§", protected, flags=re.IGNORECASE)
        protected = re.sub(r"\b([aAdD])\.\s*([cC])\.", r"\1§DOT§ \2.", protected)
        protected = re.sub(r"\b([eE][eE])\.\s*([uU][uU])\.", r"\1§DOT§ \2§DOT§", protected)
        protected = re.sub(r"\b([cC][aA]?)\.", r"\1§DOT§", protected)

        # Dividir por fin de frase o bloques de párrafo
        raw_parts = [p.strip() for p in re.split(r"\n+", protected) if p.strip()]
        sentence_list: list[str] = []
        for part in raw_parts:
            for s in re.split(r"(?<=[.?!])\s+", part):
                s = s.replace("§DOT§", ".").strip()
                if len(s) >= 15 and len(s.split()) >= 4:
                    sentence_list.append(s)

        # Fallback a división simple si el texto es breve o no pasa los filtros estrictos
        if not sentence_list:
            sentence_list = [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]

        if not sentence_list:
            return NodeResult("ok", data=text)

        # Determinar número objetivo de frases a extraer
        if int(sentences or 0) > 0:
            target_count = int(sentences)
        elif float(ratio or 0.0) > 0.0:
            target_count = max(1, int(len(sentence_list) * float(ratio)))
        elif str(length).isdigit():
            target_count = int(length)
        else:
            preset_counts = {
                "short": 3,
                "medium": 6,
                "long": 12,
                "full": len(sentence_list),
            }
            target_count = preset_counts.get(str(length).lower(), 6)

        target_count = min(target_count, len(sentence_list))

        if eng == "luhn":
            # Algoritmo Luhn clásico basado en frecuencia simple de palabras de >= 4 caracteres
            words = re.findall(r"\w{4,}", text.lower())
            freq: dict[str, int] = {}
            for w in words:
                freq[w] = freq.get(w, 0) + 1

            scores = []
            for s in sentence_list:
                score = sum(freq.get(w, 0) for w in re.findall(r"\w{4,}", s.lower()))
                scores.append((score, s))

            top = sorted(
                sorted(scores, key=lambda x: x[0], reverse=True)[:target_count],
                key=lambda x: sentence_list.index(x[1]),
            )
            return NodeResult("ok", data=_format_summary_output([s for _, s in top], format))

        elif eng in ("luhn+", "luhn_plus", "luhnplus"):
            # Luhn+ avanzado: filtro de stopwords, normalización por longitud, sesgo posicional y diversidad (MMR)
            chosen_summary = _run_luhn_plus(text, target_count=target_count, output_format=format, sentences=sentence_list)
            return NodeResult("ok", data=chosen_summary)

        elif eng in ("textrank", "text_rank"):
            # Algoritmo TextRank: grafo de similitud léxica y centralidad PageRank
            n = len(sentence_list)
            if n <= target_count:
                return NodeResult("ok", data=_format_summary_output(sentence_list, format))

            word_sets = [
                set(w for w in re.findall(r"\b\w{3,}\b", s.lower()) if w not in _SUMMARIZE_STOPWORDS and not w.isdigit())
                for s in sentence_list
            ]

            sim_matrix = [[0.0] * n for _ in range(n)]
            for i in range(n):
                for j in range(i + 1, n):
                    w1, w2 = word_sets[i], word_sets[j]
                    if w1 and w2:
                        inter = len(w1 & w2)
                        sim = inter / (math.log(len(w1) + 1) + math.log(len(w2) + 1))
                        sim_matrix[i][j] = sim
                        sim_matrix[j][i] = sim

            degrees = [sum(sim_matrix[i]) for i in range(n)]
            d = 0.85
            pr_scores = [1.0] * n
            for _ in range(10):
                new_scores = [1.0 - d] * n
                for i in range(n):
                    for j in range(n):
                        if i != j and degrees[j] > 0:
                            new_scores[i] += d * (sim_matrix[j][i] / degrees[j]) * pr_scores[j]
                pr_scores = new_scores

            # Sesgo de primera frase en TextRank
            pr_scores[0] *= 1.25

            ranked_indices = sorted(range(n), key=lambda i: pr_scores[i], reverse=True)[:target_count]
            ranked_indices.sort()
            chosen = [sentence_list[i] for i in ranked_indices]
            return NodeResult("ok", data=_format_summary_output(chosen, format))

    # Motor LLM
    is_bullet = (format or "").lower().strip() in ("bullets", "bullet", "viñetas", "vineta", "list", "lista")
    default_sys = (
        tr('Summarize the following text, keeping the key points in a bulleted list.')
        if is_bullet
        else tr('Summarize the following text, keeping the key points.')
    )
    sys_prompt = prompt or default_sys
    limits = {"short": 512, "medium": 1024, "long": 2048, "full": 4096}
    if int(sentences or 0) > 0:
        tokens = min(4096, max(256, int(sentences) * 50))
    else:
        tokens = limits.get(str(length).lower(), 1024)
    try:
        summary = call_llm(text, system=sys_prompt, tokens=tokens, llm=llm)
        return NodeResult("ok", data=summary)
    except Exception as exc:
        return NodeResult("error", error=tr('LLM summarization failed: {exc}', exc=exc))


def merge(ctx: Any, inputs: list[NodeResult]) -> NodeResult:
    """Combina los datos de múltiples entradas exitosas en una lista."""
    inp = Inputs(inputs)
    return NodeResult("ok", data=inp.all_ok())


def script(
    ctx: Any,
    inputs: list[NodeResult],
    fn: Callable[[Inputs, Any], Any],
    sources: list[str] | None = None,
) -> NodeResult:
    """Ejecuta una función de script personalizada de Python."""
    inp = Inputs(inputs, sources=sources)
    try:
        res = fn(inp, ctx)
        return NodeResult("ok", data=res)
    except Exception as exc:
        return NodeResult("error", error=tr("Error in the node's Python code: {exc}", exc=exc))


# ---------------------------------------------------------------------------
# Nodos de Salida
# ---------------------------------------------------------------------------


def file(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    filename: str = "salida.md",
    directory: str = "",
    content: str = "inputs",
    message: str = "",
    mode: str = "sobrescribir",
    format: str = "raw",
) -> NodeResult:
    """Escribe la salida en un fichero del disco."""
    inp = Inputs(inputs)
    first = inp.first_data()
    if content == "static":
        payload = message
    elif format == "json_pretty" and isinstance(first, (dict, list)):
        payload = json.dumps(first, ensure_ascii=False, indent=2)
    else:
        payload = inp.text()

    today = time.strftime("%Y-%m-%d")
    hour = time.strftime("%H-%M-%S")
    ts = str(int(time.time()))
    final_name = (
        filename.replace("{fecha}", today)
        .replace("{hora}", hour)
        .replace("{timestamp}", ts)
    )

    base_dir = Path("data/outputs") / (directory.strip().lstrip("/"))
    base_dir.mkdir(parents=True, exist_ok=True)
    target = base_dir / final_name

    if mode == "numerar" and target.exists():
        stem, ext = target.stem, target.suffix
        i = 2
        while target.exists():
            target = base_dir / f"{stem}-{i}{ext}"
            i += 1

    try:
        if mode == "añadir":
            with target.open("a", encoding="utf-8") as f:
                f.write("\n" + payload)
        else:
            target.write_text(payload, encoding="utf-8")
        return NodeResult("ok", data=str(target))
    except Exception as exc:
        return NodeResult("error", error=tr('Error writing file: {exc}', exc=exc))


def evidence_vault(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    directory: str = "data/vault",
    compress: bool = True,
    filename_pattern: str = "evidence_{incident_id}_{hash}",
    include_metadata: bool = True,
) -> NodeResult:
    """Archiva evidencia inmutable calculando SHA-256 en disco local con compresión gzip opcional."""
    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    if first is None and not raw_text:
        return NodeResult("error", error=tr('The Evidence Vault node did not receive any data'))

    incident_id = "inc"
    resource = ""
    target_payload: Any = first if first is not None else raw_text

    if isinstance(first, dict):
        if first.get("incident_id"):
            incident_id = str(first["incident_id"]).strip()
        elif first.get("id"):
            incident_id = str(first["id"]).strip()

        if first.get("resource"):
            resource = str(first["resource"]).strip()
        elif first.get("target"):
            resource = str(first["target"]).strip()

    if isinstance(target_payload, (dict, list)):
        payload_bytes = json.dumps(target_payload, ensure_ascii=False, indent=2).encode("utf-8")
    elif isinstance(target_payload, bytes):
        payload_bytes = target_payload
    else:
        payload_bytes = str(target_payload).encode("utf-8")

    sha256_full = hashlib.sha256(payload_bytes).hexdigest()
    sha256_short = sha256_full[:12]
    now_ts = int(time.time())
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    fname = (
        filename_pattern.replace("{incident_id}", incident_id)
        .replace("{hash}", sha256_short)
        .replace("{timestamp}", str(now_ts))
    )

    base_dir = Path(directory.strip() or "data/vault")
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        if compress:
            if not fname.endswith(".json.gz") and not fname.endswith(".gz"):
                fname += ".json.gz"
            final_path = base_dir / fname
            compressed_bytes = gzip.compress(payload_bytes)
            final_path.write_bytes(compressed_bytes)
            size_bytes = len(compressed_bytes)
        else:
            if not fname.endswith(".json") and not fname.endswith(".txt") and not fname.endswith(".log"):
                fname += ".json"
            final_path = base_dir / fname
            final_path.write_bytes(payload_bytes)
            size_bytes = len(payload_bytes)

        # Si se solicita metadatos, guardar un manifiesto .meta.json al lado
        if include_metadata:
            meta_path = final_path.parent / f"{final_path.name}.meta.json"
            meta_content = {
                "incident_id": incident_id,
                "resource": resource,
                "created_at": now_iso,
                "original_size_bytes": len(payload_bytes),
                "stored_size_bytes": size_bytes,
                "compressed": compress,
                "sha256": sha256_full,
                "file": str(final_path),
            }
            meta_path.write_text(json.dumps(meta_content, indent=2, ensure_ascii=False), encoding="utf-8")

        result_data = {
            "incident_id": incident_id,
            "resource": resource,
            "vault_path": str(final_path),
            "sha256": sha256_full,
            "size_bytes": size_bytes,
            "compressed": compress,
            "timestamp": now_iso,
            "telemetry": target_payload,
        }
        return NodeResult("ok", data=result_data)
    except Exception as exc:
        return NodeResult("error", error=tr('Error writing to Evidence Vault: {exc}', exc=exc))


def _text_to_atlassian_doc(text: str) -> dict[str, Any]:
    """Convierte texto plano o markdown simple en formato Atlassian Document Format (ADF)."""
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text or ""]

    content = []
    for p in paragraphs:
        lines = p.splitlines()
        p_content = []
        for idx, line in enumerate(lines):
            if idx > 0:
                p_content.append({"type": "hardBreak"})
            p_content.append({"type": "text", "text": line})
        content.append({
            "type": "paragraph",
            "content": p_content,
        })

    return {
        "version": 1,
        "type": "doc",
        "content": content,
    }


def jira_issue_output(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    instance_url: str,
    action: str = "create",
    issue_key: str = "",
    project_key: str = "",
    email: str = "",
    secret: str = "",
    issue_type: str = "Task",
    summary: str = "",
    description: str = "",
    comment: str = "",
    priority: str = "",
    status_transition: str = "",
    labels: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> NodeResult:
    """Crea, actualiza, comenta o transiciona tickets en Jira Cloud usando la REST API v3."""
    base_url = instance_url.strip().rstrip("/")
    if not base_url:
        return NodeResult("error", error=tr('Missing Jira instance URL (e.g. https://my-domain.atlassian.net)'))

    headers = _build_atlassian_auth_header(email, secret)
    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    act = (action or "create").strip().lower()

    # Si la entrada es una lista de tickets enriquecida por el agente (ej. per_item)
    # o un diccionario singular, extraemos los items a procesar
    items = []
    if isinstance(first, list):
        items = first
    elif isinstance(first, dict) and isinstance(first.get("issues"), list):
        items = first["issues"]
    elif isinstance(first, dict):
        items = [first]
    else:
        items = [{"raw": raw_text}]

    def _execute_single(item_data: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
        # Determinar clave de ticket (issue_key o del item)
        target_key = (
            issue_key.strip()
            or str(item_data.get("key") or item_data.get("issue_key") or item_data.get("id") or "").strip()
        )

        # Determinar texto de comentario o contenido
        comment_text = comment.strip()
        if not comment_text:
            comment_text = str(
                item_data.get("comment")
                or item_data.get("analysis")
                or item_data.get("plan")
                or item_data.get("analisis_triaje")
                or (item_data.get("plan") if isinstance(item_data.get("plan"), str) else "")
                or (item_data.get("analysis") if isinstance(item_data.get("analysis"), str) else "")
                or ""
            )
        if not comment_text and act == "add_comment":
            comment_text = raw_text

        # 1. ACCIÓN: AÑADIR COMENTARIO
        if act == "add_comment":
            if not target_key:
                raise ValueError(tr('issue_key is required to add a comment to the ticket'))
            if not comment_text:
                raise ValueError(tr('No comment content for ticket {target_key}', target_key=target_key))

            comment_endpoint = f"{base_url}/rest/api/3/issue/{target_key}/comment"
            comment_payload = {"body": _text_to_atlassian_doc(comment_text)}
            resp = client.post(comment_endpoint, headers=headers, json=comment_payload)
            if resp.status_code >= 400:
                raise RuntimeError(f"Jira comment error ({resp.status_code}): {resp.text[:200]}")
            data = resp.json()
            return {
                "key": target_key,
                "action": "comment_added",
                "comment_id": data.get("id"),
                "url": f"{base_url}/browse/{target_key}",
            }

        # 2. ACCIÓN: TRANSICIÓN DE ESTADO
        elif act in ("transition", "change_status"):
            if not target_key:
                raise ValueError(tr('issue_key is required to transition the ticket status'))
            target_status = (status_transition.strip() or str(item_data.get("status_transition") or "")).strip()
            if not target_status:
                raise ValueError(tr("status_transition is missing (e.g. 'In Progress', 'Done', 'Triaged')"))

            # Obtener transiciones disponibles para el issue
            trans_endpoint = f"{base_url}/rest/api/3/issue/{target_key}/transitions"
            t_resp = client.get(trans_endpoint, headers=headers)
            if t_resp.status_code >= 400:
                raise RuntimeError(tr('Error fetching transitions for {target_key}: {text}', target_key=target_key, text=t_resp.text[:200]))
            transitions = t_resp.json().get("transitions", [])
            match_trans = next(
                (t for t in transitions if t.get("name", "").lower() == target_status.lower()),
                None
            )
            if not match_trans:
                available = ", ".join(t.get("name", "") for t in transitions)
                raise ValueError(tr("Transition '{target_status}' not available for {target_key}. Available: {ninguna}", target_status=target_status, target_key=target_key, ninguna=available or tr('none')))

            trans_payload = {"transition": {"id": match_trans["id"]}}
            if comment_text:
                trans_payload["update"] = {"comment": [{"add": {"body": _text_to_atlassian_doc(comment_text)}}]}

            post_t = client.post(trans_endpoint, headers=headers, json=trans_payload)
            if post_t.status_code >= 400:
                raise RuntimeError(tr('Error applying transition to {target_key}: {text}', target_key=target_key, text=post_t.text[:200]))

            return {
                "key": target_key,
                "action": "transitioned",
                "status": match_trans.get("name"),
                "url": f"{base_url}/browse/{target_key}",
            }

        # 3. ACCIÓN: ACTUALIZAR CAMPOS (Prioridad, etiquetas, resumen)
        elif act == "update":
            if not target_key:
                raise ValueError(tr('issue_key is required to update the ticket'))
            update_endpoint = f"{base_url}/rest/api/3/issue/{target_key}"
            fields_to_update: dict[str, Any] = {}

            prio = (priority.strip() or str(item_data.get("priority") or "")).strip()
            if prio:
                fields_to_update["priority"] = {"name": prio}

            lbls = (labels.strip() or str(item_data.get("labels") or "")).strip()
            if lbls:
                fields_to_update["labels"] = [lbl.strip().replace(" ", "-") for lbl in lbls.split(",") if lbl.strip()]

            if summary.strip():
                fields_to_update["summary"] = summary.strip()[:250]
            if description.strip():
                fields_to_update["description"] = _text_to_atlassian_doc(description.strip())

            if not fields_to_update and not comment_text:
                raise ValueError(tr('No fields or comments were specified to update on the ticket'))

            update_payload: dict[str, Any] = {}
            if fields_to_update:
                update_payload["fields"] = fields_to_update
            if comment_text:
                update_payload["update"] = {"comment": [{"add": {"body": _text_to_atlassian_doc(comment_text)}}]}

            put_resp = client.put(update_endpoint, headers=headers, json=update_payload)
            if put_resp.status_code >= 400:
                raise RuntimeError(f"Error actualizando issue {target_key}: {put_resp.text[:200]}")

            return {
                "key": target_key,
                "action": "updated",
                "updated_fields": list(fields_to_update.keys()),
                "url": f"{base_url}/browse/{target_key}",
            }

        # 4. ACCIÓN: CREAR NUEVO TICKET (Por defecto)
        else:
            proj = (project_key.strip() or str(item_data.get("project_key") or "")).strip().upper()
            if not proj:
                raise ValueError(tr('The project key (project_key, e.g. SEC, OPS) is missing to create the ticket'))

            final_sum = summary.strip()
            if not final_sum:
                final_sum = str(
                    item_data.get("summary")
                    or item_data.get("title")
                    or (item_data.get("meta") or {}).get("incident_id")
                    or tr('Incident detected by Dédalo')
                )

            final_d = description.strip()
            if not final_d:
                if item_data.get("plan"):
                    final_d = str(item_data["plan"])
                elif item_data.get("analysis"):
                    final_d = str(item_data["analysis"])
                else:
                    final_d = raw_text or final_sum

            create_endpoint = f"{base_url}/rest/api/3/issue"
            fields_payload: dict[str, Any] = {
                "project": {"key": proj},
                "summary": final_sum[:250],
                "issuetype": {"name": issue_type.strip() or "Task"},
                "description": _text_to_atlassian_doc(final_d),
            }

            prio = (priority.strip() or str(item_data.get("priority") or "")).strip()
            if prio:
                fields_payload["priority"] = {"name": prio}

            lbls = (labels.strip() or str(item_data.get("labels") or "")).strip()
            if lbls:
                lbl_list = [lbl.strip().replace(" ", "-") for lbl in lbls.split(",") if lbl.strip()]
                if lbl_list:
                    fields_payload["labels"] = lbl_list

            resp = client.post(create_endpoint, headers=headers, json={"fields": fields_payload})
            if resp.status_code >= 400:
                raise RuntimeError(tr('Error creating Jira ticket ({status_code}): {text}', status_code=resp.status_code, text=resp.text[:300]))
            data = resp.json()
            k = data.get("key")
            return {
                "key": k,
                "id": data.get("id"),
                "url": f"{base_url}/browse/{k}",
                "summary": final_sum,
                "project": proj,
                "action": "created",
            }

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            executed = []
            for item in items[:MAX_ITEMS]:
                if not isinstance(item, dict):
                    item = {"raw": str(item)}
                res = _execute_single(item, client)
                executed.append(res)

            if len(executed) == 1 and not isinstance(first, list):
                return NodeResult("ok", data=executed[0])
            return NodeResult("ok", data={"processed": len(executed), "results": executed})
    except Exception as exc:
        return NodeResult("error", error=tr('Error in Jira operation ({act}): {exc}', act=act, exc=exc))


def confluence_page_output(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    instance_url: str,
    page_id: str = "",
    space_key: str = "",
    email: str = "",
    secret: str = "",
    title: str = "",
    content: str = "",
    mode: str = "create_or_update",
    timeout: float = DEFAULT_TIMEOUT,
) -> NodeResult:
    """Crea o actualiza una página en Confluence Cloud usando XHTML/Storage format."""
    base_url = instance_url.strip().rstrip("/")
    if not base_url:
        return NodeResult("error", error=tr('Missing Confluence instance URL (e.g. https://my-domain.atlassian.net/wiki)'))

    wiki_base = base_url if base_url.endswith("/wiki") else f"{base_url}/wiki"
    headers = _build_atlassian_auth_header(email, secret)

    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    target_page_id = page_id.strip()
    target_space = space_key.strip()
    final_title = title.strip()
    final_content = content.strip()

    # Si viene de input.confluence_pages o de un nodo que extrajo la página:
    if isinstance(first, dict):
        if not target_page_id and first.get("id"):
            target_page_id = str(first["id"]).strip()
        if not target_space and first.get("space"):
            target_space = str(first["space"]).strip()
        if not final_title and first.get("title"):
            final_title = str(first["title"]).strip()

        if not final_content:
            if first.get("content"):
                final_content = str(first["content"])
            elif first.get("plan"):
                final_content = str(first["plan"])
            elif first.get("analysis"):
                final_content = str(first["analysis"])
            elif first.get("text"):
                final_content = str(first["text"])
            else:
                final_content = json.dumps(first, ensure_ascii=False, indent=2)
    elif not final_title:
        final_title = tr('Dédalo Document - {M}', M=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
        if not final_content:
            final_content = raw_text or tr('Automatically generated content.')

    if not final_content:
        final_content = tr('Empty document.')

    if not target_page_id and not target_space:
        return NodeResult("error", error=tr('Missing Confluence space key (space_key) or page ID (page_id)'))

    # Formatear el contenido nuevo en HTML/XHTML para Confluence Storage Format
    html_paragraphs = "".join(f"<p>{html.escape(p)}</p>" for p in final_content.split("\n\n") if p.strip())
    if not html_paragraphs:
        html_paragraphs = f"<p>{html.escape(final_content)}</p>"

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            existing_page = None

            # 1. Búsqueda por page_id directo
            if target_page_id:
                get_url = f"{wiki_base}/rest/api/content/{target_page_id}?expand=version,body.storage,space"
                g_resp = client.get(get_url, headers=headers)
                if g_resp.status_code == 200:
                    resp_json = g_resp.json()
                    if isinstance(resp_json, dict) and "id" in resp_json:
                        existing_page = resp_json
                        if not final_title:
                            final_title = existing_page.get("title", final_title)
                        if not target_space:
                            target_space = (existing_page.get("space") or {}).get("key", "")

            # 2. Búsqueda por space y title si mode == create_or_update
            if not existing_page and mode in ("create_or_update", "append") and target_space and final_title:
                search_url = f"{wiki_base}/rest/api/content"
                s_resp = client.get(
                    search_url,
                    headers=headers,
                    params={"spaceKey": target_space, "title": final_title, "expand": "version,body.storage"},
                )
                if s_resp.status_code == 200:
                    results = s_resp.json().get("results", [])
                    if results:
                        existing_page = results[0]

            if existing_page:
                # Actualizar página existente incrementando versión
                pid = existing_page["id"]
                current_ver = existing_page.get("version", {}).get("number", 1)
                update_url = f"{wiki_base}/rest/api/content/{pid}"

                storage_value = html_paragraphs
                if mode == "append":
                    current_body = ((existing_page.get("body") or {}).get("storage") or {}).get("value", "")
                    storage_value = f"{current_body}\n{html_paragraphs}"

                put_payload = {
                    "version": {"number": current_ver + 1},
                    "title": final_title,
                    "type": "page",
                    "body": {
                        "storage": {
                            "value": storage_value,
                            "representation": "storage",
                        }
                    },
                }
                resp = client.put(update_url, headers=headers, json=put_payload)
            else:
                # Crear página nueva
                create_url = f"{wiki_base}/rest/api/content"
                post_payload = {
                    "type": "page",
                    "title": final_title,
                    "space": {"key": target_space},
                    "body": {
                        "storage": {
                            "value": html_paragraphs,
                            "representation": "storage",
                        }
                    },
                }
                resp = client.post(create_url, headers=headers, json=post_payload)

            if resp.status_code >= 400:
                return NodeResult("error", error=f"Error en Confluence ({resp.status_code}): {resp.text[:300]}")

            data = resp.json()
            webui = (data.get("_links") or {}).get("webui") or ""
            page_url = f"{wiki_base}{webui}"
            return NodeResult("ok", data={
                "id": data.get("id"),
                "title": final_title,
                "space": target_space,
                "url": page_url,
                "version": (data.get("version") or {}).get("number", 1),
                "action": "updated" if existing_page else "created",
            })
    except Exception as exc:
        return NodeResult("error", error=tr('Connection error with Confluence: {exc}', exc=exc))


def sqlite_exec(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str = "data/store.db",
    query: str = "",
) -> NodeResult:
    """Ejecuta sentencias INSERT, UPDATE o DELETE en SQLite local."""
    if not query.strip():
        return NodeResult("error", error=tr('Missing SQL statement'))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    inp = Inputs(inputs)
    first = inp.first_data()
    try:
        with sqlite3.connect(p) as conn:
            cur = conn.cursor()
            if isinstance(first, dict):
                cur.execute(query, first)
            elif isinstance(first, (list, tuple)) and first and isinstance(first[0], (dict, list, tuple)):
                cur.executemany(query, first)
            elif isinstance(first, (list, tuple)):
                cur.execute(query, first)
            else:
                cur.execute(query)
            conn.commit()
            return NodeResult("ok", data={"rows_affected": cur.rowcount})
    except Exception as exc:
        return NodeResult("error", error=tr('Error in SQLite execution: {exc}', exc=exc))


def email_smtp(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    host: str = "smtp.gmail.com",
    port: int = 587,
    user: str = "",
    secret: str = "",
    from_addr: str = "",
    to_addr: str = "",
    subject: str = 'Dédalo notice',
    use_tls: bool = True,
    is_html: bool = False,
) -> NodeResult:
    """Envía un correo electrónico mediante SMTP."""
    inp = Inputs(inputs)
    body = inp.text()
    if not to_addr.strip():
        return NodeResult("error", error=tr('Missing recipient address (to_addr)'))
    password = secrets.get(secret) if secret else ""
    sender = from_addr.strip() or user.strip()

    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr.strip()
    msg["Subject"] = subject
    subtype = "html" if is_html else "plain"
    msg.attach(MIMEText(body, subtype, "utf-8"))

    try:
        import smtplib

        p_num = int(port) if str(port).isdigit() else 587
        if p_num == 465:
            server = smtplib.SMTP_SSL(host, p_num, timeout=15.0)
        else:
            server = smtplib.SMTP(host, p_num, timeout=15.0)
            if use_tls:
                server.starttls()
        if user and password:
            server.login(user, password)
        server.send_message(msg)
        server.quit()
        return NodeResult("ok", data=tr('Email sent to {to_addr}', to_addr=to_addr))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending SMTP email: {exc}', exc=exc))


def ntfy(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    topic: str,
    server: str = "https://ntfy.sh",
    title: str = "Dédalo",
    priority: str = "3",
    tags: str = "robot",
    secret: str = "",
) -> NodeResult:
    """Envía una notificación push mediante ntfy.sh o servidor propio."""
    if not topic.strip():
        return NodeResult("error", error=tr('Missing ntfy topic'))
    inp = Inputs(inputs)
    body = inp.text()
    base = server.rstrip("/")
    url = f"{base}/{topic.strip()}"
    headers = {
        "Title": title,
        "Priority": str(priority),
    }
    if tags.strip():
        headers["Tags"] = tags.strip()
    if secret:
        token = secrets.get(secret)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, data=body.encode("utf-8"), headers=headers)
            if resp.status_code >= 400:
                return NodeResult("error", error=tr('ntfy failed with HTTP {status_code}: {text}', status_code=resp.status_code, text=resp.text[:100]))
            return NodeResult("ok", data=tr('ntfy notification sent to {topic}', topic=topic))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending ntfy: {exc}', exc=exc))


def discord(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    webhook_url: str = "",
    secret: str = "",
    username: str = "Dédalo",
    title: str = "",
    content: str = "",
    use_embed: bool = False,
) -> NodeResult:
    """Envía un mensaje a un webhook de Discord."""
    url = webhook_url
    if secret:
        token = secrets.get(secret)
        if token:
            url = token
    if not url.strip():
        return NodeResult("error", error=tr('Missing Discord webhook URL'))
    inp = Inputs(inputs)
    msg_content = content.replace("{entrada}", inp.text()) if content else inp.text()
    payload: dict[str, Any] = {"username": username or "Dédalo"}
    if use_embed:
        payload["embeds"] = [{
            "title": title or tr('Dédalo notice'),
            "description": msg_content[:4000],
            "color": 3447003,
        }]
    else:
        payload["content"] = (f"**{title}**\n" if title else "") + msg_content[:2000]
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=tr('Discord webhook failed: {status_code}', status_code=resp.status_code))
            return NodeResult("ok", data=tr('Message sent to Discord successfully'))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending to Discord: {exc}', exc=exc))


def slack(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    webhook_url: str = "",
    secret: str = "",
    channel: str = "",
    text: str = "",
) -> NodeResult:
    """Envía un mensaje a un Incoming Webhook de Slack."""
    url = webhook_url
    if secret:
        token = secrets.get(secret)
        if token:
            url = token
    if not url.strip():
        return NodeResult("error", error=tr('Missing Slack webhook URL'))
    inp = Inputs(inputs)
    msg_text = text.replace("{entrada}", inp.text()) if text else inp.text()
    payload: dict[str, Any] = {"text": msg_text}
    if channel.strip():
        payload["channel"] = channel.strip()
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=tr('Slack webhook failed: {status_code}', status_code=resp.status_code))
            return NodeResult("ok", data=tr('Message sent to Slack successfully'))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending to Slack: {exc}', exc=exc))


def command_action(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    cmd: str,
    timeout: float = 15.0,
    cwd: str = "",
    sandbox: str = "",
) -> NodeResult:
    """Ejecuta un comando de sistema como acción de salida."""
    return command_input(ctx, inputs, cmd=cmd, timeout=timeout, cwd=cwd, sandbox=sandbox)


def rest_webhook(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    method: str = "POST",
    body: str = "",
    headers: str = "",
    secret: str = "",
    auth_header: str = "Authorization",
) -> NodeResult:
    """Envía un webhook HTTP hacia un servicio externo."""
    inp = Inputs(inputs)
    content_text = inp.text()

    req_headers = {"Content-Type": "application/json"}
    if headers:
        try:
            req_headers.update(json.loads(headers))
        except Exception:
            pass

    if secret:
        token = secrets.get(secret)
        if token:
            req_headers[auth_header] = token

    payload_data = None
    if body:
        rendered = body.replace("{entrada}", content_text)
        try:
            payload_data = json.loads(rendered)
        except Exception:
            payload_data = {"text": rendered}
    else:
        payload_data = {"text": content_text}

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.request(method.upper(), url, headers=req_headers, json=payload_data)
            if resp.status_code >= 400:
                return NodeResult("error", error=tr('Webhook failed with HTTP {status_code}', status_code=resp.status_code))
            return NodeResult("ok", data=tr('Webhook sent (HTTP {status_code})', status_code=resp.status_code))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending webhook: {exc}', exc=exc))


def telegram(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    chat_id: str = "",
    token_secret: str = "telegram-token",
    parse_mode: str = "HTML",
    silent: bool = False,
) -> NodeResult:
    """Envía un mensaje a Telegram mediante el bot API."""
    inp = Inputs(inputs)
    text = inp.text()
    if not text:
        return NodeResult("ok", data=tr('No content to send'))

    token = secrets.get(token_secret) or os.environ.get("DEDALO_TELEGRAM_TOKEN")
    dest_chat = chat_id or os.environ.get("DEDALO_TELEGRAM_CHAT_ID")

    if not token or not dest_chat:
        return NodeResult("error", error=tr('Telegram bot token or chat_id not configured'))

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": dest_chat,
        "text": text[:4000],
        "disable_notification": bool(silent),
    }
    if parse_mode and parse_mode != "texto":
        payload["parse_mode"] = parse_mode

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=tr('Telegram API failed: {text}', text=resp.text[:150]))
            return NodeResult("ok", data=tr('Telegram message sent successfully'))
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending Telegram: {exc}', exc=exc))


def alert(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    text: str = "",
    urgency: str = "info",
) -> NodeResult:
    """Registra una alerta en log y consola."""
    inp = Inputs(inputs)
    msg = text or inp.text()
    log_line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{urgency.upper()}] {msg}"
    print(log_line)

    alerts_dir = Path("data/logs")
    alerts_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (alerts_dir / "alerts.log").open("a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception:
        pass

    return NodeResult("ok", data=log_line)


def diff_data(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    key_field: str = "",
    mode: str = "added",
    state_key: str = "",
) -> NodeResult:
    """Compara los datos actuales con la ejecución anterior y emite novedades o diferencias."""
    inp = Inputs(inputs)
    raw_data = inp.first()
    if raw_data is None:
        raw_data = inp.text()

    if isinstance(raw_data, list):
        items = raw_data
    elif isinstance(raw_data, dict):
        items = [raw_data]
    elif isinstance(raw_data, str) and raw_data.strip():
        items = [line.strip() for line in raw_data.splitlines() if line.strip()]
    else:
        items = []

    flow_name = getattr(ctx, "flow_name", "default")
    s_key = (state_key or "diff").replace("/", "_").replace("\\", "_")
    state_file = Path("data/state") / f"{flow_name}_{s_key}.json"

    prev_snapshot: dict[str, Any] = {}
    if state_file.is_file():
        try:
            prev_snapshot = json.loads(state_file.read_text(encoding="utf-8"))
            if not isinstance(prev_snapshot, dict):
                prev_snapshot = {}
        except Exception:
            prev_snapshot = {}

    def _item_key(it: Any) -> str:
        if isinstance(it, dict):
            if key_field and key_field in it:
                return str(it[key_field])
            for cand in ("url", "link", "guid", "id", "title", "nombre"):
                if cand in it:
                    return str(it[cand])
            return hashlib.sha256(json.dumps(it, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return str(it)

    curr_snapshot: dict[str, Any] = {}
    curr_items_order: list[tuple[str, Any]] = []
    for it in items:
        k = _item_key(it)
        curr_snapshot[k] = it
        curr_items_order.append((k, it))

    added = [it for k, it in curr_items_order if k not in prev_snapshot]
    removed = [it for k, it in prev_snapshot.items() if k not in curr_snapshot]
    modified = [it for k, it in curr_items_order if k in prev_snapshot and it != prev_snapshot[k]]

    # Guardar foto actual
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(curr_snapshot, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass

    m = (mode or "added").lower().strip()
    if m == "added":
        return NodeResult("ok", data=added)
    elif m == "removed":
        return NodeResult("ok", data=removed)
    elif m == "modified":
        return NodeResult("ok", data=modified)
    elif m in ("all", "todo"):
        return NodeResult("ok", data={
            "added": added,
            "removed": removed,
            "modified": modified,
            "count_added": len(added),
            "count_removed": len(removed),
            "count_modified": len(modified),
        })
    return NodeResult("ok", data=added)


def fuzzy_dedup(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    field: str = "",
    threshold: float = 0.80,
    merge_strategy: str = "keep_first",
) -> NodeResult:
    """Deduplica listas de elementos usando similitud difusa (difflib) sin LLM."""
    inp = Inputs(inputs)
    raw_data = inp.first()
    if not isinstance(raw_data, list):
        if isinstance(raw_data, dict):
            return NodeResult("ok", data=[raw_data])
        return NodeResult("ok", data=raw_data)

    th = max(0.1, min(1.0, float(threshold or 0.80)))
    strategy = (merge_strategy or "keep_first").lower().strip()

    def _get_str(it: Any) -> str:
        if isinstance(it, dict):
            if field and field in it:
                return str(it[field]).lower().strip()
            for cand in ("title", "name", "nombre", "titulo"):
                if cand in it:
                    return str(it[cand]).lower().strip()
            return " ".join(str(v) for v in it.values()).lower().strip()
        return str(it).lower().strip()

    def _calc_similarity(s1: str, s2: str) -> float:
        if s1 == s2:
            return 1.0
        import difflib

        ratio = difflib.SequenceMatcher(None, s1, s2).ratio()
        w1, w2 = set(re.findall(r"\w+", s1)), set(re.findall(r"\w+", s2))
        if w1 and w2:
            intersection = len(w1 & w2)
            subset_ratio = intersection / min(len(w1), len(w2))
            jaccard = intersection / len(w1 | w2)
            token_sim = max(jaccard, subset_ratio * 0.85)
            return max(ratio, token_sim)
        return ratio

    unique_items: list[Any] = []
    unique_strings: list[str] = []
    duplicates_map: dict[int, list[Any]] = defaultdict(list)

    for it in raw_data:
        s = _get_str(it)
        matched_idx = -1
        for idx, u_s in enumerate(unique_strings):
            sim = _calc_similarity(s, u_s)
            if sim >= th:
                matched_idx = idx
                break

        if matched_idx == -1:
            unique_strings.append(s)
            unique_items.append(it)
        else:
            duplicates_map[matched_idx].append(it)

    if strategy == "group":
        grouped = []
        for idx, it in enumerate(unique_items):
            cluster = [it] + duplicates_map[idx]
            grouped.append(cluster)
        return NodeResult("ok", data=grouped)

    elif strategy in ("merge_list", "merge"):
        merged = []
        for idx, it in enumerate(unique_items):
            dups = duplicates_map[idx]
            if isinstance(it, dict):
                item_copy = dict(it)
                if dups:
                    item_copy["duplicates"] = dups
                merged.append(item_copy)
            else:
                merged.append(it)
        return NodeResult("ok", data=merged)

    return NodeResult("ok", data=unique_items)


def aggregate(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    field: str = "",
    operation: str = "avg",
    group_by: str = "",
) -> NodeResult:
    """Calcula agregaciones y métricas estadísticas sobre listas de diccionarios."""
    inp = Inputs(inputs)
    raw_data = inp.first()
    if not isinstance(raw_data, list):
        return NodeResult("error", error=tr('The input must be a list of items.'))

    def _to_float(v: Any) -> float | None:
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.,-]", "", v).strip()
            if not cleaned:
                return None
            if "," in cleaned and "." not in cleaned:
                cleaned = cleaned.replace(",", ".")
            elif "," in cleaned and "." in cleaned:
                if cleaned.rfind(",") > cleaned.rfind("."):
                    cleaned = cleaned.replace(".", "").replace(",", ".")
                else:
                    cleaned = cleaned.replace(",", "")
            try:
                return float(cleaned)
            except ValueError:
                return None
        return None

    def _calc_metrics(vals: list[float], op: str) -> Any:
        if not vals:
            return 0.0 if op != "summary" else {}
        import statistics

        op_norm = (op or "avg").lower().strip()
        if op_norm == "count":
            return len(vals)
        elif op_norm == "sum":
            return round(sum(vals), 4)
        elif op_norm == "avg":
            return round(statistics.mean(vals), 4)
        elif op_norm == "min":
            return min(vals)
        elif op_norm == "max":
            return max(vals)
        elif op_norm == "median":
            return round(statistics.median(vals), 4)
        elif op_norm == "summary":
            return {
                "count": len(vals),
                "sum": round(sum(vals), 4),
                "avg": round(statistics.mean(vals), 4),
                "min": min(vals),
                "max": max(vals),
                "median": round(statistics.median(vals), 4),
            }
        return round(statistics.mean(vals), 4)

    grp_field = group_by.strip()
    if grp_field:
        groups: dict[str, list[float]] = defaultdict(list)
        for it in raw_data:
            if isinstance(it, dict):
                g_key = str(it.get(grp_field, "desconocido"))
                val = _to_float(it.get(field)) if field else _to_float(it)
                if val is not None:
                    groups[g_key].append(val)
        res_grouped = {g: _calc_metrics(v, operation) for g, v in groups.items()}
        return NodeResult("ok", data=res_grouped)

    values: list[float] = []
    for it in raw_data:
        val = _to_float(it.get(field)) if isinstance(it, dict) and field else _to_float(it)
        if val is not None:
            values.append(val)

    res = _calc_metrics(values, operation)
    return NodeResult("ok", data=res)


def format_table(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    format: str = "markdown",
    columns: str = "",
    headers: str = "",
    max_rows: int = 100,
) -> NodeResult:
    """Convierte una lista estructurada en una tabla formateada Markdown o HTML."""
    inp = Inputs(inputs)
    raw_data = inp.first()
    if not isinstance(raw_data, list):
        if isinstance(raw_data, dict):
            raw_data = [raw_data]
        else:
            return NodeResult("error", error=tr('format_table requires a list of dictionaries.'))

    if not raw_data:
        return NodeResult("ok", data="")

    cols = [c.strip() for c in columns.split(",") if c.strip()]
    if not cols:
        first = raw_data[0]
        if isinstance(first, dict):
            cols = list(first.keys())
        else:
            cols = ["valor"]

    hdrs = [h.strip() for h in headers.split(",") if h.strip()]
    if len(hdrs) < len(cols):
        hdrs += [cols[i].replace("_", " ").capitalize() for i in range(len(hdrs), len(cols))]

    limit = max(1, int(max_rows or 100))
    rows = raw_data[:limit]

    fmt = (format or "markdown").lower().strip()
    if fmt in ("html", "htm"):
        html_lines = [
            '<table style="border-collapse: collapse; width: 100%; font-family: sans-serif; font-size: 0.9rem;">',
            '  <thead>',
            '    <tr style="background: #f1f5f9; text-align: left;">',
        ]
        for h in hdrs:
            html_lines.append(f'      <th style="padding: 8px 12px; border: 1px solid #cbd5e1;">{html.escape(h)}</th>')
        html_lines.append('    </tr>\n  </thead>\n  <tbody>')
        for r in rows:
            html_lines.append('    <tr>')
            for c in cols:
                val = r.get(c, "") if isinstance(r, dict) else str(r)
                html_lines.append(f'      <td style="padding: 6px 12px; border: 1px solid #cbd5e1;">{html.escape(str(val))}</td>')
            html_lines.append('    </tr>')
        html_lines.append('  </tbody>\n</table>')
        return NodeResult("ok", data="\n".join(html_lines))

    md_lines = [
        "| " + " | ".join(hdrs) + " |",
        "| " + " | ".join(["---"] * len(hdrs)) + " |",
    ]
    for r in rows:
        row_vals = []
        for c in cols:
            val = r.get(c, "") if isinstance(r, dict) else str(r)
            clean_val = str(val).replace("|", "\\|").replace("\n", " ")
            row_vals.append(clean_val)
        md_lines.append("| " + " | ".join(row_vals) + " |")

    return NodeResult("ok", data="\n".join(md_lines))


def keywords_rake(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    max_keywords: int = 5,
    min_freq: int = 1,
    max_words: int = 3,
    language: str = "es",
    output_format: str = "list",
) -> NodeResult:
    """Extrae palabras y frases clave deterministas mediante RAKE en Python puro."""
    inp = Inputs(inputs)
    text = inp.text().strip()
    if not text:
        return NodeResult("ok", data=[] if output_format == "list" else "")

    stopwords = _SUMMARIZE_STOPWORDS
    sentences = re.split(r"[\.\!\?\,\;\:\(\)\[\]\n\t]+", text.lower())

    phrases: list[str] = []
    for s in sentences:
        words = s.strip().split()
        curr: list[str] = []
        for w in words:
            w_clean = re.sub(r"^\W+|\W+$", "", w)
            if not w_clean:
                continue
            if w_clean in stopwords:
                if curr:
                    phrases.append(" ".join(curr))
                    curr = []
            else:
                curr.append(w_clean)
        if curr:
            phrases.append(" ".join(curr))

    m_words = max(1, int(max_words or 3))
    m_freq = max(1, int(min_freq or 1))
    phrases = [p for p in phrases if 1 <= len(p.split()) <= m_words]

    word_freq = Counter()
    word_deg = defaultdict(int)
    for p in phrases:
        wlist = p.split()
        p_len = len(wlist)
        for w in wlist:
            word_freq[w] += 1
            word_deg[w] += p_len

    word_scores = {w: word_deg[w] / word_freq[w] for w in word_freq if word_freq[w] >= m_freq}

    phrase_scores: dict[str, float] = {}
    for p in phrases:
        wlist = p.split()
        if any(w not in word_scores for w in wlist):
            continue
        score = sum(word_scores[w] for w in wlist)
        if p not in phrase_scores or score > phrase_scores[p]:
            phrase_scores[p] = score

    sorted_phrases = sorted(phrase_scores.items(), key=lambda x: x[1], reverse=True)
    limit = max(1, int(max_keywords or 5))
    top_keywords = [p for p, _ in sorted_phrases[:limit]]

    fmt = (output_format or "list").lower().strip()
    if fmt in ("text", "texto", "string"):
        return NodeResult("ok", data=", ".join(top_keywords))
    return NodeResult("ok", data=top_keywords)


def ssl_cert(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    host: str,
    port: int = 443,
    timeout: float = 5.0,
) -> NodeResult:
    """Verifica la validez y los días restantes de un certificado SSL/TLS."""
    inp = Inputs(inputs)
    target = (host or inp.text()).strip()
    target = target.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    if not target:
        return NodeResult("error", error=tr('A hostname is required to verify the SSL certificate.'))

    target_port = int(port or 443)
    try:
        ctx_ssl = ssl.create_default_context()
        with socket.create_connection((target, target_port), timeout=float(timeout)) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=target) as ssock:
                cert = ssock.getpeercert()

        if not cert:
            return NodeResult("error", error=tr('Could not get the certificate of {target}', target=target))

        exp_str = cert.get("notAfter", "")
        exp_date = datetime.datetime.strptime(exp_str, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        days_left = (exp_date - now).days

        issuer_dict = dict(x[0] for x in cert.get("issuer", ()))
        subject_dict = dict(x[0] for x in cert.get("subject", ()))

        result = {
            "host": target,
            "port": target_port,
            "days_left": days_left,
            "expires_at": exp_date.isoformat(),
            "is_valid": days_left > 0,
            "issuer": issuer_dict.get("organizationName") or issuer_dict.get("commonName") or str(issuer_dict),
            "subject": subject_dict.get("commonName") or str(subject_dict),
            "serial_number": cert.get("serialNumber", ""),
        }
        return NodeResult("ok", data=result)
    except Exception as exc:
        return NodeResult("error", error=tr('Failed to check SSL certificate of {target}: {exc}', target=target, exc=exc))


def dns_check(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    host: str,
    record_type: str = "A",
    timeout: float = 5.0,
) -> NodeResult:
    """Verifica la resolución DNS de un host y mide el tiempo de respuesta."""
    inp = Inputs(inputs)
    target = (host or inp.text()).strip()
    target = target.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    if not target:
        return NodeResult("error", error=tr('A hostname is required to check DNS.'))

    try:
        t0 = time.perf_counter()
        name, aliases, ips = socket.gethostbyname_ex(target)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return NodeResult("ok", data={
            "host": target,
            "canonical_name": name,
            "aliases": aliases,
            "ips": ips,
            "response_time_ms": duration_ms,
        })
    except Exception as exc:
        return NodeResult("error", error=tr('DNS resolution failed for {target}: {exc}', target=target, exc=exc))


def ping_host(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    host: str,
    port: int = 80,
    mode: str = "tcp",
    timeout: float = 3.0,
) -> NodeResult:
    """Mide la latencia y disponibilidad de un host por TCP socket o ICMP."""
    inp = Inputs(inputs)
    target = (host or inp.text()).strip()
    target = target.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    if not target:
        return NodeResult("error", error=tr('A host is required to ping.'))

    m = (mode or "tcp").lower().strip()
    to_val = float(timeout or 3.0)

    if m == "icmp":
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(to_val))), target]
        try:
            t0 = time.perf_counter()
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=to_val + 1)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            if r.returncode == 0:
                m_lat = re.search(r"time=([\d.]+)\s*ms", r.stdout)
                lat = float(m_lat.group(1)) if m_lat else duration_ms
                return NodeResult("ok", data={"host": target, "alive": True, "latency_ms": lat, "mode": "icmp"})
            else:
                return NodeResult("error", error=tr('Host {target} unreachable via ICMP ping.', target=target))
        except Exception as exc:
            return NodeResult("error", error=f"Error en ping ICMP: {exc}")

    try:
        t0 = time.perf_counter()
        target_port = int(port or 80)
        with socket.create_connection((target, target_port), timeout=to_val):
            pass
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        return NodeResult("ok", data={
            "host": target,
            "port": target_port,
            "alive": True,
            "latency_ms": duration_ms,
            "mode": "tcp",
        })
    except Exception as exc:
        return NodeResult("error", error=tr('Could not connect to {target}:{port} ({exc})', target=target, port=port, exc=exc))


def _mqtt_encode_len(length: int) -> bytes:
    res = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length > 0:
            byte |= 128
        res.append(byte)
        if length == 0:
            break
    return bytes(res)


def mqtt_publish(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    broker: str = "localhost",
    port: int = 1883,
    topic: str = "",
    payload: str = "{entrada}",
    retain: bool = False,
    qos: int = 0,
    username: str = "",
    secret: str = "",
    timeout: float = 5.0,
) -> NodeResult:
    """Publica un mensaje en un broker MQTT sin librerías externas pesadas."""
    inp = Inputs(inputs)
    t = topic.strip()
    if not t:
        return NodeResult("error", error=tr("An MQTT 'topic' is required."))

    msg = payload.replace("{entrada}", inp.text()) if "{entrada}" in payload else (payload or inp.text())
    password = secrets.get(secret) if secret else ""

    try:
        flags = 0x02
        if username:
            flags |= 0x80
        if password:
            flags |= 0x40
        vh = b"\x00\x04MQTT\x04" + bytes([flags]) + (60).to_bytes(2, "big")
        cid = f"dedalo_{int(time.time())}".encode("utf-8")
        body = vh + len(cid).to_bytes(2, "big") + cid
        if username:
            u_b = username.encode("utf-8")
            body += len(u_b).to_bytes(2, "big") + u_b
        if password:
            p_b = password.encode("utf-8")
            body += len(p_b).to_bytes(2, "big") + p_b
        connect_packet = b"\x10" + _mqtt_encode_len(len(body)) + body

        byte1 = 0x30 | (1 if retain else 0) | ((int(qos) & 0x03) << 1)
        t_b = t.encode("utf-8")
        p_b = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
        pub_body = len(t_b).to_bytes(2, "big") + t_b + p_b
        publish_packet = bytes([byte1]) + _mqtt_encode_len(len(pub_body)) + pub_body

        disconnect_packet = b"\xe0\x00"

        with socket.create_connection((broker, int(port or 1883)), timeout=float(timeout)) as sock:
            sock.sendall(connect_packet)
            connack = sock.recv(4)
            if len(connack) < 4 or connack[0] != 0x20 or connack[3] != 0:
                err_code = connack[3] if len(connack) >= 4 else -1
                return NodeResult("error", error=tr('MQTT connection failure (CONNACK error code {err_code})', err_code=err_code))
            sock.sendall(publish_packet)
            sock.sendall(disconnect_packet)

        return NodeResult("ok", data=tr('Published to {t}: {msg}', t=t, msg=msg[:100]))
    except Exception as exc:
        return NodeResult("error", error=f"Error en MQTT publish: {exc}")


def mqtt_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    broker: str = "localhost",
    port: int = 1883,
    topic: str = "",
    username: str = "",
    secret: str = "",
    timeout: float = 5.0,
) -> NodeResult:
    """Lee o escucha un mensaje en un broker MQTT."""
    t = topic.strip()
    if not t:
        return NodeResult("error", error=tr("An MQTT 'topic' is required for reading."))

    password = secrets.get(secret) if secret else ""
    try:
        flags = 0x02
        if username:
            flags |= 0x80
        if password:
            flags |= 0x40
        vh = b"\x00\x04MQTT\x04" + bytes([flags]) + (60).to_bytes(2, "big")
        cid = f"dedalo_rx_{int(time.time())}".encode("utf-8")
        body = vh + len(cid).to_bytes(2, "big") + cid
        if username:
            u_b = username.encode("utf-8")
            body += len(u_b).to_bytes(2, "big") + u_b
        if password:
            p_b = password.encode("utf-8")
            body += len(p_b).to_bytes(2, "big") + p_b
        connect_packet = b"\x10" + _mqtt_encode_len(len(body)) + body

        pkt_id = 1
        t_b = t.encode("utf-8")
        sub_body = pkt_id.to_bytes(2, "big") + len(t_b).to_bytes(2, "big") + t_b + b"\x00"
        subscribe_packet = b"\x82" + _mqtt_encode_len(len(sub_body)) + sub_body

        with socket.create_connection((broker, int(port or 1883)), timeout=float(timeout)) as sock:
            sock.sendall(connect_packet)
            connack = sock.recv(4)
            if len(connack) < 4 or connack[0] != 0x20 or connack[3] != 0:
                return NodeResult("error", error=tr('Failed to connect to the MQTT broker.'))
            sock.sendall(subscribe_packet)
            sock.recv(5)

            sock.settimeout(float(timeout))
            data = sock.recv(4096)
            sock.sendall(b"\xe0\x00")

            if data and (data[0] & 0xF0) == 0x30:
                idx = 1
                rem_len = 0
                mult = 1
                while True:
                    b = data[idx]
                    rem_len += (b & 127) * mult
                    mult *= 128
                    idx += 1
                    if (b & 128) == 0:
                        break
                topic_len = int.from_bytes(data[idx:idx+2], "big")
                idx += 2 + topic_len
                payload_bytes = data[idx:]
                return NodeResult("ok", data=payload_bytes.decode("utf-8", errors="replace"))

        return NodeResult("ok", data="")
    except socket.timeout:
        return NodeResult("ok", data="")
    except Exception as exc:
        return NodeResult("error", error=f"Error en MQTT read: {exc}")


# ==============================================================================
# Sanitización de Correos Electrónicos y Protección Anti Prompt-Injection
# ==============================================================================

_INVISIBLE_CHARS_RE = re.compile(
    r"[\u200b\u200c\u200d\u2060\ufeff\u00ad"  # Zero-width spaces, joiners, soft hyphens, BOM
    r"\u202a-\u202e\u2066-\u2069"              # Bidi overrides & isolates
    r"\U000e0000-\U000e007f"                    # Unicode Tag plane (smuggled ASCII)
    r"]"
)

_HIDDEN_CSS_RE = re.compile(
    r"(?:"
    r"display\s*:\s*none|"
    r"visibility\s*:\s*hidden|"
    r"opacity\s*:\s*0(?:\.0+)?(?:\s|;|$)|"
    r"font-size\s*:\s*0(?:px|pt|em|rem|%)?(?:\s|;|$)|"
    r"color\s*:\s*(?:transparent|white|#fff(?:fff)?|rgba\([^)]*,\s*0(?:\.0+)?\))|"
    r"text-indent\s*:\s*-\d{3,}(?:px|em)?|"
    r"(?:left|top)\s*:\s*-\d{3,}(?:px|em)?|"
    r"height\s*:\s*0(?:px)?;\s*overflow\s*:\s*hidden|"
    r"overflow\s*:\s*hidden;\s*height\s*:\s*0(?:px)?"
    r")",
    re.IGNORECASE,
)

_SIGNATURE_PATTERNS = [
    r"^--\s*$",                             # Delimitador estándar RFC 3676 (-- )
    r"^_{3,}\s*$",                          # Separador de subrayados
    r"enviado\s+desde\s+mi\s+(?:iphone|ipad|galaxy|móvil|dispositivo|teléfono)",
    r"sent\s+from\s+my\s+(?:iphone|ipad|galaxy|phone|mobile|device)",
    r"obtener\s+outlook\s+para\s+(?:ios|android)",
    r"get\s+outlook\s+for\s+(?:ios|android)",
    r"enviado\s+desde\s+correo\s+para\s+windows",
    r"sent\s+from\s+mail\s+for\s+windows",
    r"descarga\s+la\s+aplicaci[oó]n\s+de\s+(?:gmail|outlook)",
]
_SIGNATURE_RE = re.compile(r"|".join(f"(?:{p})" for p in _SIGNATURE_PATTERNS), re.IGNORECASE | re.MULTILINE)

_DISCLAIMER_PATTERNS = [
    r"(?:este\s+mensaje|este\s+correo|la\s+informaci[oó]n\s+contenida\s+en\s+este\s+mensaje)[^\n.]{0,100}(?:confidencial|privilegiada|secreta)",
    r"(?:aviso\s+legal|cl[aá]usula\s+de\s+confidencialidad|pol[ií]tica\s+de\s+privacidad|protecci[oó]n\s+de\s+datos)",
    r"(?:this\s+email\s+and\s+any\s+files\s+transmitted|the\s+information\s+contained\s+in\s+this\s+email|this\s+message\s+is\s+confidential)",
    r"(?:confidentiality\s+notice|disclaimer\s*:|if\s+you\s+are\s+not\s+the\s+intended\s+recipient)",
]
_DISCLAIMER_RE = re.compile(r"|".join(f"(?:{p})" for p in _DISCLAIMER_PATTERNS), re.IGNORECASE | re.MULTILINE)

_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"
})


class EmailSanitizerParser(HTMLParser):
    """Convierte HTML de correo a texto limpio suprimiendo scripts, estilos,
    comentarios HTML y elementos ocultos vía CSS/atributos (anti prompt injection)."""

    def __init__(self):
        super().__init__()
        self.output_chunks: list[str] = []
        self.hidden_depth = 0
        self.ignored_tags = {"script", "style", "head", "title", "meta", "link", "template", "noscript", "iframe", "object", "embed"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag_lower = tag.lower()
        attr_dict = {k.lower(): (v or "") for k, v in attrs}

        is_hidden = False
        if tag_lower in self.ignored_tags:
            is_hidden = True
        elif "hidden" in attr_dict:
            is_hidden = True
        elif attr_dict.get("aria-hidden", "").lower() == "true":
            is_hidden = True
        else:
            style = attr_dict.get("style", "")
            if style and _HIDDEN_CSS_RE.search(style):
                is_hidden = True

        if is_hidden:
            if tag_lower not in _VOID_HTML_TAGS:
                self.hidden_depth += 1
            return
        if self.hidden_depth > 0:
            if tag_lower not in _VOID_HTML_TAGS:
                self.hidden_depth += 1
            return

        if tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"):
            self.output_chunks.append("\n")
        elif tag_lower == "br":
            self.output_chunks.append("\n")
        elif tag_lower == "li":
            self.output_chunks.append("\n• ")
        elif tag_lower in ("td", "th"):
            self.output_chunks.append(" | ")

    def handle_endtag(self, tag: str):
        tag_lower = tag.lower()
        if self.hidden_depth > 0:
            self.hidden_depth -= 1
            return

        if tag_lower in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol", "table"):
            self.output_chunks.append("\n")

    def handle_data(self, data: str):
        if self.hidden_depth > 0:
            return
        self.output_chunks.append(data)

    def handle_comment(self, data: str):
        # Descartar comentarios HTML por completo (vector común de prompt injection)
        pass

    def get_text(self) -> str:
        return "".join(self.output_chunks)


def clean_email_content(
    content: str,
    *,
    is_html: bool = False,
    strip_signatures: bool = True,
    strip_disclaimers: bool = True,
    max_chars: int = 3000,
) -> str:
    """Limpia el contenido de un correo: procesa HTML si aplica, suprime
    inyecciones ocultas, caracteres invisibles, firmas mecánicas y disclaimers."""
    if not content:
        return ""

    if is_html or ("<html" in content.lower() or "<body" in content.lower() or "<div" in content.lower() or "<p" in content.lower() or "<br" in content.lower()):
        try:
            parser = EmailSanitizerParser()
            parser.feed(content)
            parser.close()
            text = parser.get_text()
        except Exception:
            text = re.sub(r"<[^>]+>", " ", content)
    else:
        text = content

    # Descartar caracteres Unicode invisibles / tag plane (anti jailbreak smuggling)
    text = _INVISIBLE_CHARS_RE.sub("", text)

    lines = [line.strip() for line in text.splitlines()]
    clean_lines = []
    for line in lines:
        if strip_signatures and _SIGNATURE_RE.search(line):
            break
        clean_lines.append(line)

    result_text = "\n".join(clean_lines)

    if strip_disclaimers and _DISCLAIMER_RE.search(result_text):
        paragraphs = result_text.split("\n\n")
        retained = []
        for p in paragraphs:
            if not _DISCLAIMER_RE.search(p):
                retained.append(p)
        result_text = "\n\n".join(retained)

    result_text = re.sub(r"\n{3,}", "\n\n", result_text).strip()

    if max_chars and max_chars > 0:
        result_text = result_text[:max_chars]

    return result_text


def clean_email_node(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    strip_signatures: bool = True,
    strip_disclaimers: bool = True,
    max_chars: int = 3000,
) -> NodeResult:
    """Nodo de transformación para limpiar textos o listas de correos eliminando
    inyecciones ocultas, firmas y avisos legales."""
    inp = Inputs(inputs)
    first = inp.first_data()

    if isinstance(first, list):
        cleaned_list = []
        for item in first:
            if isinstance(item, dict):
                new_item = dict(item)
                body = new_item.get("body", "")
                if body:
                    new_item["body"] = clean_email_content(
                        str(body),
                        strip_signatures=bool(strip_signatures),
                        strip_disclaimers=bool(strip_disclaimers),
                        max_chars=int(max_chars or 3000),
                    )
                if "subject" in new_item:
                    new_item["subject"] = _INVISIBLE_CHARS_RE.sub("", str(new_item["subject"])).strip()
                cleaned_list.append(new_item)
            else:
                cleaned_list.append(clean_email_content(
                    str(item),
                    strip_signatures=bool(strip_signatures),
                    strip_disclaimers=bool(strip_disclaimers),
                    max_chars=int(max_chars or 3000),
                ))
        return NodeResult("ok", data=cleaned_list)

    text = inp.text()
    cleaned = clean_email_content(
        text,
        strip_signatures=bool(strip_signatures),
        strip_disclaimers=bool(strip_disclaimers),
        max_chars=int(max_chars or 3000),
    )
    return NodeResult("ok", data=cleaned)


def imap_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    host: str = "",
    port: int = 993,
    username: str = "",
    secret: str = "",
    folder: str = "INBOX",
    filter: str = "UNSEEN",
    limit: int = 5,
    mark_as_read: bool = False,
    timeout: float = 15.0,
    clean_body: bool = True,
    strip_signatures: bool = True,
    max_body_chars: int = 3000,
) -> NodeResult:
    """Lee correos electrónicos vía IMAP sobre TLS con imaplib nativo y sanitización anti prompt-injection."""
    h = host.strip()
    u = username.strip()
    if not h or not u:
        return NodeResult("error", error=tr("'host' and 'username' are required for the IMAP connection."))

    pwd = secrets.get(secret) if secret else ""
    if not pwd:
        return NodeResult("error", error=tr('A secret with the password or App Password is required for IMAP.'))

    max_items = max(1, int(limit or 5))
    try:
        import imaplib

        mail = imaplib.IMAP4_SSL(h, int(port or 993), timeout=float(timeout))
        mail.login(u, pwd)
        mail.select(folder or "INBOX")

        crit = filter.strip() or "UNSEEN"
        status, messages = mail.search(None, crit)
        if status != "OK" or not messages or not messages[0]:
            mail.logout()
            return NodeResult("ok", data=[])

        mail_ids = messages[0].split()
        selected_ids = mail_ids[-max_items:]
        selected_ids.reverse()

        emails_res: list[dict[str, Any]] = []
        for mid in selected_ids:
            fetch_cmd = "(RFC822)" if mark_as_read else "(BODY.PEEK[])"
            status, data = mail.fetch(mid, fetch_cmd)
            if status != "OK" or not data or not data[0]:
                continue

            raw_email = data[0][1]
            if not isinstance(raw_email, bytes):
                continue

            msg = email.message_from_bytes(raw_email)

            def _decode_header_str(header_val: str) -> str:
                if not header_val:
                    return ""
                decoded = []
                for part, enc in email.header.decode_header(header_val):
                    if isinstance(part, bytes):
                        try:
                            decoded.append(part.decode(enc or "utf-8", errors="replace"))
                        except Exception:
                            decoded.append(part.decode("latin-1", errors="replace"))
                    else:
                        decoded.append(str(part))
                return "".join(decoded)

            subject = _decode_header_str(msg.get("Subject", ""))
            sender = _decode_header_str(msg.get("From", ""))
            date_str = msg.get("Date", "")

            body_text = ""
            html_text = ""
            attachment_names = []
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disp = str(part.get("Content-Disposition", ""))
                    if "attachment" in content_disp:
                        fn = part.get_filename()
                        if fn:
                            attachment_names.append(_decode_header_str(fn))
                    elif content_type == "text/plain" and not body_text:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    elif content_type == "text/html" and not html_text:
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            else:
                content_type = msg.get_content_type()
                payload = msg.get_payload(decode=True)
                if payload:
                    decoded = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
                    if content_type == "text/html":
                        html_text = decoded
                    else:
                        body_text = decoded

            raw_content = body_text if body_text else html_text
            is_html = not bool(body_text) and bool(html_text)

            if clean_body:
                final_body = clean_email_content(
                    raw_content,
                    is_html=is_html,
                    strip_signatures=strip_signatures,
                    max_chars=int(max_body_chars or 3000),
                )
            else:
                final_body = raw_content[:int(max_body_chars or 3000)]

            clean_subject = _INVISIBLE_CHARS_RE.sub("", subject).strip()
            clean_sender = _INVISIBLE_CHARS_RE.sub("", sender).strip()

            emails_res.append({
                "id": mid.decode() if isinstance(mid, bytes) else str(mid),
                "subject": clean_subject,
                "from": clean_sender,
                "date": date_str,
                "body": final_body,
                "attachments": attachment_names,
            })

        mail.logout()
        return NodeResult("ok", data=emails_res)
    except Exception as exc:
        return NodeResult("error", error=f"Error leyendo IMAP: {exc}")


def delay_execution(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    seconds: float = 2.0,
    jitter: float = 0.0,
) -> NodeResult:
    """Pausa la ejecución durante un tiempo determinado antes de emitir los datos sin modificar."""
    inp = Inputs(inputs)
    wait_time = max(0.0, float(seconds))
    if jitter > 0:
        wait_time += random.uniform(0, float(jitter))
    # Limitar a un máximo de 300 segundos para evitar bloqueos
    wait_time = min(wait_time, 300.0)
    if wait_time > 0:
        time.sleep(wait_time)

    first_data = inp.first()
    if first_data is not None:
        return NodeResult("ok", data=first_data)
    text_val = inp.text()
    return NodeResult("ok", data=text_val or tr('Wait completed ({wait_time:.1f}s)', wait_time=wait_time))


SPANISH_MONTHS = [
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
]
SPANISH_DAYS = [
    "lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"
]


def datetime_ops(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    mode: str = "format",
    preset: str = "es_long",
    format_str: str = "",
    offset_days: int = 0,
    offset_hours: int = 0,
    offset_minutes: int = 0,
    timezone: str = "Europe/Madrid",
    output_format: str = "text",
) -> NodeResult:
    """Calcula, formatea y manipula fechas y horas."""
    inp = Inputs(inputs)

    # 1. Determinar zona horaria
    tz: datetime.tzinfo = datetime.timezone.utc
    if timezone:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(timezone.strip())
        except Exception:
            tz = datetime.timezone.utc

    # 2. Determinar fecha base
    dt: datetime.datetime | None = None
    if mode == "now" or not inp.text().strip():
        dt = datetime.datetime.now(tz=tz)
    else:
        raw_val = inp.text().strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y",
        ):
            try:
                dt = datetime.datetime.strptime(raw_val.replace("Z", "+0000"), fmt)
                break
            except Exception:
                pass
        if dt is None:
            dt = datetime.datetime.now(tz=tz)
        elif dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)

    # 3. Aplicar desfases
    delta = datetime.timedelta(
        days=int(offset_days),
        hours=int(offset_hours),
        minutes=int(offset_minutes),
    )
    dt = dt + delta

    # 4. Formatear según preset
    weekday_es = SPANISH_DAYS[dt.weekday()].capitalize()
    month_es = SPANISH_MONTHS[dt.month].capitalize()

    if preset == "es_long":
        formatted = f"{weekday_es}, {dt.day} de {month_es} de {dt.year}"
    elif preset == "es_short":
        formatted = f"{dt.day} de {month_es[:3]} de {dt.year} {dt.strftime('%H:%M')}"
    elif preset == "date_only":
        formatted = dt.strftime("%Y-%m-%d")
    elif preset == "time_only":
        formatted = dt.strftime("%H:%M:%S")
    elif preset == "iso":
        formatted = dt.isoformat()
    elif preset == "custom" and format_str.strip():
        try:
            formatted = dt.strftime(format_str.strip())
        except Exception:
            formatted = dt.isoformat()
    else:
        formatted = dt.strftime(format_str.strip() if format_str.strip() else "%Y-%m-%d %H:%M:%S")

    data = {
        "iso": dt.isoformat(),
        "formatted": formatted,
        "timestamp": int(dt.timestamp()),
        "year": dt.year,
        "month": dt.month,
        "month_es": month_es,
        "day": dt.day,
        "weekday_es": weekday_es,
        "hour": dt.hour,
        "minute": dt.minute,
        "second": dt.second,
        "timezone": str(tz),
    }
    if output_format == "dict":
        return NodeResult("ok", data=data)
    elif output_format == "iso":
        return NodeResult("ok", data=dt.isoformat())
    return NodeResult("ok", data=formatted)


def sitemap_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str = "",
    max_urls: int = 50,
    filter_hours: float = 0.0,
    filter_keyword: str = "",
    follow_sub_sitemaps: bool = True,
    output_format: str = "json",
) -> NodeResult:
    """Descarga y analiza un sitemap.xml para extraer URLs y fechas de modificación."""
    inp = Inputs(inputs)
    target_url = (url or inp.text()).strip()
    if not target_url:
        return NodeResult("error", error=tr('Missing sitemap URL.'))
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        target_url = "https://" + target_url

    max_limit = min(max(1, int(max_urls)), 500)
    cutoff_time = None
    if filter_hours and float(filter_hours) > 0:
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=float(filter_hours))

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Dedalo/1.0; +https://github.com/pixelotes/dedalo)",
        "Accept": "application/xml, text/xml, */*",
    }

    try:
        def fetch_xml(u: str) -> str:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
                resp = client.get(u)
                resp.raise_for_status()
                return resp.text

        def parse_sitemap_content(xml_text: str) -> tuple[list[dict[str, str]], list[str]]:
            urls_found: list[dict[str, str]] = []
            sub_sitemaps: list[str] = []
            try:
                import xml.etree.ElementTree as ET

                root = ET.fromstring(xml_text)
            except Exception:
                return urls_found, sub_sitemaps

            ns_pattern = re.compile(r"\{.*\}")
            for elem in root:
                tag = ns_pattern.sub("", elem.tag).lower()
                if tag == "sitemap":
                    loc = ""
                    for child in elem:
                        ctag = ns_pattern.sub("", child.tag).lower()
                        if ctag == "loc" and child.text:
                            loc = child.text.strip()
                    if loc:
                        sub_sitemaps.append(loc)
                elif tag == "url":
                    loc = ""
                    lastmod = ""
                    for child in elem:
                        ctag = ns_pattern.sub("", child.tag).lower()
                        if ctag == "loc" and child.text:
                            loc = child.text.strip()
                        elif ctag == "lastmod" and child.text:
                            lastmod = child.text.strip()
                    if loc:
                        urls_found.append({"url": loc, "lastmod": lastmod})
            return urls_found, sub_sitemaps

        xml_content = fetch_xml(target_url)
        found_urls, sub_maps = parse_sitemap_content(xml_content)

        if not found_urls and sub_maps and follow_sub_sitemaps:
            for sub_url in sub_maps[:10]:
                if len(found_urls) >= max_limit:
                    break
                try:
                    sub_xml = fetch_xml(sub_url)
                    sub_urls, _ = parse_sitemap_content(sub_xml)
                    found_urls.extend(sub_urls)
                except Exception:
                    continue

        filtered: list[dict[str, str]] = []
        for item in found_urls:
            u_loc = item["url"]
            if filter_keyword and filter_keyword.lower() not in u_loc.lower():
                continue
            if cutoff_time and item.get("lastmod"):
                try:
                    lm_str = item["lastmod"]
                    if "T" in lm_str:
                        lm_dt = datetime.datetime.fromisoformat(lm_str.replace("Z", "+00:00"))
                    else:
                        lm_dt = datetime.datetime.strptime(lm_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
                    if lm_dt < cutoff_time:
                        continue
                except Exception:
                    pass
            filtered.append(item)
            if len(filtered) >= max_limit:
                break

        text_lines = [f"- {it['url']}" + (f" ({it['lastmod']})" if it.get("lastmod") else "") for it in filtered]
        if output_format in ("text", "markdown"):
            return NodeResult("ok", data="\n".join(text_lines))
        return NodeResult("ok", data=filtered)
    except Exception as exc:
        return NodeResult("error", error=f"Error leyendo sitemap: {exc}")


def healthcheck_ping(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str = "",
    method: str = "GET",
    send_status: str = "auto",
    send_payload: bool = False,
) -> NodeResult:
    """Envía un ping/heartbeat a Healthchecks.io, Uptime Kuma o servicios similares."""
    inp = Inputs(inputs)
    target_url = (url or inp.text()).strip()
    if not target_url:
        return NodeResult("error", error=tr('Missing monitoring (healthcheck) URL.'))

    has_prior_error = any(r.failed for r in inputs)
    is_success = not has_prior_error

    if send_status == "fail":
        is_success = False
    elif send_status == "success":
        is_success = True

    final_url = target_url
    if "hc-ping.com" in final_url or "/ping/" in final_url:
        if not is_success and not final_url.endswith("/fail"):
            final_url = final_url.rstrip("/") + "/fail"
    elif "/api/push/" in final_url:
        delim = "&" if "?" in final_url else "?"
        status_val = "up" if is_success else "down"
        msg_val = "OK" if is_success else "Error"
        final_url = f"{final_url}{delim}status={status_val}&msg={msg_val}"

    body_text = None
    if send_payload:
        first_data = inp.first()
        if first_data is not None:
            if isinstance(first_data, (dict, list)):
                body_text = json.dumps(first_data, ensure_ascii=False)[:4000]
            else:
                body_text = str(first_data)[:4000]
        else:
            body_text = inp.text()[:4000]

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            if method.upper() == "POST":
                resp = client.post(final_url, content=body_text.encode("utf-8") if body_text else None)
            else:
                resp = client.get(final_url)
        return NodeResult(
            "ok",
            data={
                "url": final_url,
                "status_code": resp.status_code,
                "ok": resp.is_success,
                "is_success_signal": is_success,
                "message": tr('Healthcheck ping sent to {final_url} (HTTP {status_code})', final_url=final_url, status_code=resp.status_code),
            },
        )
    except Exception as exc:
        return NodeResult("error", error=tr('Error sending healthcheck ping: {exc}', exc=exc))


WMO_WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: (tr('Clear'), "☀️"),
    1: (tr('Mostly clear'), "🌤️"),
    2: (tr('Partly cloudy'), "⛅"),
    3: (tr('Overcast'), "☁️"),
    45: (tr('Fog'), "🌫️"),
    48: (tr('Rime fog'), "🌫️"),
    51: (tr('Light drizzle'), "🌦️"),
    53: (tr('Moderate drizzle'), "🌦️"),
    55: (tr('Dense drizzle'), "🌧️"),
    61: (tr('Light rain'), "🌧️"),
    63: (tr('Moderate rain'), "🌧️"),
    65: (tr('Heavy rain'), "🌧️"),
    71: (tr('Light snow'), "🌨️"),
    73: (tr('Moderate snow'), "🌨️"),
    75: (tr('Heavy snow'), "❄️"),
    77: (tr('Snow grains'), "🌨️"),
    80: (tr('Light showers'), "🌦️"),
    81: (tr('Moderate showers'), "🌧️"),
    82: (tr('Violent showers'), "⛈️"),
    85: (tr('Snow showers'), "🌨️"),
    86: (tr('Heavy snow showers'), "❄️"),
    95: (tr('Thunderstorm'), "⛈️"),
    96: (tr('Thunderstorm with light hail'), "⛈️"),
    99: (tr('Thunderstorm with heavy hail'), "⛈️"),
}


def weather_forecast(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    city: str = "Valencia",
    latitude: float = 0.0,
    longitude: float = 0.0,
    forecast_days: int = 1,
    format: str = "summary",
) -> NodeResult:
    """Obtiene datos meteorológicos y previsión desde Open-Meteo sin necesidad de API key."""
    inp = Inputs(inputs)
    target_city = city.strip()
    if not target_city and inp.text().strip():
        target_city = inp.text().strip()

    lat = float(latitude)
    lon = float(longitude)
    resolved_city = target_city or tr('Location')

    try:
        with httpx.Client(timeout=10.0) as client:
            if (lat == 0.0 and lon == 0.0) and target_city:
                geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(target_city)}&count=1&language=es&format=json"
                geo_resp = client.get(geo_url)
                geo_resp.raise_for_status()
                geo_data = geo_resp.json()
                results = geo_data.get("results")
                if not results:
                    return NodeResult("error", error=tr("Could not geocode the city '{target_city}'.", target_city=target_city))
                lat = results[0]["latitude"]
                lon = results[0]["longitude"]
                resolved_city = results[0].get("name", target_city)
                country = results[0].get("country", "")
                if country:
                    resolved_city = f"{resolved_city}, {country}"

            if lat == 0.0 and lon == 0.0:
                lat, lon = 39.4699, -0.3763

            days = min(max(1, int(forecast_days)), 7)
            api_url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
                f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
                f"&forecast_days={days}&timezone=auto"
            )
            resp = client.get(api_url)
            resp.raise_for_status()
            wdata = resp.json()

        curr = wdata.get("current", {})
        daily = wdata.get("daily", {})

        w_code = curr.get("weather_code", 0)
        desc, emoji = WMO_WEATHER_CODES.get(w_code, (tr('Unknown'), "🌡️"))
        temp = curr.get("temperature_2m", 0.0)
        feels_like = curr.get("apparent_temperature", temp)
        humidity = curr.get("relative_humidity_2m", 0)
        wind = curr.get("wind_speed_10m", 0.0)
        precip = curr.get("precipitation", 0.0)

        daily_summary = []
        if daily and daily.get("time"):
            for i, d_date in enumerate(daily["time"]):
                d_code = daily["weather_code"][i] if i < len(daily.get("weather_code", [])) else 0
                d_desc, d_emoji = WMO_WEATHER_CODES.get(d_code, (tr('Unknown'), "🌡️"))
                daily_summary.append({
                    "date": d_date,
                    "max_temp": daily["temperature_2m_max"][i] if i < len(daily.get("temperature_2m_max", [])) else None,
                    "min_temp": daily["temperature_2m_min"][i] if i < len(daily.get("temperature_2m_min", [])) else None,
                    "precip_prob": daily["precipitation_probability_max"][i] if i < len(daily.get("precipitation_probability_max", [])) else None,
                    "weather_desc": d_desc,
                    "emoji": d_emoji,
                })

        summary_dict = {
            "city": resolved_city,
            "latitude": lat,
            "longitude": lon,
            "temperature": temp,
            "apparent_temperature": feels_like,
            "weather_desc": desc,
            "emoji": emoji,
            "humidity": humidity,
            "wind_speed_kmh": wind,
            "precipitation_mm": precip,
            "forecast": daily_summary,
        }

        today_forecast = daily_summary[0] if daily_summary else {}
        prob_str = tr(' · 🌧️ {precip_prob}% rain', precip_prob=today_forecast.get('precip_prob')) if today_forecast.get("precip_prob") is not None else ""
        temp_range = tr(' (Min: {min_temp}°C / Max: {max_temp}°C)', min_temp=today_forecast.get('min_temp'), max_temp=today_forecast.get('max_temp')) if today_forecast.get("max_temp") is not None else ""

        markdown_text = (
            tr((
                '{emoji} **{resolved_city}**: {temp}°C (Feels like {feels_like}°C) · {desc}\n'
                '💨 Wind {wind} km/h · 💧 Humidity {humidity}%{prob_str}{temp_range}'
            ), emoji=emoji, resolved_city=resolved_city, temp=temp, feels_like=feels_like, desc=desc, wind=wind, humidity=humidity, prob_str=prob_str, temp_range=temp_range)
        )
        summary_dict["markdown"] = markdown_text

        if format == "raw":
            return NodeResult("ok", data=wdata)
        elif format == "markdown":
            return NodeResult("ok", data=markdown_text)
        else:
            return NodeResult("ok", data=summary_dict)
    except Exception as exc:
        return NodeResult("error", error=f"Error consultando Open-Meteo: {exc}")


def summarize(results_dict: dict[str, NodeResult], ctx: Any) -> dict[str, Any]:
    """Resume la ejecución de todos los nodos de un flujo."""
    summary = {}
    for node_id, res in results_dict.items():
        summary[node_id] = {
            "status": res.status,
            "data": res.data if res.ok else None,
            "error": res.error if res.failed else None,
            "duration_ms": getattr(res, "duration", 0.0),
        }
    return summary


@dataclass
class _PreviewContext:
    flow_name: str = "preview"
    started_at: float = field(default_factory=time.time)
    payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)


def execute_single_node(
    node_type: str,
    config: dict[str, Any] | None = None,
    inputs: list[Any] | Any = None,
    code: str = "",
    flow_name: str = "preview",
    ctx: Any = None,
) -> NodeResult:
    """Ejecuta un nodo aislado en memoria con sus entradas y configuración.

    Ideal para pruebas rápidas de nodos individuales, depuración paso a paso
    y vista previa en tiempo real de transformaciones (selectores CSS, regex, templates, etc.).
    """
    import sys
    import textwrap
    from dedalo import compiler

    if node_type not in compiler.NODE_TYPES:
        return NodeResult("error", error=tr('Unknown node type: "{node_type}"', node_type=node_type))

    spec = compiler.NODE_TYPES[node_type]

    # Normalizar inputs a lista de NodeResult
    items: list[NodeResult] = []
    if inputs is None:
        items = []
    elif isinstance(inputs, list):
        for it in inputs:
            if isinstance(it, NodeResult):
                items.append(it)
            elif isinstance(it, dict) and "status" in it:
                items.append(NodeResult(
                    status=str(it.get("status", "ok")),
                    data=it.get("data"),
                    error=str(it.get("error", "")),
                ))
            else:
                items.append(NodeResult("ok", data=it))
    elif isinstance(inputs, NodeResult):
        items = [inputs]
    elif isinstance(inputs, dict) and "status" in inputs:
        items = [NodeResult(
            status=str(inputs.get("status", "ok")),
            data=inputs.get("data"),
            error=str(inputs.get("error", "")),
        )]
    else:
        items = [NodeResult("ok", data=inputs)]

    # Crear contexto si no se facilitó uno
    if ctx is None:
        ctx = _PreviewContext(flow_name=flow_name)

    cfg = {**spec.optional, **(config or {})}
    cfg.pop("fan_in", None)
    code_field = spec.code_field or ("code" if node_type == "transform.script" else "")
    if code_field:
        script_code = code or cfg.pop(code_field, "")
    else:
        script_code = ""

    t0 = time.time()
    try:
        if code_field:
            body = textwrap.dedent(script_code or "").strip("\n")
            lines = ["def _cuerpo(entrada, ctx):"]
            lines += [f"    {line}" if line.strip() else "" for line in body.splitlines()] or [
                "    return None"
            ]
            ns: dict[str, Any] = {}
            exec("\n".join(lines), ns)
            fn = ns["_cuerpo"]
            res = script(ctx, items, fn)
        else:
            runtime_fn = getattr(sys.modules[__name__], spec.runtime_fn, None)
            if not runtime_fn or not callable(runtime_fn):
                return NodeResult("error", error=tr('Runtime function not found for "{node_type}"', node_type=node_type))
            res = runtime_fn(ctx, items, **cfg)
    except Exception as exc:
        res = NodeResult("error", error=tr('Exception while running node: {exc}', exc=exc))

    elapsed_ms = round((time.time() - t0) * 1000, 2)
    res.duration = elapsed_ms
    return res


# ==============================================================================
# Integración con Git (Nativa vía CLI)
# ==============================================================================

def _run_git(args: list[str], cwd: str | Path | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    """Ejecuta un comando git y devuelve (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=float(timeout),
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", tr("The 'git' executable is not installed or not available in the system PATH.")
    except subprocess.TimeoutExpired:
        return 124, "", tr('Timed out while running git ({timeout}s).', timeout=timeout)
    except Exception as exc:
        return 1, "", tr('Unexpected error running git: {exc}', exc=exc)


def git_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    action: str = "log",
    repo_path: str = ".",
    url: str = "",
    revision: str = "HEAD~1..HEAD",
    since: str = "",
    limit: int = 5,
    path_filter: str = "",
    max_chars: int = 8000,
    timeout: float = 30.0,
) -> NodeResult:
    """Lee información de un repositorio Git: historial de commits, diffs, clonado ligero o estado."""
    act = (action or "log").strip().lower()
    path_obj = Path(repo_path or ".").expanduser().resolve()

    if act == "clone_or_pull":
        target_url = url.strip()
        if not target_url and not (path_obj / ".git").exists():
            return NodeResult("error", error=tr("'url' is required to clone a nonexistent remote repository."))

        if (path_obj / ".git").exists():
            code, out, err = _run_git(["pull", "--ff-only"], cwd=path_obj, timeout=timeout)
            if code != 0:
                return NodeResult("error", error=f"Fallo en git pull: {err or out}")
            return NodeResult("ok", data={"action": "pull", "path": str(path_obj), "output": out or "Already up to date."})

        # Clonar shallow con limite de profundidad 1
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        code, out, err = _run_git(["clone", "--depth", "1", "--no-tags", target_url, str(path_obj)], timeout=timeout)
        if code != 0:
            return NodeResult("error", error=f"Fallo en git clone: {err or out}")
        return NodeResult("ok", data={"action": "clone", "path": str(path_obj), "output": out or "Cloned successfully."})

    # Para el resto de acciones, el repositorio local debe existir
    if not (path_obj / ".git").exists() and not (path_obj.is_dir() and (path_obj / "HEAD").exists()):
        return NodeResult("error", error=tr("The path '{repo_path}' is not a valid Git repository.", repo_path=repo_path))

    if act == "log":
        max_commits = max(1, int(limit or 5))
        # Formato delimitado por centinela: \x1e al inicio del commit, \x1f entre campos del header
        format_str = "%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"
        cmd = ["log", f"-n{max_commits}", f"--format={format_str}", "--name-only"]
        if since.strip():
            cmd.append(f"--since={since.strip()}")
        if path_filter.strip():
            cmd.append("--")
            cmd.extend([p.strip() for p in path_filter.split(",") if p.strip()])

        code, out, err = _run_git(cmd, cwd=path_obj, timeout=timeout)
        if code != 0:
            return NodeResult("error", error=f"Fallo en git log: {err or out}")

        if not out:
            return NodeResult("ok", data=[])

        commits = []
        entries = [e.strip() for e in out.split("\x1e") if e.strip()]
        for entry in entries:
            lines = entry.splitlines()
            if not lines:
                continue
            header = lines[0].strip()
            files = [f.strip() for f in lines[1:] if f.strip()]
            parts = header.split("\x1f")
            if len(parts) >= 6:
                commits.append({
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "author": parts[2],
                    "email": parts[3],
                    "date": parts[4],
                    "message": parts[5],
                    "files": files,
                })
        return NodeResult("ok", data=commits)

    elif act == "diff":
        rev = revision.strip() or "HEAD~1..HEAD"
        cmd_diff = ["diff", rev]
        cmd_stat = ["diff", "--stat", rev]

        if path_filter.strip():
            filter_args = ["--", *[p.strip() for p in path_filter.split(",") if p.strip()]]
            cmd_diff.extend(filter_args)
            cmd_stat.extend(filter_args)

        code_stat, out_stat, err_stat = _run_git(cmd_stat, cwd=path_obj, timeout=timeout)
        if code_stat != 0:
            return NodeResult("error", error=f"Fallo en git diff --stat: {err_stat or out_stat}")

        code_diff, out_diff, err_diff = _run_git(cmd_diff, cwd=path_obj, timeout=timeout)
        if code_diff != 0:
            return NodeResult("error", error=f"Fallo en git diff: {err_diff or out_diff}")

        code_files, out_files, _ = _run_git(["diff", "--name-only", rev], cwd=path_obj, timeout=timeout)
        files_list = [f.strip() for f in out_files.splitlines() if f.strip()] if code_files == 0 else []

        max_len = int(max_chars or 8000)
        diff_text = out_diff[:max_len]
        truncated = len(out_diff) > max_len

        return NodeResult("ok", data={
            "revision": rev,
            "stat": out_stat,
            "files": files_list,
            "diff": diff_text,
            "truncated": truncated,
            "total_chars": len(out_diff),
        })

    elif act == "status":
        code, out, err = _run_git(["status", "--porcelain"], cwd=path_obj, timeout=timeout)
        if code != 0:
            return NodeResult("error", error=f"Fallo en git status: {err or out}")

        changes = []
        for line in out.splitlines():
            line = line.strip()
            if len(line) >= 3:
                status_code = line[:2].strip()
                file_path = line[3:].strip()
                changes.append({"status": status_code, "file": file_path})

        return NodeResult("ok", data={
            "clean": len(changes) == 0,
            "total_changes": len(changes),
            "changes": changes,
        })

    return NodeResult("error", error=tr('Unrecognized git action: "{action}". Use log, diff, clone_or_pull or status.', action=action))


def git_commit_push(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    repo_path: str = ".",
    message: str = "update: automated commit from dedalo",
    push: bool = False,
    branch: str = "main",
    remote: str = "origin",
    files: str = ".",
    author_name: str = "Dedalo Bot",
    author_email: str = "dedalo@local",
    timeout: float = 30.0,
) -> NodeResult:
    """Añade cambios, realiza un commit y opcionalmente ejecuta push en un repositorio Git."""
    path_obj = Path(repo_path or ".").expanduser().resolve()
    if not (path_obj / ".git").exists():
        return NodeResult("error", error=tr("The path '{repo_path}' is not a valid Git repository.", repo_path=repo_path))

    file_patterns = [f.strip() for f in (files or ".").split(",") if f.strip()] or ["."]
    code, _, err = _run_git(["add", *file_patterns], cwd=path_obj, timeout=timeout)
    if code != 0:
        return NodeResult("error", error=f"Fallo en git add: {err}")

    code_diff, out_diff, _ = _run_git(["diff", "--cached", "--name-only"], cwd=path_obj, timeout=timeout)
    if not out_diff.strip():
        return NodeResult("ok", data={"committed": False, "pushed": False, "message": tr('No pending changes to commit.')})

    author_str = f"{author_name.strip()} <{author_email.strip()}>" if author_name and author_email else ""
    cmd_commit = ["commit", "-m", message.strip() or "update"]
    if author_str:
        cmd_commit.extend(["--author", author_str])

    code, out_commit, err = _run_git(cmd_commit, cwd=path_obj, timeout=timeout)
    if code != 0:
        return NodeResult("error", error=f"Fallo en git commit: {err or out_commit}")

    _, commit_hash, _ = _run_git(["rev-parse", "HEAD"], cwd=path_obj, timeout=timeout)

    pushed = False
    push_out = ""
    if push:
        cmd_push = ["push", remote.strip() or "origin", branch.strip() or "main"]
        code_p, push_out, push_err = _run_git(cmd_push, cwd=path_obj, timeout=timeout)
        if code_p != 0:
            return NodeResult("error", error=tr('Commit created ({commit_hash}), but git push failed: {push_out}', commit_hash=commit_hash[:7], push_out=push_err or push_out))
        pushed = True

    return NodeResult("ok", data={
        "committed": True,
        "commit_hash": commit_hash,
        "short_hash": commit_hash[:7],
        "message": message.strip(),
        "pushed": pushed,
        "push_output": push_out,
    })


# ---------------------------------------------------------------------------
# Auditoría de Seguridad de Vulnerabilidades (SBOM / CVE con OSV.dev)
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {
    "NONE": 0,
    "LOW": 1,
    "MEDIUM": 2,
    "MODERATE": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}

_PURL_ECOSYSTEM_MAP = {
    "pypi": "PyPI",
    "npm": "npm",
    "maven": "Maven",
    "golang": "Go",
    "go": "Go",
    "deb": "Debian",
    "debian": "Debian",
    "apk": "Alpine",
    "alpine": "Alpine",
    "nuget": "NuGet",
    "composer": "Packagist",
    "cargo": "crates.io",
    "gem": "RubyGems",
    "hex": "Hex",
    "pub": "Pub",
}


def _detect_ecosystem_from_purl(purl: str) -> str:
    """Extrae el ecosistema a partir de un Package URL (PURL)."""
    if not purl or not purl.startswith("pkg:"):
        return ""
    scheme_part = purl.split("/", 1)[0]
    pkg_type = scheme_part.replace("pkg:", "").lower()
    return _PURL_ECOSYSTEM_MAP.get(pkg_type, "")


def _parse_purl(purl: str) -> tuple[str, str, str]:
    """Extrae (name, version, ecosystem) de un purl estándar."""
    ecosystem = _detect_ecosystem_from_purl(purl)
    version = ""
    rest = purl
    if "@" in purl:
        rest, version = purl.rsplit("@", 1)
        if "?" in version:
            version = version.split("?", 1)[0]
    name = rest.split("/", 1)[-1] if "/" in rest else ""
    return name, version, ecosystem


def _parse_sbom_components(data: Any, default_eco: str = "") -> list[dict[str, str]]:
    """Extrae lista normalizada de paquetes [{"name": ..., "version": ..., "ecosystem": ...}]
    a partir de CycloneDX JSON, SPDX JSON, listas de diccionarios o texto plano."""
    components: list[dict[str, str]] = []

    # 1. Si es un string (archivo requirements.txt, bom json, etc.)
    if isinstance(data, str):
        data_str = data.strip()
        if data_str.startswith("{") or data_str.startswith("["):
            try:
                data = json.loads(data_str)
            except Exception:
                pass

    # 2. CycloneDX JSON
    if isinstance(data, dict) and (data.get("bomFormat") == "CycloneDX" or "components" in data):
        raw_comps = data.get("components") or []
        for c in raw_comps:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name", "")).strip()
            version = str(c.get("version", "")).strip()
            purl = str(c.get("purl", "")).strip()
            eco = _detect_ecosystem_from_purl(purl) or default_eco or "PyPI"
            if not name and purl:
                name, v, e = _parse_purl(purl)
                version = version or v
                eco = eco or e
            if name:
                components.append({"name": name, "version": version, "ecosystem": eco, "purl": purl})
        return components

    # 3. SPDX JSON
    if isinstance(data, dict) and ("spdxVersion" in data or "packages" in data):
        raw_pkgs = data.get("packages") or []
        for p in raw_pkgs:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).strip()
            version = str(p.get("versionInfo", "")).strip()
            purl = ""
            for ref in p.get("externalRefs") or []:
                if isinstance(ref, dict) and ref.get("referenceType") == "purl":
                    purl = str(ref.get("referenceLocator", "")).strip()
                    break
            eco = _detect_ecosystem_from_purl(purl) or default_eco or "PyPI"
            if name:
                components.append({"name": name, "version": version, "ecosystem": eco, "purl": purl})
        return components

    # 4. Lista directa de dicts o strings
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("package") or "").strip()
                version = str(item.get("version") or "").strip()
                purl = str(item.get("purl") or "").strip()
                eco = str(item.get("ecosystem") or _detect_ecosystem_from_purl(purl) or default_eco or "PyPI").strip()
                if name:
                    components.append({"name": name, "version": version, "ecosystem": eco, "purl": purl})
            elif isinstance(item, str) and item.strip():
                line = item.strip()
                m = re.match(r"^([a-zA-Z0-9_\-\.\@\/]+)(?:==|@|>=|<=)([a-zA-Z0-9_\-\.]+)$", line)
                if m:
                    components.append({"name": m.group(1), "version": m.group(2), "ecosystem": default_eco or "PyPI", "purl": ""})
        return components

    # 5. Texto plano (requirements.txt o lista newline-separated)
    if isinstance(data, str):
        for line in data.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^([a-zA-Z0-9_\-\.\@\/]+)(?:==|@|>=|<=)([a-zA-Z0-9_\-\.]+)$", line)
            if m:
                components.append({"name": m.group(1), "version": m.group(2), "ecosystem": default_eco or "PyPI", "purl": ""})

    return components


def _extract_severity_and_fix(vuln: dict) -> tuple[str, str, str]:
    """Extrae (severity, fixed_version, cve_id) de un objeto de vulnerabilidad OSV."""
    vuln_id = vuln.get("id", "")
    aliases = vuln.get("aliases") or []
    cve_id = next((a for a in aliases if a.startswith("CVE-")), "") or vuln_id

    sev_str = "MEDIUM"
    db_spec = vuln.get("database_specific") or {}
    if isinstance(db_spec, dict) and db_spec.get("severity"):
        raw_sev = str(db_spec["severity"]).upper()
        if raw_sev in ("CRITICAL", "HIGH", "MEDIUM", "MODERATE", "LOW"):
            sev_str = "MEDIUM" if raw_sev == "MODERATE" else raw_sev
    elif vuln.get("severity"):
        for s in vuln["severity"]:
            if isinstance(s, dict) and s.get("score"):
                score = str(s["score"])
                if "CVSS:" in score:
                    if "/AV:N/AC:L/PR:N" in score or "/S:C/C:H/I:H/A:H" in score:
                        sev_str = "CRITICAL"
                    elif "/C:H" in score or "/I:H" in score:
                        sev_str = "HIGH"

    fixed_ver = ""
    for aff in vuln.get("affected") or []:
        if isinstance(aff, dict):
            for rng in aff.get("ranges") or []:
                if isinstance(rng, dict):
                    for ev in rng.get("events") or []:
                        if isinstance(ev, dict) and ev.get("fixed"):
                            fixed_ver = str(ev["fixed"])
                            break
            if fixed_ver:
                break

    return sev_str, fixed_ver, cve_id


def cve_audit(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    ecosystem: str = "",
    severity_threshold: str = "LOW",
    max_results: int = 100,
    timeout: float = 25.0,
) -> NodeResult:
    """Audita una lista de dependencias o archivo SBOM (CycloneDX / SPDX) consultando
    la base de datos abierta de vulnerabilidades de OSV.dev (OpenSSF/Google) en batch."""
    inp = Inputs(inputs)
    raw = inp.first_data()
    if raw is None:
        raw = inp.text()

    if not raw:
        return NodeResult("error", error=tr('The CVE audit node requires input data (SBOM or package list)'))

    components = _parse_sbom_components(raw, default_eco=ecosystem)
    if not components:
        return NodeResult("error", error=tr('No valid components or packages found in the input (expected CycloneDX, SPDX, list of dicts or requirements)'))

    queries = []
    for c in components:
        if c.get("purl"):
            queries.append({"package": {"purl": c["purl"]}})
        elif c.get("name") and c.get("version"):
            q: dict[str, Any] = {
                "package": {"name": c["name"]},
                "version": c["version"],
            }
            if c.get("ecosystem"):
                q["package"]["ecosystem"] = c["ecosystem"]
            queries.append(q)
        elif c.get("name"):
            queries.append({"package": {"name": c["name"], "ecosystem": c.get("ecosystem", "PyPI")}})

    if not queries:
        return NodeResult("error", error=tr('Could not generate valid queries from the extracted components'))

    all_vuln_results = []
    osv_url = "https://api.osv.dev/v1/querybatch"

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            chunk_size = 500
            for i in range(0, len(queries), chunk_size):
                batch = queries[i : i + chunk_size]
                resp = client.post(osv_url, json={"queries": batch})
                if resp.status_code >= 400:
                    return NodeResult("error", error=f"Error en OSV.dev API ({resp.status_code}): {resp.text[:200]}")
                batch_res = resp.json().get("results", [])
                all_vuln_results.extend(batch_res)
    except Exception as exc:
        return NodeResult("error", error=tr('Connection error with OSV.dev: {exc}', exc=exc))

    min_level = _SEVERITY_ORDER.get(severity_threshold.upper().strip(), 1)
    if severity_threshold.upper().strip() == "ALL":
        min_level = 0

    vulnerabilities = []
    vulnerable_packages_set = set()
    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    max_sev_found = "NONE"

    for idx, res_item in enumerate(all_vuln_results):
        if idx >= len(components):
            break
        comp = components[idx]
        vulns_found = (res_item or {}).get("vulns", [])
        if not vulns_found:
            continue

        for v in vulns_found:
            sev, fixed_ver, cve_id = _extract_severity_and_fix(v)
            sev_lvl = _SEVERITY_ORDER.get(sev, 2)
            if sev_lvl < min_level:
                continue

            vulnerable_packages_set.add(comp["name"])
            if sev in severity_counts:
                severity_counts[sev] += 1

            if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(max_sev_found, 0):
                max_sev_found = sev

            summary = v.get("summary") or ""
            if not summary and v.get("details"):
                summary = v["details"][:150].strip() + "..."

            refs = [r.get("url") for r in (v.get("references") or []) if isinstance(r, dict) and r.get("url")]

            vulnerabilities.append({
                "package": comp["name"],
                "version": comp.get("version", ""),
                "ecosystem": comp.get("ecosystem", ""),
                "id": v.get("id", ""),
                "cve": cve_id,
                "severity": sev,
                "summary": summary,
                "fixed_version": fixed_ver,
                "references": refs[:3],
            })

            if len(vulnerabilities) >= int(max_results):
                break
        if len(vulnerabilities) >= int(max_results):
            break

    icon_map = {
        "CRITICAL": "🔴",
        "HIGH": "🟠",
        "MEDIUM": "🟡",
        "LOW": "🔵",
        "NONE": "🟢",
    }
    sev_icon = icon_map.get(max_sev_found, "⚪")

    md_lines = [
        tr('## 🛡️ Vulnerability Security Audit (OSV / CVE)'),
        "",
        tr('- **Audited packages**: {components}', components=len(components)),
        f"- **Paquetes vulnerables**: {len(vulnerable_packages_set)}",
        tr('- **Vulnerabilities found**: {vulnerabilities}', vulnerabilities=len(vulnerabilities)),
        tr('- **Maximum severity**: {sev_icon} **{max_sev_found}**', sev_icon=sev_icon, max_sev_found=max_sev_found),
        "",
    ]

    if vulnerabilities:
        md_lines.extend([
            tr('| Package | Version | CVE / ID | Severity | Patched Version | Summary |'),
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for item in vulnerabilities:
            icon = icon_map.get(item["severity"], "⚪")
            cve_link = f"[{item['cve']}](https://osv.dev/vulnerability/{item['id']})" if item["id"] else item["cve"]
            fix = f"`{item['fixed_version']}`" if item["fixed_version"] else "*Pendiente*"
            summ = item["summary"].replace("|", "/")
            md_lines.append(
                f"| `{item['package']}` | `{item['version']}` | {cve_link} | {icon} {item['severity']} | {fix} | {summ} |"
            )
    else:
        md_lines.append(tr('🟢 **No known vulnerabilities were detected for the analyzed components.**'))

    markdown_report = "\n".join(md_lines)

    result_data = {
        "total_packages": len(components),
        "vulnerable_packages": len(vulnerable_packages_set),
        "total_vulnerabilities": len(vulnerabilities),
        "highest_severity": max_sev_found,
        "severity_counts": severity_counts,
        "vulnerabilities": vulnerabilities,
        "markdown": markdown_report,
    }

    return NodeResult("ok", data=result_data)


# ---------------------------------------------------------------------------
# Monitoreo de Dominios y Red: WHOIS/RDAP y DNS (DoH)
# ---------------------------------------------------------------------------


def whois_rdap(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    domain: str = "",
    timeout: float = 15.0,
) -> NodeResult:
    """Consulta información de registro y caducidad de dominio mediante RDAP (Registration Data Access Protocol)."""
    inp = Inputs(inputs)
    target = domain.strip().lower()
    if not target:
        target = inp.text().strip().lower()
    if not target:
        first = inp.first_data()
        if isinstance(first, dict):
            target = str(first.get("domain") or first.get("host") or "").strip().lower()

    if not target:
        return NodeResult("error", error=tr('Missing domain name to query in whois_rdap'))

    if "://" in target:
        target = target.split("://", 1)[1]
    target = target.split("/", 1)[0].split(":", 1)[0].strip()

    rdap_url = f"https://rdap.org/domain/{target}"

    try:
        with httpx.Client(timeout=float(timeout), follow_redirects=True) as client:
            resp = client.get(rdap_url, headers={"Accept": "application/rdap+json, application/json"})
            if resp.status_code == 404:
                return NodeResult("error", error=tr("The domain '{target}' was not found in the RDAP registry (it may not be registered or the TLD may be unsupported)", target=target))
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Error en consulta RDAP ({resp.status_code}): {resp.text[:200]}")
            data = resp.json()
    except Exception as exc:
        return NodeResult("error", error=tr("RDAP connection error for '{target}': {exc}", target=target, exc=exc))

    registrar_name = ""
    for entity in data.get("entities") or []:
        if isinstance(entity, dict):
            roles = entity.get("roles") or []
            if "registrar" in roles:
                vcard = entity.get("vcardArray") or []
                if len(vcard) > 1 and isinstance(vcard[1], list):
                    for prop in vcard[1]:
                        if isinstance(prop, list) and prop and prop[0] == "fn":
                            registrar_name = str(prop[3])
                            break
                if not registrar_name and entity.get("handle"):
                    registrar_name = str(entity["handle"])
                break

    created_date = ""
    updated_date = ""
    expiration_date = ""

    for ev in data.get("events") or []:
        if isinstance(ev, dict):
            act = ev.get("eventAction", "").lower()
            date_str = ev.get("eventDate", "")
            if "registration" in act or "create" in act:
                created_date = date_str
            elif "last changed" in act or "update" in act:
                updated_date = date_str
            elif "expiration" in act:
                expiration_date = date_str

    days_left = None
    is_expired = False
    if expiration_date:
        try:
            clean_date = expiration_date.split(".")[0].rstrip("Z")
            if "T" in clean_date:
                dt_exp = datetime.datetime.fromisoformat(clean_date)
            else:
                dt_exp = datetime.datetime.strptime(clean_date, "%Y-%m-%d")
            delta = dt_exp - datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            days_left = delta.days
            is_expired = days_left < 0
        except Exception:
            pass

    nameservers = []
    for ns in data.get("nameservers") or []:
        if isinstance(ns, dict) and ns.get("ldhName"):
            nameservers.append(str(ns["ldhName"]).lower())

    statuses = data.get("status") or []

    md_report = [
        tr('## 🌐 RDAP Domain Information: `{target}`', target=target),
        f"- **Registrador**: {registrar_name or '*Desconocido*'}",
        tr('- **Expiration date**: {disponible} (Days remaining: **{A}**)', disponible=expiration_date or tr('*Not available*'), A=days_left if days_left is not None else 'N/A'),
        tr('- **Creation date**: {A}', A=created_date or 'N/A'),
        tr('- **Name Servers (NS)**: {A}', A=', '.join(nameservers) if nameservers else 'N/A'),
        tr('- **Status**: {A}', A=', '.join(statuses) if statuses else 'N/A'),
    ]

    out = {
        "domain": target,
        "registrar": registrar_name,
        "created_date": created_date,
        "updated_date": updated_date,
        "expiration_date": expiration_date,
        "days_until_expiration": days_left,
        "is_expired": is_expired,
        "nameservers": nameservers,
        "status": statuses,
        "markdown": "\n".join(md_report),
    }
    return NodeResult("ok", data=out)


def dns_record(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    name: str = "",
    type: str = "A",
    provider: str = "cloudflare",
    timeout: float = 8.0,
) -> NodeResult:
    """Resuelve registros DNS (A, AAAA, MX, TXT para SPF/DMARC, CNAME, NS, SOA) usando DNS-over-HTTPS (DoH)."""
    inp = Inputs(inputs)
    target = name.strip().lower()
    if not target:
        target = inp.text().strip().lower()
    if not target:
        first = inp.first_data()
        if isinstance(first, dict):
            target = str(first.get("domain") or first.get("host") or first.get("name") or "").strip().lower()

    if not target:
        return NodeResult("error", error=tr('Missing hostname or domain to query in dns_record'))

    if "://" in target:
        target = target.split("://", 1)[1]
    target = target.split("/", 1)[0].split(":", 1)[0].strip()

    rec_type = type.strip().upper() or "A"

    if provider.lower() == "google":
        url = f"https://dns.google/resolve?name={target}&type={rec_type}"
        headers = {}
    else:
        url = f"https://cloudflare-dns.com/dns-query?name={target}&type={rec_type}"
        headers = {"Accept": "application/dns-json"}

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Error en DoH ({resp.status_code}): {resp.text[:200]}")
            dns_json = resp.json()
    except Exception as exc:
        return NodeResult("error", error=tr("Error in DNS resolution for '{target}': {exc}", target=target, exc=exc))

    status_code = dns_json.get("Status", 0)
    answers = dns_json.get("Answer") or []
    values = []
    for a in answers:
        if isinstance(a, dict) and "data" in a:
            val = str(a["data"]).strip().strip('"')
            values.append(val)

    out = {
        "name": target,
        "type": rec_type,
        "status_code": status_code,
        "is_nxdomain": status_code == 3,
        "answers": answers,
        "values": values,
        "total": len(values),
    }
    return NodeResult("ok", data=out)


# ---------------------------------------------------------------------------
# Detección de Fugas de Secretos (DLP) y Entropía de Shannon
# ---------------------------------------------------------------------------


def _shannon_entropy(data: str) -> float:
    """Calcula la entropía de Shannon en bits por carácter (0.0 a ~6.0 bits)."""
    if not data:
        return 0.0
    length = len(data)
    counts = Counter(data)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


_SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("GitHub Token", re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,255})\b")),
    ("Slack Token", re.compile(r"\b(xox[baprs]-[0-9a-zA-Z]{10,48})\b")),
    ("Private Key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("JWT Token", re.compile(r"\b(eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b")),
    ("Generic Bearer/Secret", re.compile(r"(?i)(?:api[_-]?key|bearer|secret|token|password|passwd)\s*[:=]\s*['\"]?([A-Za-z0-9_\-.~+]{20,})['\"]?")),
    ("Connection String Password", re.compile(r"://[^:\s]+:([^@\s]{6,})@")),
]


def secret_scanner(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    mode: str = "detect_and_redact",
    min_entropy: float = 4.3,
    check_patterns: bool = True,
    check_entropy: bool = True,
) -> NodeResult:
    """Escanea textos, logs o estructuras en busca de secretos, credenciales expuestas y cadenas de alta entropía."""
    inp = Inputs(inputs)
    raw = inp.first_data()
    raw_text = inp.text()
    if not raw_text and raw is not None:
        raw_text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, (dict, list)) else str(raw)

    if not raw_text:
        return NodeResult("ok", data={"has_secrets": False, "count": 0, "findings": [], "text": ""})

    findings = []
    redacted_text = raw_text
    found_spans: list[tuple[int, int, str, str]] = []

    if check_patterns:
        for secret_name, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(raw_text):
                val = match.group(1) if match.groups() else match.group(0)
                span = match.span(1) if match.groups() else match.span(0)
                found_spans.append((span[0], span[1], secret_name, val))

    if check_entropy:
        word_pattern = re.compile(r"\b([A-Za-z0-9_/\+=\-]{20,})\b")
        for match in word_pattern.finditer(raw_text):
            token = match.group(1)
            if any(s[0] <= match.start() and s[1] >= match.end() for s in found_spans):
                continue
            has_letters = bool(re.search(r"[A-Za-z]", token))
            has_digits = bool(re.search(r"[0-9]", token))
            if has_letters and has_digits:
                ent = _shannon_entropy(token)
                if ent >= float(min_entropy):
                    found_spans.append((match.start(), match.end(), f"High Entropy ({ent:.2f} bits)", token))

    found_spans.sort(key=lambda x: x[0], reverse=True)
    deduped_spans = []
    last_start = float("inf")
    for start, end, stype, sval in found_spans:
        if end <= last_start:
            deduped_spans.append((start, end, stype, sval))
            last_start = start

    for start, end, stype, sval in reversed(deduped_spans):
        line_no = raw_text[:start].count("\n") + 1
        masked_val = (sval[:2] + "***" + sval[-2:]) if len(sval) > 6 else "***"
        findings.append({
            "type": stype,
            "line": line_no,
            "entropy": round(_shannon_entropy(sval), 2),
            "preview": masked_val,
        })

    if mode == "detect_and_redact":
        for start, end, stype, sval in deduped_spans:
            redacted_text = redacted_text[:start] + f"[REDACTED_SECRET: {stype}]" + redacted_text[end:]
    elif mode == "mask":
        for start, end, stype, sval in deduped_spans:
            masked = (sval[:2] + "********" + sval[-2:]) if len(sval) > 6 else "********"
            redacted_text = redacted_text[:start] + masked + redacted_text[end:]

    has_secrets = len(findings) > 0
    return NodeResult("ok", data={
        "has_secrets": has_secrets,
        "count": len(findings),
        "findings": findings,
        "text": redacted_text,
    })


# ---------------------------------------------------------------------------
# Mapeo y Extracción de JSON (json_path_extract & data_mapper)
# ---------------------------------------------------------------------------


def _resolve_json_path(data: Any, path: str, default: Any = None) -> Any:
    """Navega por estructuras anidadas (dict/list) con soporte para notación punto, índices [0] y comodín [*]."""
    if not path or path.strip() in ("", "$"):
        return data

    clean_path = path.strip().lstrip("$.")
    tokens: list[Any] = []
    parts = clean_path.split(".")
    for part in parts:
        subparts = re.split(r"\[|\]", part)
        for sp in subparts:
            sp = sp.strip()
            if not sp:
                continue
            if sp.isdigit():
                tokens.append(int(sp))
            else:
                tokens.append(sp)

    current: Any = data
    for i, token in enumerate(tokens):
        if current is None:
            return default

        if token == "*":
            remaining_tokens = tokens[i + 1 :]
            if not remaining_tokens:
                return current if isinstance(current, list) else list(current.values()) if isinstance(current, dict) else [current]

            sub_path = ".".join(str(t) for t in remaining_tokens)
            results = []
            iterable = current if isinstance(current, list) else current.values() if isinstance(current, dict) else []
            for item in iterable:
                res = _resolve_json_path(item, sub_path, default=None)
                if res is not None:
                    if isinstance(res, list):
                        results.extend(res)
                    else:
                        results.append(res)
            return results

        if isinstance(current, dict):
            if token in current:
                current = current[token]
            elif str(token) in current:
                current = current[str(token)]
            else:
                return default
        elif isinstance(current, list):
            if isinstance(token, int) and 0 <= token < len(current):
                current = current[token]
            else:
                sub_path = ".".join(str(t) for t in tokens[i:])
                return [_resolve_json_path(item, sub_path, default=default) for item in current]
        else:
            return default

    return current


def json_path_extract(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str,
    default: Any = "",
    flatten: bool = True,
) -> NodeResult:
    """Extrae valores específicos de un JSON o estructura anidada usando sintaxis de ruta."""
    inp = Inputs(inputs)
    data = inp.first_data()
    if data is None:
        txt = inp.text()
        if txt:
            try:
                data = json.loads(txt)
            except Exception:
                data = txt

    if data is None:
        return NodeResult("error", error=tr('Missing input data for json_path_extract'))

    result = _resolve_json_path(data, path, default=default)
    if flatten and isinstance(result, list):
        flat = []
        for item in result:
            if isinstance(item, list):
                flat.extend(item)
            else:
                flat.append(item)
        result = flat

    return NodeResult("ok", data=result)


def data_mapper(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    mapping: Any,
    per_item: bool = True,
) -> NodeResult:
    """Proyecta y reestructura diccionarios o listas de diccionarios según una plantilla declarativa."""
    inp = Inputs(inputs)
    data = inp.first_data()
    if data is None:
        txt = inp.text()
        if txt:
            try:
                data = json.loads(txt)
            except Exception:
                pass

    if data is None:
        return NodeResult("error", error=tr('Missing input data for data_mapper'))

    spec = mapping
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except Exception:
            spec = {}
            for line in mapping.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    spec[k.strip()] = v.strip()

    if not isinstance(spec, dict):
        return NodeResult("error", error=tr("The 'mapping' parameter must be a dictionary or valid JSON (e.g. {'nombre': 'metadata.name'})"))

    def _map_single_obj(obj: Any) -> dict[str, Any]:
        mapped = {}
        for target_key, src_path in spec.items():
            mapped[target_key] = _resolve_json_path(obj, str(src_path), default=None)
        return mapped

    if per_item and isinstance(data, list):
        out = [_map_single_obj(item) for item in data if isinstance(item, (dict, list))]
    elif isinstance(data, dict):
        out = _map_single_obj(data)
    else:
        out = _map_single_obj(data)

    return NodeResult("ok", data=out)


# ---------------------------------------------------------------------------
# Notificaciones Microsoft Teams (MessageCard / Adaptive Cards)
# ---------------------------------------------------------------------------


def teams_webhook(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    webhook_url: str = "",
    secret: str = "",
    title: str = "",
    text: str = "",
    theme_color: str = "0076D7",
    summary: str = "",
    button_text: str = "",
    button_url: str = "",
    timeout: float = 15.0,
) -> NodeResult:
    """Envía tarjetas enriquecidas (MessageCard) a Microsoft Teams vía Incoming Webhook."""
    target_url = webhook_url.strip()
    if not target_url and secret:
        target_url = secrets.get(secret).strip()

    if not target_url:
        return NodeResult("error", error=tr('Missing Microsoft Teams Webhook URL (or associated secret)'))

    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    final_text = text.strip()
    if not final_text:
        if isinstance(first, dict):
            if first.get("markdown"):
                final_text = str(first["markdown"])
            elif first.get("text"):
                final_text = str(first["text"])
            else:
                final_text = json.dumps(first, ensure_ascii=False, indent=2)
        elif raw_text:
            final_text = raw_text
        else:
            final_text = tr('Dédalo notification.')

    card_title = title.strip() or (first.get("title") if isinstance(first, dict) else "") or tr('Dédalo notice')
    card_summary = summary.strip() or card_title

    payload: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "summary": card_summary,
        "themeColor": theme_color.lstrip("#") or "0076D7",
        "title": card_title,
        "text": final_text,
    }

    b_text = button_text.strip()
    b_url = button_url.strip()
    if b_text and b_url:
        payload["potentialAction"] = [
            {
                "@type": "OpenUri",
                "name": b_text,
                "targets": [{"os": "default", "uri": b_url}],
            }
        ]

    try:
        with httpx.Client(timeout=float(timeout)) as client:
            resp = client.post(target_url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Error en Microsoft Teams Webhook ({resp.status_code}): {resp.text[:200]}")
    except Exception as exc:
        return NodeResult("error", error=tr('Connection error with Microsoft Teams: {exc}', exc=exc))

    return NodeResult("ok", data={"sent": True, "title": card_title, "status_code": resp.status_code})


# ---------------------------------------------------------------------------
# Control de Flujo: Aprobación Humana (Approval Gate)
# ---------------------------------------------------------------------------


def human_approval(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt_message: str = 'Do you want to authorize the execution of this step?',
    on_reject: str = "skip",
    auto_approve: bool = False,
) -> NodeResult:
    """Paso de aprobación / confirmación humana (Approval Gate).
    En terminal solicita confirmación interactiva al usuario.
    Admite los flags --yes / --non-interactive o DEDALO_AUTO_APPROVE=1 para saltarse la espera.
    Si se aprueba, emite los datos recibidos intactos. Si se rechaza, emite SKIPPED o error."""
    inp = Inputs(inputs)
    first = inp.first_data()
    raw_text = inp.text()

    # 1. Comprobar si se ha habilitado auto-aprobación
    env_auto = os.environ.get("DEDALO_AUTO_APPROVE", "").lower() in ("1", "true", "yes", "y")
    ctx_auto = bool(getattr(ctx, "auto_approve", False))
    if auto_approve or env_auto or ctx_auto:
        return NodeResult("ok", data=first if first is not None else (raw_text or tr('Automatically approved')))

    # 2. Comprobar si la sesión es interactiva (TTY en stdin)
    is_interactive = sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    if is_interactive:
        preview = ""
        if first is not None:
            preview = json.dumps(first, ensure_ascii=False) if isinstance(first, (dict, list)) else str(first)
            if len(preview) > 200:
                preview = preview[:200] + "..."
        elif raw_text:
            preview = raw_text[:200] + ("..." if len(raw_text) > 200 else "")

        border = "=" * 60
        print(tr('\n{border}\n⚠️  HUMAN APPROVAL REQUEST (Gate)\n{prompt_message}', border=border, prompt_message=prompt_message))
        if preview:
            print(tr('📦 Data to process: {preview}', preview=preview))
        print(f"{border}")

        try:
            ans = input(tr('👉 Do you authorize the execution? [y/N]: ')).strip().lower()
            confirmed = ans in ("s", "si", "y", "yes")
        except (EOFError, KeyboardInterrupt):
            confirmed = False
            print(tr('\n⛔ Cancelled by the user.'))

        if confirmed:
            return NodeResult("ok", data=first if first is not None else (raw_text or "Aprobado"))

    # Si no se confirmó o no era interactivo
    if on_reject.lower() == "error":
        msg = tr('Execution cancelled by the user at the approval step') if is_interactive else tr('The approval step requires an interactive terminal or the --yes / --non-interactive flag')
        return NodeResult("error", error=msg)

    return SKIPPED





