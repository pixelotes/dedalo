"""Funciones de ejecución para los nodos de Dédalo.

Cada función sigue la signatura: `(ctx, inputs, **config) -> NodeResult`.
Los scripts compilados invocan directamente estas funciones sin sobrecarga de runtime.
"""

from __future__ import annotations

import csv
import ctypes
import datetime
import difflib
import email
import email.header
import gc
import hashlib
import html
import imaplib
import json
import math
import os
import random
import re
import socket
import ssl
import statistics
from collections import Counter, defaultdict
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable

import httpx

from dedalo import llms, secrets


def compact_memory() -> None:
    """Fuerza la recolección de basura de Python y solicita a glibc liberar heap al SO."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

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
            raise KeyError(f"No hay entrada conectada con el id «{key}»")
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
                    f"LLM API devolvió HTTP {resp.status_code}: {resp.text[:200]}"
                )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise RuntimeError(f"Error al conectar con el LLM en {endpoint}: {exc}") from exc


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
            return NodeResult("error", error="El motor 'curl_cffi' no está instalado en este entorno.")
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
        return NodeResult("error", error=f"No se pudo descargar el feed: {fetch_res.error}")

    xml_text = fetch_res.data if isinstance(fetch_res.data, str) else json.dumps(fetch_res.data)

    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        return NodeResult("error", error=f"XML RSS inválido: {exc}")

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
        return NodeResult("error", error=f"Fichero no encontrado: {path}")
    try:
        delim = delimiter.replace("\\t", "\t") if delimiter else ","
        with p.open(encoding=encoding or "utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter=delim)
            return NodeResult("ok", data=list(reader))
    except Exception as exc:
        return NodeResult("error", error=f"Error al leer CSV: {exc}")


def webhook_trigger(ctx: Any, inputs: list[NodeResult], **config) -> NodeResult:
    """Recibe datos desde un webhook entrante HTTP."""
    payload = getattr(ctx, "payload", None)
    if payload is None:
        return NodeResult("ok", data={"event": "manual_preview", "message": "Disparado manualmente sin payload"})
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
        return NodeResult("error", error="Falta la consulta SQL")
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=f"Base de datos SQLite no encontrada: {path}")
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
) -> NodeResult:
    """Ejecuta un comando en la shell del sistema y captura su salida."""
    if not cmd.strip():
        return NodeResult("error", error="Falta el comando a ejecutar")
    inp = Inputs(inputs)
    rendered_cmd = cmd.replace("{entrada}", inp.text())
    work_dir = Path(cwd) if cwd.strip() else None
    try:
        res = subprocess.run(
            rendered_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            cwd=work_dir,
        )
        data = {
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip(),
            "exit_code": res.returncode,
        }
        if res.returncode != 0:
            return NodeResult("error", data=data, error=f"Comando devolvió código {res.returncode}: {res.stderr[:200]}")
        return NodeResult("ok", data=data)
    except subprocess.TimeoutExpired:
        return NodeResult("error", error=f"Tiempo de ejecución agotado ({timeout}s) para el comando")
    except Exception as exc:
        return NodeResult("error", error=f"Error al ejecutar comando: {exc}")


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
        return NodeResult("error", error=f"El directorio no existe: {path}")
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
        return NodeResult("error", error=f"Error escaneando directorio: {exc}")


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
        return NodeResult("error", error="Falta la URL para verificar salud")
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
            err = "" if is_healthy else f"Estado HTTP {resp.status_code} != esperado {expected_status}"
            return NodeResult(status, data=data, error=err)
    except Exception as exc:
        latency_ms = round((time.time() - t0) * 1000, 1)
        return NodeResult(
            "error",
            data={"url": url, "status_code": 0, "latency_ms": latency_ms, "is_healthy": False},
            error=f"Fallo de conexión en health check: {exc}",
        )


def json_file(ctx: Any, inputs: list[NodeResult], *, path: str) -> NodeResult:
    """Lee un fichero JSON local."""
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=f"Fichero no encontrado: {path}")
    try:
        return NodeResult("ok", data=json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        return NodeResult("error", error=f"Error al leer JSON: {exc}")


def disk_free(ctx: Any, inputs: list[NodeResult], *, path: str = "") -> NodeResult:
    """Devuelve el porcentaje de espacio libre en disco (float)."""
    target = path.strip() or os.path.expanduser("~")
    try:
        total, used, free = shutil.disk_usage(target)
        pct = (free / total) * 100.0 if total > 0 else 0.0
        return NodeResult("ok", data=round(pct, 2))
    except Exception as exc:
        return NodeResult("error", error=f"Error al medir disco: {exc}")


def cpu_load(ctx: Any, inputs: list[NodeResult], *, seconds: float = 1.0) -> NodeResult:
    """Devuelve el uso de CPU aproximado."""
    try:
        # Medición simple mediante lectura de carga del sistema
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        pct = min(100.0, (load1 / cpu_count) * 100.0)
        return NodeResult("ok", data=round(pct, 2))
    except Exception as exc:
        return NodeResult("error", error=f"Error al medir CPU: {exc}")


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
        return NodeResult("error", error=f"El valor de entrada o límite no es numérico: {val!r}, {limit!r}")

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
        return NodeResult("error", error="La entrada para filtrar debe ser una lista")

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
        return NodeResult("error", error=f"Error al procesar HTML: {exc}")

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
        return NodeResult("error", error="La entrada de array_ops debe ser una lista")
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
        return NodeResult("error", error="Falta el patrón de expresión regular")
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
        return NodeResult("error", error=f"Acción regex desconocida: {action}")
    except Exception as exc:
        return NodeResult("error", error=f"Error al evaluar regex: {exc}")


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
        return NodeResult("error", data=data, error=f"Condición de router no cumplida ({condition})")


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
            return f"[Contenido compactado con Luhn+ por límite de contexto ({current_content_tokens} -> {int(len(compacted) / 3.5)} tokens)]:\n{compacted}"
        return content_text

    if per_item:
        first = inp.first_data()
        if not isinstance(first, list):
            return NodeResult("error", error="El modo per_item requiere una lista de entrada")
        results = []
        field_name = as_field.strip()
        for item in first[:MAX_ITEMS]:
            item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
            if effective_ctx and int(effective_ctx) > 0:
                item_str = _compact_if_needed(item_str)
            user_msg = f"{prompt}\n\nElemento:\n{item_str}"
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
    user_msg = f"{prompt}\n\nEntrada:\n{input_text}" if input_text else prompt
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


def _compact_conversation_history(
    messages: list[dict[str, Any]],
    context_window: int,
    preserve_recent_turns: int = 1,
) -> tuple[list[dict[str, Any]], bool]:
    """Si los mensajes superan el 90% del contexto soportado, compacta el historial previo con Luhn+."""
    threshold = int(context_window * 0.90)
    current_tokens = _estimate_messages_tokens(messages)
    if current_tokens < threshold:
        return messages, False

    did_compact = False
    new_msgs = list(messages)

    # 1. Compactar mensajes de herramientas que individualmente superen el umbral o sean desproporcionados
    for idx, m in enumerate(new_msgs):
        if m.get("role") == "tool" and m.get("content"):
            c_text = str(m["content"])
            # Si el contenido de la tool supera el 40% del umbral de contexto
            if len(c_text) > int(threshold * 1.5):
                summary = _run_luhn_plus(c_text, target_count=8, output_format="bullets")
                compacted_m = dict(m)
                compacted_m["content"] = f"[Salida de herramienta compactada con Luhn+ (superado 90% contexto)]:\n{summary}"
                new_msgs[idx] = compacted_m
                did_compact = True

    # Si tras compactar herramientas ya bajamos del 90%, retornamos
    if did_compact and _estimate_messages_tokens(new_msgs) < threshold:
        return new_msgs, True

    # 2. Compactar turnos intermedios si el historial se ha acumulado
    if len(new_msgs) > 4:
        head = new_msgs[:2]  # system + user prompt original
        tail_len = min(len(new_msgs) - 2, max(2, preserve_recent_turns * 2))
        tail = new_msgs[-tail_len:]
        middle = new_msgs[2:-tail_len]
        if middle:
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
                    chunks.append(f"Herramienta {name or 'tool'}:\n{content}")
                elif role == "user":
                    chunks.append(f"Usuario: {content}")

            raw_middle = "\n\n".join(chunks)
            summary_bullets = _run_luhn_plus(raw_middle, target_count=8, output_format="bullets")

            summary_msg = {
                "role": "user",
                "content": (
                    f"[Memoria resumida del progreso previo mediante Luhn+ (alcanzado 90% de contexto: {current_tokens}/{context_window} tokens)]:\n"
                    f"{summary_bullets}"
                ),
            }
            ack_msg = {
                "role": "assistant",
                "content": "Comprendido. Mantengo la memoria compactada de las acciones previas y continúo.",
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

ALL_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write", "grep", "find", "ls")
CODING_TOOL_NAMES: tuple[str, ...] = ("read", "bash", "edit", "write")
READ_ONLY_TOOL_NAMES: tuple[str, ...] = ("read", "grep", "find", "ls")


def _resolve_safe_path(base_dir: Path, rel_path: str) -> Path:
    """Resuelve una ruta y garantiza que no escape del directorio base (anti path-traversal)."""
    clean_rel = rel_path.strip() if rel_path else "."
    target = (base_dir / clean_rel).resolve()
    if target != base_dir and not target.is_relative_to(base_dir):
        raise PermissionError(f"Ruta fuera del espacio de trabajo permitido: '{rel_path}'")
    return target


def _clip_tool_output(text: str, max_chars: int = 4000) -> str:
    """Trunca el texto de salida preservando la cabecera y el final del mensaje."""
    s = str(text)
    if len(s) <= max_chars:
        return s
    head_len = (max_chars - 60) // 2
    tail_len = max_chars - 60 - head_len
    return f"{s[:head_len]}\n... [truncado {len(s) - max_chars} caracteres] ...\n{s[-tail_len:]}"


def _tool_read(base_dir: Path, path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Lee el contenido de un archivo dentro de base_dir con numeración de líneas opcional."""
    if not path or not path.strip():
        return "Error: Debes especificar el parámetro 'path'."
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    if not target.exists():
        return f"Error: El archivo '{path}' no existe."
    if target.is_dir():
        return f"Error: '{path}' es un directorio. Utiliza la herramienta 'ls' para ver su contenido."
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error al leer '{path}': {exc}"

    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return f"Archivo '{path}' (vacío, 0 líneas)."

    start = max(1, int(start_line or 1))
    end = min(total_lines, int(end_line or total_lines)) if end_line is not None else total_lines
    if start > total_lines:
        return f"Archivo con {total_lines} líneas. Rango solicitado ({start}-{end}) fuera de límites."
    selected = lines[start - 1 : end]
    numbered = [f"{i:4d} | {line}" for i, line in enumerate(selected, start=start)]
    header = f"Archivo: {path} (líneas {start}-{end} de {total_lines})"
    return header + "\n" + "\n".join(numbered)


