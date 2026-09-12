"""Funciones de ejecución para los nodos de Dédalo.

Cada función sigue la signatura: `(ctx, inputs, **config) -> NodeResult`.
Los scripts compilados invocan directamente estas funciones sin sobrecarga de runtime.
"""

from __future__ import annotations

import csv
import ctypes
import datetime
import gc
import glob
import json
import os
import re
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

from dedalo import secrets


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
    url: str = "",
    model: str = "",
    secret_name: str = "",
    tokens: int = DEFAULT_AGENT_TOKENS,
    temperature: float = 0.7,
    json_mode: bool = False,
) -> str:
    """Llama a un endpoint compatible con OpenAI (/v1/chat/completions)."""
    base_url = (
        url
        or os.environ.get("DEDALO_LLM_URL")
        or "http://localhost:11434/v1"
    ).rstrip("/")
    target_model = (
        model
        or os.environ.get("DEDALO_LLM_MODEL")
        or "llama3"
    )

    api_key = ""
    if secret_name:
        api_key = secrets.get(secret_name) or ""
    if not api_key:
        api_key = os.environ.get("DEDALO_LLM_KEY", "")

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
        "temperature": temperature,
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
    per_item: bool = False,
    tokens: int = DEFAULT_AGENT_TOKENS,
    model: str = "",
    url: str = "",
    secret: str = "",
    temperature: float = 0.7,
    system_prompt: str = "",
    json_mode: bool = False,
) -> NodeResult:
    """Invoca al LLM con un prompt y los datos de entrada."""
    inp = Inputs(inputs)

    if per_item:
        first = inp.first_data()
        if not isinstance(first, list):
            return NodeResult("error", error="El modo per_item requiere una lista de entrada")
        results = []
        for item in first[:MAX_ITEMS]:
            item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
            user_msg = f"{prompt}\n\nElemento:\n{item_str}"
            try:
                ans = call_llm(
                    user_msg,
                    system=system_prompt,
                    model=model,
                    url=url,
                    secret_name=secret,
                    tokens=tokens,
                    temperature=float(temperature),
                    json_mode=bool(json_mode),
                )
                results.append(ans)
            except Exception as exc:
                return NodeResult("error", error=f"Error en llamada LLM per_item: {exc}")
        return NodeResult("ok", data=results)

    input_text = inp.text()
    user_msg = f"{prompt}\n\nEntrada:\n{input_text}" if input_text else prompt
    try:
        ans = call_llm(
            user_msg,
            system=system_prompt,
            model=model,
            url=url,
            secret_name=secret,
            tokens=tokens,
            temperature=float(temperature),
            json_mode=bool(json_mode),
        )
        return NodeResult("ok", data=ans)
    except Exception as exc:
        return NodeResult("error", error=f"Error en llamada LLM: {exc}")


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


def summarize_text(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    engine: str = "luhn",
    length: str = "medium",
    prompt: str = "",
    field: str = "",
    sentences: int = 0,
    ratio: float = 0.0,
) -> NodeResult:
    """Resume un texto usando Luhn (extractivo configurable) o LLM."""
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

    if engine == "luhn":
        # Limpiar encabezados de secciones comunes (ej. == Encabezado == en Wikipedia o markdown ###)
        cleaned_text = re.sub(r"(?m)^\s*=+[^\n=]+=+\s*$", "", text)

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

        words = re.findall(r"\w{4,}", text.lower())
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1

        scores = []
        for s in sentence_list:
            score = sum(freq.get(w, 0) for w in re.findall(r"\w{4,}", s.lower()))
            scores.append((score, s))

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
        top = sorted(
            sorted(scores, key=lambda x: x[0], reverse=True)[:target_count],
            key=lambda x: sentence_list.index(x[1]),
        )
        return NodeResult("ok", data=" ".join(s for _, s in top))

    # Motor LLM
    sys_prompt = prompt or "Resume el siguiente texto manteniendo los puntos clave."
    limits = {"short": 512, "medium": 1024, "long": 2048, "full": 4096}
    if int(sentences or 0) > 0:
        tokens = min(4096, max(256, int(sentences) * 50))
    else:
        tokens = limits.get(str(length).lower(), 1024)
    try:
        summary = call_llm(text, system=sys_prompt, tokens=tokens)
        return NodeResult("ok", data=summary)
    except Exception as exc:
        return NodeResult("error", error=f"Fallo en resumen con LLM: {exc}")


def agent(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    prompt: str,
    per_item: bool = False,
    tokens: int = DEFAULT_AGENT_TOKENS,
    model: str = "",
    url: str = "",
    secret: str = "",
) -> NodeResult:
    """Invoca al LLM con un prompt y los datos de entrada."""
    inp = Inputs(inputs)

    if per_item:
        first = inp.first_data()
        if not isinstance(first, list):
            return NodeResult("error", error="El modo per_item requiere una lista de entrada")
        results = []
        for item in first[:MAX_ITEMS]:
            item_str = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
            user_msg = f"{prompt}\n\nElemento:\n{item_str}"
            try:
                ans = call_llm(user_msg, model=model, url=url, secret_name=secret, tokens=tokens)
                results.append(ans)
            except Exception as exc:
                return NodeResult("error", error=f"Error en llamada LLM per_item: {exc}")
        return NodeResult("ok", data=results)

    input_text = inp.text()
    user_msg = f"{prompt}\n\nEntrada:\n{input_text}" if input_text else prompt
    try:
        ans = call_llm(user_msg, model=model, url=url, secret_name=secret, tokens=tokens)
        return NodeResult("ok", data=ans)
    except Exception as exc:
        return NodeResult("error", error=f"Error en llamada LLM: {exc}")


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


def summarize(results_dict: dict[str, NodeResult], ctx: Any) -> dict[str, Any]:
    """Resume la ejecución de todos los nodos de un flujo."""
    summary = {}
    for node_id, res in results_dict.items():
        summary[node_id] = {
            "status": res.status,
            "data": res.data if res.ok else None,
            "error": res.error if res.failed else None,
        }
    return summary
