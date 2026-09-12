"""Funciones de ejecución para los nodos de Dédalo.

Cada función sigue la signatura: `(ctx, inputs, **config) -> NodeResult`.
Los scripts compilados invocan directamente estas funciones sin sobrecarga de runtime.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from dedalo import secrets

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

    payload = {
        "model": target_model,
        "messages": messages,
        "max_tokens": tokens,
        "temperature": temperature,
    }

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


def http_call(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    method: str = "GET",
    headers: str = "",
    secret: str = "",
    auth_header: str = "Authorization",
) -> NodeResult:
    """Petición HTTP a una API REST."""
    req_headers = {"User-Agent": "Dedalo/0.1"}
    if headers:
        try:
            extra = json.loads(headers) if isinstance(headers, str) else headers
            if isinstance(extra, dict):
                req_headers.update(extra)
        except json.JSONDecodeError:
            pass

    if secret:
        token = secrets.get(secret)
        if token:
            req_headers[auth_header] = (
                f"Bearer {token}" if auth_header == "Authorization" and not token.startswith("Bearer ") else token
            )

    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = client.request(method.upper(), url, headers=req_headers)
            if resp.status_code >= 400:
                return NodeResult("error", error=f"HTTP {resp.status_code}: {resp.text[:150]}")
            ctype = resp.headers.get("content-type", "")
            if "application/json" in ctype:
                return NodeResult("ok", data=resp.json())
            return NodeResult("ok", data=resp.text)
    except Exception as exc:
        return NodeResult("error", error=f"Fallo HTTP: {exc}")


def rss(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    only_new: bool = False,
    corte: str = "completo",
) -> NodeResult:
    """Lee y parsea un feed RSS."""
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "Dedalo/0.1"})
            if resp.status_code >= 400:
                return NodeResult("error", error=f"HTTP {resp.status_code} al leer RSS")
            xml_text = resp.text
    except Exception as exc:
        return NodeResult("error", error=f"No se pudo descargar el feed: {exc}")

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

    if corte.isdigit():
        max_chars = int(corte)
        for it in items:
            if len(it.get("description", "")) > max_chars:
                it["description"] = it["description"][:max_chars] + "..."

    return NodeResult("ok", data=items)


def csv_file(ctx: Any, inputs: list[NodeResult], *, path: str) -> NodeResult:
    """Lee un fichero CSV local y devuelve lista de diccionarios."""
    p = Path(path)
    if not p.is_file():
        return NodeResult("error", error=f"Fichero no encontrado: {path}")
    try:
        with p.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            return NodeResult("ok", data=list(reader))
    except Exception as exc:
        return NodeResult("error", error=f"Error al leer CSV: {exc}")


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
    limit: float,
) -> NodeResult:
    """Filtra según un umbral numérico. Si la condición no se cumple, devuelve SKIPPED."""
    inp = Inputs(inputs)
    val = inp.first_data()
    if val is None:
        return SKIPPED
    try:
        num = float(val)
    except (ValueError, TypeError):
        return NodeResult("error", error=f"El valor de entrada no es numérico: {val!r}")

    lim = float(limit)
    ok = False
    if comparison == "menor_que":
        ok = num < lim
    elif comparison == "menor_o_igual":
        ok = num <= lim
    elif comparison == "mayor_que":
        ok = num > lim
    elif comparison == "mayor_o_igual":
        ok = num >= lim

    return NodeResult("ok", data=num) if ok else SKIPPED


def filter_items(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    contains: str,
    field: str = "",
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
        if isinstance(elem, dict):
            target = str(elem.get(field, "") if field else json.dumps(elem))
        else:
            target = str(elem)
        if term in target.lower():
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


def web_read(
    ctx: Any,
    inputs: list[NodeResult],
    *,
    url: str,
    length: str = "medium",
) -> NodeResult:
    """Descarga y limpia una página web."""
    res = http_call(ctx, inputs, url=url)
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
) -> NodeResult:
    """Resume un texto usando Luhn (extractivo) o LLM."""
    inp = Inputs(inputs)
    text = inp.text()
    if not text:
        return NodeResult("ok", data="")

    if engine == "luhn":
        # Algoritmo extractivo básico basado en frecuencia de palabras
        sentences = re.split(r"(?<=[.?!])\s+", text)
        words = re.findall(r"\w{4,}", text.lower())
        freq: dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1

        scores = []
        for s in sentences:
            score = sum(freq.get(w, 0) for w in re.findall(r"\w{4,}", s.lower()))
            scores.append((score, s))

        target_count = {"short": 3, "medium": 6, "long": 12, "full": len(sentences)}.get(
            length, 6
        )
        top = sorted(sorted(scores, key=lambda x: x[0], reverse=True)[:target_count], key=lambda x: sentences.index(x[1]))
        return NodeResult("ok", data=" ".join(s for _, s in top))

    # Motor LLM
    sys_prompt = prompt or "Resume el siguiente texto manteniendo los puntos clave."
    limits = {"short": 512, "medium": 1024, "long": 2048, "full": 4096}
    tokens = limits.get(length, 1024)
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
) -> NodeResult:
    """Escribe la salida en un fichero del disco."""
    inp = Inputs(inputs)
    payload = message if content == "static" else inp.text()

    today = time.strftime("%Y-%m-%d")
    final_name = filename.replace("{fecha}", today)

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
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json={"chat_id": dest_chat, "text": text[:4000]})
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