def _tool_bash(base_dir: Path, command: str, timeout: float = 30.0) -> str:
    """Ejecuta un comando en la consola dentro de base_dir y retorna la salida combinada."""
    if not command or not command.strip():
        return "Error: Comando vacío."
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        parts = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            parts.append(f"[STDERR]\n{proc.stderr}")
        parts.append(f"[Código de salida: {proc.returncode}]")
        return "\n".join(parts)
    except subprocess.TimeoutExpired:
        return f"Error: El comando superó el tiempo límite de {timeout} segundos."
    except Exception as exc:
        return f"Error al ejecutar comando: {exc}"


def _tool_edit(base_dir: Path, path: str, old_str: str, new_str: str) -> str:
    """Edita un archivo reemplazando una única ocurrencia exacta de old_str por new_str."""
    if not path or not path.strip():
        return "Error: Debes especificar el parámetro 'path'."
    if not old_str:
        return "Error: 'old_str' no puede estar vacío."
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    if not target.exists():
        return f"Error: El archivo '{path}' no existe."
    if target.is_dir():
        return f"Error: '{path}' es un directorio."
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"Error al leer '{path}': {exc}"

    count = content.count(old_str)
    if count == 0:
        return f"Error: No se encontró 'old_str' en '{path}'. Verifica caracteres, saltos de línea e indentación."
    if count > 1:
        return f"Error: 'old_str' aparece {count} veces en '{path}'. Debe ser único para editarlo con seguridad. Añade más contexto circundante."

    updated = content.replace(old_str, new_str, 1)
    try:
        target.write_text(updated, encoding="utf-8")
        return f"Éxito: Archivo '{path}' editado correctamente."
    except Exception as exc:
        return f"Error al guardar cambios en '{path}': {exc}"


def _tool_write(base_dir: Path, path: str, content: str) -> str:
    """Crea o sobreescribe un archivo en base_dir."""
    if not path or not path.strip():
        return "Error: Debes especificar el parámetro 'path'."
    try:
        target = _resolve_safe_path(base_dir, path.strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Éxito: Archivo '{path}' guardado ({len(content)} caracteres)."
    except Exception as exc:
        return f"Error al escribir en '{path}': {exc}"


def _tool_grep(base_dir: Path, pattern: str, path: str = ".", case_insensitive: bool = False) -> str:
    """Busca un patrón de texto o expresión regular dentro de base_dir omitiendo carpetas binarias o caches."""
    if not pattern or not pattern.strip():
        return "Error: Debes especificar el parámetro 'pattern'."
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    if not target.exists():
        return f"Error: La ruta '{path}' no existe."

    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return f"Error en sintaxis de expresión regular '{pattern}': {exc}"

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
                        return "\n".join(matches) + f"\n... (límite de {max_matches} coincidencias alcanzado)"
        except Exception:
            continue

    if not matches:
        return f"Sin coincidencias para '{pattern}' en '{path}'."
    return "\n".join(matches)


def _tool_find(base_dir: Path, pattern: str = "*", path: str = ".") -> str:
    """Busca archivos o carpetas por patrón glob en base_dir."""
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    if not target.exists():
        return f"Error: La ruta '{path}' no existe."
    if not target.is_dir():
        return f"Error: '{path}' no es un directorio."

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
                return "\n".join(matches) + f"\n... (límite de {max_results} elementos alcanzado)"

    if not matches:
        return f"No se encontraron archivos con el patrón '{pat}' en '{path}'."
    return "\n".join(matches)


def _tool_ls(base_dir: Path, path: str = ".") -> str:
    """Lista el contenido de un directorio en base_dir con tamaños."""
    try:
        target = _resolve_safe_path(base_dir, (path or ".").strip())
    except PermissionError as pe:
        return f"Error de seguridad: {pe}"
    if not target.exists():
        return f"Error: La ruta '{path}' no existe."
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
                entries.append("... (más elementos truncados)")
                break
    except Exception as exc:
        return f"Error al listar '{path}': {exc}"

    return "\n".join(entries) if entries else "(directorio vacío)"


CODER_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "read": {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Lee el contenido de un archivo dentro del espacio de trabajo con numeración de líneas opcional.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa del archivo a leer.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Línea inicial a leer (1-indexed, opcional).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Línea final a leer (1-indexed, opcional).",
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
            "description": "Ejecuta un comando en la consola dentro del espacio de trabajo y devuelve la salida combinada y código de salida.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Comando de shell a ejecutar (ej. 'pytest tests/', 'ruff check .').",
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
            "description": "Edita un archivo reemplazando una ocurrencia exacta y única de 'old_str' por 'new_str'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa del archivo a editar.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Cadena de texto exacta existente que se desea reemplazar.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Nueva cadena de texto que sustituirá a 'old_str'.",
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
            "description": "Crea o sobreescribe un archivo completo con el contenido indicado en el espacio de trabajo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa del archivo a escribir.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenido completo del archivo a guardar.",
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
            "description": "Busca un patrón de texto o expresión regular en archivos del proyecto, omitiendo carpetas binarias o de dependencias.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Patrón o expresión regular a buscar.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa donde buscar (por defecto '.').",
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "description": "Indica si la búsqueda debe ignorar mayúsculas y minúsculas (por defecto False).",
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
            "description": "Busca archivos y directorios que coincidan con un patrón glob en el espacio de trabajo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Patrón glob (ej. '*.py', '*test*'). Por defecto '*'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa inicial de búsqueda (por defecto '.').",
                    },
                },
            },
        },
    },
    "ls": {
        "type": "function",
        "function": {
            "name": "ls",
            "description": "Lista el contenido de un directorio en el espacio de trabajo indicando si es archivo o carpeta y su tamaño.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Ruta relativa a listar (por defecto '.').",
                    },
                },
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
    output_format: str = "full",
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float | None = None,
    system_prompt: str = "",
    timeout: float = 120.0,
) -> NodeResult:
    """Microarnés de agente programador con bucle ReAct multi-turno, detección de contexto y compactación con Luhn+."""
    inp = Inputs(inputs)
    input_text = inp.text()
    user_msg = f"{prompt}\n\nContexto / Entrada previa:\n{input_text}" if input_text else prompt

    base_path = Path(workspace_dir or ".").expanduser().resolve()
    if not base_path.exists():
        return NodeResult("error", error=f"El directorio de trabajo no existe: '{workspace_dir}'")

    preset = (tools_preset or "coding").lower().strip()
    if preset == "all":
        active_tool_names = list(ALL_TOOL_NAMES)
    elif preset == "read_only":
        active_tool_names = list(READ_ONLY_TOOL_NAMES)
    elif preset == "custom":
        custom_list = [t.strip().lower() for t in custom_tools.split(",") if t.strip()]
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
        "Eres un agente de programación autónomo e inspección de código. "
        "Tienes acceso a herramientas para explorar, leer, editar y ejecutar pruebas en el proyecto. "
        "Sé riguroso, objetivo y conciso. Cuando completes la tarea solicitada, resume tus conclusiones o modificaciones."
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
    }

    endpoint = f"{base_url}/chat/completions"
    turns = 0
    modified_files: set[str] = set()
    tool_calls_log: list[dict[str, Any]] = []
    final_response = ""
    completed = False
    turn_limit = max(1, min(int(max_turns or 5), 20))

    try:
        with httpx.Client(timeout=float(timeout or 120.0)) as client:
            for _ in range(turn_limit):
                turns += 1

                # Comprobación de ventana de contexto: compactar con Luhn+ si se alcanza el 90%
                messages, did_compact = _compact_conversation_history(messages, context_window)
                if did_compact:
                    compactions_count += 1
                    tool_calls_log.append({
                        "turn": turns,
                        "tool": "luhn+_compact",
                        "args": {"context_window": context_window, "threshold": int(context_window * 0.90)},
                        "output_preview": f"Contexto compactado al 90% con Luhn+ ({_estimate_messages_tokens(messages)} tokens estimados)",
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
                    return NodeResult("error", error=f"LLM API devolvió HTTP {resp.status_code}: {resp.text[:300]}")

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
                        result_str = f"Error: La herramienta '{fn_name}' no está disponible o no está autorizada en este nodo."
                    else:
                        try:
                            result_str = tool_handlers[fn_name](args)
                            if fn_name in ("write", "edit") and "path" in args:
                                modified_files.add(str(args["path"]))
                        except Exception as err:
                            result_str = f"Error al ejecutar '{fn_name}': {err}"

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

            if not completed:
                # Comprobar compactación antes del resumen final
                messages, _ = _compact_conversation_history(messages, context_window)
                messages.append({
                    "role": "user",
                    "content": "Se ha alcanzado el límite de turnos. Resume brevemente las acciones realizadas y el estado actual.",
                })
                try:
                    final_resp = client.post(
                        endpoint,
                        headers=headers,
                        json={
                            "model": target_model,
                            "messages": messages,
                            "temperature": float(temperature),
                        },
                    )
                    if final_resp.status_code == 200:
                        final_data = final_resp.json()
                        final_response = (final_data["choices"][0]["message"].get("content") or "").strip()
                except Exception:
                    final_response = (messages[-2].get("content") or "Límite de turnos alcanzado.").strip()

    except Exception as exc:
        return NodeResult("error", error=f"Error en bucle del agente programador: {exc}")

    res_data = {
        "response": final_response,
        "turns_used": turns,
        "completed": completed,
        "context_window": context_window,
        "compactions_count": compactions_count,
        "modified_files": sorted(list(modified_files)),
        "tool_calls_count": len([c for c in tool_calls_log if c["tool"] != "luhn+_compact"]),
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
        return NodeResult("error", error="Se requiere un término de búsqueda en 'query'.")

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
                return NodeResult("error", error=f"Fallo en búsqueda DuckDuckGo: {exc}")

        if not resp_text:
            return NodeResult("error", error="No se pudo obtener respuesta de DuckDuckGo.")

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
            return NodeResult("error", error="El motor 'tavily' requiere configurar un secreto con la API Key.")

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
            return NodeResult("error", error=f"Fallo en búsqueda Tavily: {exc}")

    elif eng in ("exa", "metaphor"):
        token = secrets.get(secret) if secret else ""
        if not token:
            return NodeResult("error", error="El motor 'exa' requiere configurar un secreto con la API Key.")

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
            return NodeResult("error", error=f"Fallo en búsqueda Exa: {exc}")

    else:
        return NodeResult("error", error=f"Motor de búsqueda desconocido: '{engine}'. Use 'duckduckgo', 'tavily' o 'exa'.")

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
        "Resume el siguiente texto manteniendo los puntos clave en una lista de viñetas."
        if is_bullet
        else "Resume el siguiente texto manteniendo los puntos clave."
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
        return NodeResult("error", error=f"Fallo en resumen con LLM: {exc}")


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
        return NodeResult("error", error=f"Error en código Python del nodo: {exc}")


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
        return NodeResult("error", error=f"Error al escribir fichero: {exc}")


def sqlite_exec(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    path: str = "data/store.db",
    query: str = "",
) -> NodeResult:
    """Ejecuta sentencias INSERT, UPDATE o DELETE en SQLite local."""
    if not query.strip():
        return NodeResult("error", error="Falta la sentencia SQL")
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
        return NodeResult("error", error=f"Error en ejecución SQLite: {exc}")


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
    subject: str = "Aviso de Dédalo",
    use_tls: bool = True,
    is_html: bool = False,
) -> NodeResult:
    """Envía un correo electrónico mediante SMTP."""
    inp = Inputs(inputs)
    body = inp.text()
    if not to_addr.strip():
        return NodeResult("error", error="Falta la dirección del destinatario (to_addr)")
    password = secrets.get(secret) if secret else ""
    sender = from_addr.strip() or user.strip()

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = to_addr.strip()
    msg["Subject"] = subject
    subtype = "html" if is_html else "plain"
    msg.attach(MIMEText(body, subtype, "utf-8"))

    try:
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
        return NodeResult("ok", data=f"Email enviado a {to_addr}")
    except Exception as exc:
        return NodeResult("error", error=f"Error al enviar correo SMTP: {exc}")


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
        return NodeResult("error", error="Falta el topic de ntfy")
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
                return NodeResult("error", error=f"ntfy falló con HTTP {resp.status_code}: {resp.text[:100]}")
            return NodeResult("ok", data=f"Notificación ntfy enviada a {topic}")
    except Exception as exc:
        return NodeResult("error", error=f"Error enviando ntfy: {exc}")


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
        return NodeResult("error", error="Falta la URL del webhook de Discord")
    inp = Inputs(inputs)
    msg_content = content.replace("{entrada}", inp.text()) if content else inp.text()
    payload: dict[str, Any] = {"username": username or "Dédalo"}
    if use_embed:
        payload["embeds"] = [{
            "title": title or "Aviso de Dédalo",
            "description": msg_content[:4000],
            "color": 3447003,
        }]
    else:
        payload["content"] = (f"**{title}**\n" if title else "") + msg_content[:2000]
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Discord webhook falló: {resp.status_code}")
            return NodeResult("ok", data="Mensaje enviado a Discord con éxito")
    except Exception as exc:
        return NodeResult("error", error=f"Error enviando a Discord: {exc}")


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
        return NodeResult("error", error="Falta la URL del webhook de Slack")
    inp = Inputs(inputs)
    msg_text = text.replace("{entrada}", inp.text()) if text else inp.text()
    payload: dict[str, Any] = {"text": msg_text}
    if channel.strip():
        payload["channel"] = channel.strip()
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"Slack webhook falló: {resp.status_code}")
            return NodeResult("ok", data="Mensaje enviado a Slack con éxito")
    except Exception as exc:
        return NodeResult("error", error=f"Error enviando a Slack: {exc}")


def command_action(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    cmd: str,
    timeout: float = 15.0,
    cwd: str = "",
) -> NodeResult:
    """Ejecuta un comando de sistema como acción de salida."""
    return command_input(ctx, inputs, cmd=cmd, timeout=timeout, cwd=cwd)


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
                return NodeResult("error", error=f"Webhook falló con HTTP {resp.status_code}")
            return NodeResult("ok", data=f"Webhook enviado (HTTP {resp.status_code})")
    except Exception as exc:
        return NodeResult("error", error=f"Error al enviar webhook: {exc}")


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
        return NodeResult("ok", data="Sin contenido para enviar")

    token = secrets.get(token_secret) or os.environ.get("DEDALO_TELEGRAM_TOKEN")
    dest_chat = chat_id or os.environ.get("DEDALO_TELEGRAM_CHAT_ID")

    if not token or not dest_chat:
        return NodeResult("error", error="Falta configurar el token del bot de Telegram o el chat_id")

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
                return NodeResult("error", error=f"Telegram API falló: {resp.text[:150]}")
            return NodeResult("ok", data="Mensaje de Telegram enviado con éxito")
    except Exception as exc:
        return NodeResult("error", error=f"Error al enviar Telegram: {exc}")


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
        return NodeResult("error", error="La entrada debe ser una lista de elementos.")

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
            return NodeResult("error", error="format_table requiere una lista de diccionarios.")

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
        return NodeResult("error", error="Se requiere un nombre de host para verificar el certificado SSL.")

    target_port = int(port or 443)
    try:
        ctx_ssl = ssl.create_default_context()
        with socket.create_connection((target, target_port), timeout=float(timeout)) as sock:
            with ctx_ssl.wrap_socket(sock, server_hostname=target) as ssock:
                cert = ssock.getpeercert()

        if not cert:
            return NodeResult("error", error=f"No se pudo obtener el certificado de {target}")

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
        return NodeResult("error", error=f"Fallo al comprobar certificado SSL de {target}: {exc}")


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
        return NodeResult("error", error="Se requiere un nombre de host para comprobar DNS.")

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
        return NodeResult("error", error=f"Fallo en resolución DNS de {target}: {exc}")


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
        return NodeResult("error", error="Se requiere un host para realizar ping.")

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
                return NodeResult("error", error=f"Host {target} inalcanzable por ICMP ping.")
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
        return NodeResult("error", error=f"No se pudo conectar a {target}:{port} ({exc})")


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
        return NodeResult("error", error="Se requiere un 'topic' MQTT.")

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
                return NodeResult("error", error=f"Fallo de conexión MQTT (CONNACK error code {err_code})")
            sock.sendall(publish_packet)
            sock.sendall(disconnect_packet)

        return NodeResult("ok", data=f"Publicado en {t}: {msg[:100]}")
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
        return NodeResult("error", error="Se requiere un 'topic' MQTT para lectura.")

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
                return NodeResult("error", error="Fallo al conectar con el broker MQTT.")
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
        return NodeResult("error", error="Se requiere 'host' y 'username' para la conexión IMAP.")

    pwd = secrets.get(secret) if secret else ""
    if not pwd:
        return NodeResult("error", error="Se requiere un secreto con la contraseña o App Password para IMAP.")

    max_items = max(1, int(limit or 5))
    try:
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
    return NodeResult("ok", data=text_val or f"Espera completada ({wait_time:.1f}s)")


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
        return NodeResult("error", error="Falta la URL del sitemap.")
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
        return NodeResult("error", error="Falta la URL de monitorización (healthcheck).")

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
                "message": f"Healthcheck ping enviado a {final_url} (HTTP {resp.status_code})",
            },
        )
    except Exception as exc:
        return NodeResult("error", error=f"Error enviando ping de healthcheck: {exc}")


WMO_WEATHER_CODES: dict[int, tuple[str, str]] = {
    0: ("Despejado", "☀️"),
    1: ("Mayormente despejado", "🌤️"),
    2: ("Parcialmente nublado", "⛅"),
    3: ("Nublado", "☁️"),
    45: ("Niebla", "🌫️"),
    48: ("Niebla con escarcha", "🌫️"),
    51: ("Llovizna ligera", "🌦️"),
    53: ("Llovizna moderada", "🌦️"),
    55: ("Llovizna densa", "🌧️"),
    61: ("Lluvia ligera", "🌧️"),
    63: ("Lluvia moderada", "🌧️"),
    65: ("Lluvia intensa", "🌧️"),
    71: ("Nieve ligera", "🌨️"),
    73: ("Nieve moderada", "🌨️"),
    75: ("Nieve intensa", "❄️"),
    77: ("Granizo fino", "🌨️"),
    80: ("Chubascos ligeros", "🌦️"),
    81: ("Chubascos moderados", "🌧️"),
    82: ("Chubascos violentos", "⛈️"),
    85: ("Chubascos de nieve", "🌨️"),
    86: ("Chubascos fuertes de nieve", "❄️"),
    95: ("Tormenta eléctrica", "⛈️"),
    96: ("Tormenta con granizo ligero", "⛈️"),
    99: ("Tormenta con granizo fuerte", "⛈️"),
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
    resolved_city = target_city or "Ubicación"

    try:
        with httpx.Client(timeout=10.0) as client:
            if (lat == 0.0 and lon == 0.0) and target_city:
                geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(target_city)}&count=1&language=es&format=json"
                geo_resp = client.get(geo_url)
                geo_resp.raise_for_status()
                geo_data = geo_resp.json()
                results = geo_data.get("results")
                if not results:
                    return NodeResult("error", error=f"No se pudo geocodificar la ciudad '{target_city}'.")
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
        desc, emoji = WMO_WEATHER_CODES.get(w_code, ("Desconocido", "🌡️"))
        temp = curr.get("temperature_2m", 0.0)
        feels_like = curr.get("apparent_temperature", temp)
        humidity = curr.get("relative_humidity_2m", 0)
        wind = curr.get("wind_speed_10m", 0.0)
        precip = curr.get("precipitation", 0.0)

        daily_summary = []
        if daily and daily.get("time"):
            for i, d_date in enumerate(daily["time"]):
                d_code = daily["weather_code"][i] if i < len(daily.get("weather_code", [])) else 0
                d_desc, d_emoji = WMO_WEATHER_CODES.get(d_code, ("Desconocido", "🌡️"))
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
        prob_str = f" · 🌧️ {today_forecast.get('precip_prob')}% lluvia" if today_forecast.get("precip_prob") is not None else ""
        temp_range = f" (Mín: {today_forecast.get('min_temp')}°C / Máx: {today_forecast.get('max_temp')}°C)" if today_forecast.get("max_temp") is not None else ""

        markdown_text = (
            f"{emoji} **{resolved_city}**: {temp}°C (Sensación {feels_like}°C) · {desc}\n"
            f"💨 Viento {wind} km/h · 💧 Humedad {humidity}%{prob_str}{temp_range}"
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
        return NodeResult("error", error=f"Tipo de nodo desconocido: «{node_type}»")

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
                return NodeResult("error", error=f"Función runtime no encontrada para «{node_type}»")
            res = runtime_fn(ctx, items, **cfg)
    except Exception as exc:
        res = NodeResult("error", error=f"Excepción al ejecutar nodo: {exc}")

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
        return 127, "", "El ejecutable 'git' no está instalado o disponible en el PATH del sistema."
    except subprocess.TimeoutExpired:
        return 124, "", f"Tiempo de espera agotado al ejecutar git ({timeout}s)."
    except Exception as exc:
        return 1, "", f"Error inesperado al ejecutar git: {exc}"


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
            return NodeResult("error", error="Se requiere 'url' para clonar un repositorio remoto inexistente.")

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
        return NodeResult("error", error=f"La ruta '{repo_path}' no es un repositorio Git válido.")

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

    return NodeResult("error", error=f"Acción de git no reconocida: «{action}». Usa log, diff, clone_or_pull o status.")


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
        return NodeResult("error", error=f"La ruta '{repo_path}' no es un repositorio Git válido.")

    file_patterns = [f.strip() for f in (files or ".").split(",") if f.strip()] or ["."]
    code, _, err = _run_git(["add", *file_patterns], cwd=path_obj, timeout=timeout)
    if code != 0:
        return NodeResult("error", error=f"Fallo en git add: {err}")

    code_diff, out_diff, _ = _run_git(["diff", "--cached", "--name-only"], cwd=path_obj, timeout=timeout)
    if not out_diff.strip():
        return NodeResult("ok", data={"committed": False, "pushed": False, "message": "No hay cambios pendientes para commitear."})

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
            return NodeResult("error", error=f"Commit realizado ({commit_hash[:7]}), pero fallo en git push: {push_err or push_out}")
        pushed = True

    return NodeResult("ok", data={
        "committed": True,
        "commit_hash": commit_hash,
        "short_hash": commit_hash[:7],
        "message": message.strip(),
        "pushed": pushed,
        "push_output": push_out,
    })


