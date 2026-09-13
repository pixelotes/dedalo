"""Compilador de grafos de Dédalo a código Python plano.

Un flujo se define como un grafo acíclico dirigido (DAG) de nodos de entrada,
transformación y salida. Al compilarse, no se ejecuta un intérprete de grafos:
se genera un script Python independiente con una función plana `run(ctx)`.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from dedalo.runtime import DEFAULT_AGENT_TOKENS

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENTRADA_REF_RE = re.compile(r"""entrada(?:\s*\[\s*|\.get\(\s*)['"]([^'"]+)['"]""")
_compile_source = compile

VERSION = 1
PORTS = ("ok", "error")
FAN_IN_POLICIES = ("all", "any")


class GraphError(Exception):
    """Lanzada por `compile()` cuando el grafo no pasa la validación."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(errors))


@dataclass
class Node:
    id: str
    type: str  # ej. "input.rss", "transform.agent", "output.file"
    label: str = ""
    config: dict = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "config": dict(self.config),
            "x": self.x,
            "y": self.y,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        return cls(
            id=str(data["id"]),
            type=str(data["type"]),
            label=str(data.get("label", "")),
            config=dict(data.get("config") or {}),
            x=float(data.get("x", 0) or 0),
            y=float(data.get("y", 0) or 0),
        )


@dataclass
class Edge:
    source: str
    target: str
    port: str = "ok"  # "ok" o "error"

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target, "port": self.port}

    @classmethod
    def from_dict(cls, data: dict) -> "Edge":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            port=str(data.get("port", "ok")),
        )


@dataclass
class Graph:
    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    version: int = VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        return cls(
            name=str(data.get("name", "")),
            nodes=[Node.from_dict(n) for n in data.get("nodes") or []],
            edges=[Edge.from_dict(e) for e in data.get("edges") or []],
            version=int(data.get("version", VERSION)),
        )

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id]

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]


@dataclass
class NodeSpec:
    category: str  # "input" | "transform" | "output"
    runtime_fn: str  # Nombre de la función en dedalo.runtime
    required: frozenset = frozenset()
    optional: dict = field(default_factory=dict)
    label: str = ""
    output: str = "texto"
    code_field: str = ""
    show_when: dict = field(default_factory=dict)
    choices: dict = field(default_factory=dict)
    choices_from: dict = field(default_factory=dict)
    help: dict = field(default_factory=dict)


NODE_TYPES: dict[str, NodeSpec] = {
    # -- Entradas -----------------------------------------------------------
    "input.webhook": NodeSpec(
        category="input",
        runtime_fn="webhook_trigger",
        label="Webhook Entrante",
        output="payload recibido (dict o texto)",
        help={
            "url_info": "Recibe peticiones HTTP en /api/webhook/{nombre_del_flujo}.",
        },
    ),
    "input.http_call": NodeSpec(
        category="input",
        runtime_fn="http_call",
        required=frozenset({"url"}),
        optional={
            "method": "GET",
            "headers": "",
            "body": "",
            "secret": "",
            "auth_header": "Authorization",
            "timeout": 20.0,
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label="Traer de una API / Web",
        output="dict si devuelve JSON, texto si no",
        choices={
            "method": ["GET", "POST", "PUT", "DELETE"],
            "engine": [
                {"value": "auto", "label": "Auto (curl_cffi con fallback a httpx)"},
                {"value": "curl_cffi", "label": "curl_cffi (suplantación TLS/navegador)"},
                {"value": "httpx", "label": "httpx (cliente HTTP estándar)"},
            ],
            "impersonate": [
                {"value": "chrome", "label": "Google Chrome (recomendado)"},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": "Sin suplantación"},
            ],
        },
        choices_from={"secret": "secrets"},
        help={
            "url": "URL de la API REST o página web a consultar.",
            "body": "Cuerpo para POST/PUT (admite {entrada} o JSON).",
            "secret": "Nombre del secreto con el token de autenticación.",
            "auth_header": "Cabecera donde va el secreto (por defecto Authorization).",
            "timeout": "Tiempo límite de espera en segundos.",
            "engine": "Motor HTTP: 'auto' usa curl_cffi para saltarse antibots, o httpx como fallback.",
            "impersonate": "Navegador a suplantar a nivel TLS y HTTP/2 para eludir bloqueos antibot (Cloudflare, etc.).",
            "user_agent": "User-Agent opcional. Presets: 'chrome', 'safari', 'firefox', 'mobile', 'bot', o texto libre.",
        },
    ),
    "input.rss": NodeSpec(
        category="input",
        runtime_fn="rss",
        required=frozenset({"url"}),
        optional={
            "only_new": False,
            "corte": "completo",
            "max_items": 20,
            "filter_keyword": "",
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label="Feed RSS / Atom",
        output="lista de noticias (title, link, guid, date, description)",
        choices={
            "corte": [
                {"value": "300", "label": "300 caracteres"},
                {"value": "600", "label": "600 caracteres"},
                {"value": "1000", "label": "1000 caracteres"},
                {"value": "completo", "label": "Completo"},
            ],
            "engine": [
                {"value": "auto", "label": "Auto (curl_cffi si disponible)"},
                {"value": "curl_cffi", "label": "curl_cffi (suplantación TLS)"},
                {"value": "httpx", "label": "httpx (cliente estándar)"},
            ],
            "impersonate": [
                {"value": "chrome", "label": "Google Chrome"},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": "Sin suplantación"},
            ],
        },
        help={
            "only_new": "Salta las noticias ya vistas en ejecuciones anteriores.",
            "max_items": "Número máximo de noticias a extraer.",
            "filter_keyword": "Filtra noticias que contengan esta palabra.",
            "corte": "Límite de caracteres por descripción.",
            "engine": "Motor HTTP para descargar el feed (útil para feeds en Cloudflare o Substack).",
            "impersonate": "Navegador a suplantar para evitar que el feed sea bloqueado.",
            "user_agent": "User-Agent opcional para la petición al feed.",
        },
    ),
    "input.web_read": NodeSpec(
        category="input",
        runtime_fn="web_read",
        required=frozenset({"url"}),
        optional={
            "length": "medium",
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label="Extraer página web",
        output="texto limpio y condensado",
        choices={
            "length": ["short", "medium", "long", "full"],
            "engine": [
                {"value": "auto", "label": "Auto (curl_cffi si disponible)"},
                {"value": "curl_cffi", "label": "curl_cffi (suplantación TLS)"},
                {"value": "httpx", "label": "httpx (cliente estándar)"},
            ],
            "impersonate": [
                {"value": "chrome", "label": "Google Chrome"},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": "Sin suplantación"},
            ],
        },
        help={
            "url": "URL de la página web a leer y limpiar.",
            "length": "Extensión deseada del texto extraído.",
            "engine": "Motor HTTP: 'auto' usa curl_cffi para saltarse antibots, o httpx como fallback.",
            "impersonate": "Perfil de navegador a suplantar (Chrome, Safari, Firefox).",
            "user_agent": "User-Agent opcional (dejar vacío para usar el nativo del navegador suplantado).",
        },
    ),
    "input.web_search": NodeSpec(
        category="input",
        runtime_fn="web_search",
        required=frozenset({"query"}),
        optional={
            "engine": "duckduckgo",
            "limit": 5,
            "secret": "",
            "output_format": "json",
            "timeout": 15.0,
        },
        label="Búsqueda web",
        output="resultados de búsqueda (lista JSON o texto markdown)",
        choices={
            "engine": [
                {"value": "duckduckgo", "label": "DuckDuckGo (gratuito, sin clave)"},
                {"value": "tavily", "label": "Tavily Search API (requiere API key)"},
                {"value": "exa", "label": "Exa / Metaphor API (requiere API key)"},
            ],
            "output_format": [
                {"value": "json", "label": "Estructurado (JSON / lista de dicts)"},
                {"value": "markdown", "label": "Lista Markdown formateada"},
            ],
        },
        choices_from={"secret": "secrets"},
        show_when={"secret": {"engine": ["tavily", "exa"]}},
        help={
            "query": "Término o consulta de búsqueda (soporta {entrada}).",
            "engine": "Motor de búsqueda: DuckDuckGo (sin API key), Tavily o Exa.",
            "limit": "Número máximo de resultados a obtener.",
            "secret": "Nombre del secreto con la API Key (requerido para Tavily y Exa).",
            "output_format": "Formato de salida: 'json' (lista estructurada) o 'markdown' (enlaces con viñetas).",
            "timeout": "Tiempo máximo en segundos para la petición.",
        },
    ),
    "input.csv_file": NodeSpec(
        category="input",
        runtime_fn="csv_file",
        required=frozenset({"path"}),
        optional={"delimiter": ",", "encoding": "utf-8"},
        label="Fichero CSV",
        output="lista de diccionarios (filas)",
        choices={
            "delimiter": [
                {"value": ",", "label": "Coma (,)"},
                {"value": ";", "label": "Punto y coma (;)"},
                {"value": "\\t", "label": "Tabulador (\\t)"},
                {"value": "|", "label": "Pleca (|)"},
            ],
            "encoding": ["utf-8", "latin-1", "ascii"],
        },
        help={"path": "Ruta al fichero CSV en disco."},
    ),
    "input.json_file": NodeSpec(
        category="input",
        runtime_fn="json_file",
        required=frozenset({"path"}),
        label="Fichero JSON",
        output="dict o lista según el archivo",
        help={"path": "Ruta al fichero JSON en disco."},
    ),
    "input.sqlite_query": NodeSpec(
        category="input",
        runtime_fn="sqlite_query",
        required=frozenset({"query"}),
        optional={"path": "data/store.db"},
        label="Consulta SQLite (SELECT)",
        output="lista de diccionarios (filas)",
        help={
            "query": "Consulta SQL SELECT a ejecutar.",
            "path": "Ruta al fichero de base de datos SQLite.",
        },
    ),
    "input.command": NodeSpec(
        category="input",
        runtime_fn="command_input",
        required=frozenset({"cmd"}),
        optional={"timeout": 15.0, "cwd": ""},
        label="Comando del Sistema (Shell)",
        output="dict con stdout, stderr, exit_code",
        help={
            "cmd": "Comando bash/shell a ejecutar (admite {entrada}).",
            "timeout": "Segundos máximos de ejecución.",
            "cwd": "Directorio de trabajo relativo o absoluto.",
        },
    ),
    "input.git": NodeSpec(
        category="input",
        runtime_fn="git_read",
        optional={
            "action": "log",
            "repo_path": ".",
            "url": "",
            "revision": "HEAD~1..HEAD",
            "since": "",
            "limit": 5,
            "path_filter": "",
            "max_chars": 8000,
            "timeout": 30.0,
        },
        label="Lector Git",
        output="commits (log), parche (diff), o estado del repositorio",
        choices={
            "action": [
                {"value": "log", "label": "Historial de commits (log)"},
                {"value": "diff", "label": "Parche de cambios (diff)"},
                {"value": "clone_or_pull", "label": "Clonar o actualizar (clone/pull)"},
                {"value": "status", "label": "Estado del árbol (status)"},
            ],
        },
        help={
            "action": "Acción a ejecutar: log (commits), diff (cambios), clone_or_pull (clonar/actualizar), status.",
            "repo_path": "Ruta local al repositorio (por defecto . para el repo actual).",
            "url": "URL remota de Git (requerida si se clona un repo inexistente).",
            "revision": "Rango o referencia de revisión para diff (ej. HEAD~1..HEAD o main..feature).",
            "since": "Ventana temporal para log (ej. '24 hours ago', 'yesterday').",
            "limit": "Número máximo de commits a devolver en modo log.",
            "path_filter": "Filtro de rutas o extensiones separado por comas (ej. '*.py, *.js').",
            "max_chars": "Límite máximo de caracteres del diff para no desbordar tokens.",
        },
    ),
    "input.pi_health": NodeSpec(
        category="input",
        runtime_fn="pi_health",
        label="Salud del Sistema (Pi / Linux)",
        output="dict con cpu_temp_c, memory_percent_used, memory_free_mb, load_1m, disk_percent_used",
        help={
            "info": "Lee temperatura de CPU, memoria y carga directamente de /proc y /sys sin dependencias.",
        },
    ),
    "input.directory_scan": NodeSpec(
        category="input",
        runtime_fn="directory_scan",
        optional={"path": ".", "pattern": "*", "recursive": False, "max_items": 50},
        label="Escanear Directorio",
        output="lista de ficheros (name, path, size_bytes, modified_at)",
        help={
            "path": "Carpeta a escanear.",
            "pattern": "Patrón de búsqueda (ej. *.log, *.pdf).",
            "recursive": "Incluir subdirectorios.",
        },
    ),
    "input.http_health": NodeSpec(
        category="input",
        runtime_fn="http_health",
        required=frozenset({"url"}),
        optional={"timeout": 5.0, "expected_status": 200},
        label="Comprobación de Salud HTTP",
        output="dict con url, status_code, latency_ms, is_healthy",
        help={
            "url": "URL a comprobar.",
            "expected_status": "Código de respuesta esperado.",
        },
    ),
    "input.disk_free": NodeSpec(
        category="input",
        runtime_fn="disk_free",
        optional={"path": ""},
        label="Espacio libre en disco",
        output="número (% libre)",
    ),
    "input.cpu_load": NodeSpec(
        category="input",
        runtime_fn="cpu_load",
        optional={"seconds": 1.0},
        label="Uso de CPU",
        output="número (% de carga)",
    ),
    "input.ssl_cert": NodeSpec(
        category="input",
        runtime_fn="ssl_cert",
        required=frozenset({"host"}),
        optional={"port": 443, "timeout": 5.0},
        label="Certificado SSL/TLS",
        output="dict con days_left, expires_at, is_valid, issuer",
        help={
            "host": "Dominio o host a verificar (ej. google.com o mi-servidor.org).",
            "port": "Puerto TLS (por defecto 443).",
            "timeout": "Tiempo máximo en segundos.",
        },
    ),
    "input.dns_check": NodeSpec(
        category="input",
        runtime_fn="dns_check",
        required=frozenset({"host"}),
        optional={"record_type": "A", "timeout": 5.0},
        label="Comprobación DNS",
        output="dict con ips, canonical_name, response_time_ms",
        help={
            "host": "Dominio a resolver (ej. example.com).",
            "timeout": "Tiempo límite en segundos.",
        },
    ),
    "input.ping": NodeSpec(
        category="input",
        runtime_fn="ping_host",
        required=frozenset({"host"}),
        optional={"port": 80, "mode": "tcp", "timeout": 3.0},
        label="Ping y Latencia",
        output="dict con alive, latency_ms, host",
        choices={
            "mode": [
                {"value": "tcp", "label": "TCP Connect (sin root)"},
                {"value": "icmp", "label": "ICMP Ping (sistema)"},
            ],
        },
        help={
            "host": "Host o IP a comprobar.",
            "port": "Puerto para ping TCP (por defecto 80).",
            "mode": "Modo de comprobación: TCP o ICMP.",
        },
    ),
    "input.mqtt": NodeSpec(
        category="input",
        runtime_fn="mqtt_read",
        required=frozenset({"topic"}),
        optional={"broker": "localhost", "port": 1883, "username": "", "secret": "", "timeout": 5.0},
        label="MQTT Suscribir",
        output="mensaje recibido en el topic",
        choices_from={"secret": "secrets"},
        help={
            "topic": "Topic MQTT a escuchar.",
            "broker": "Dirección del broker (ej. localhost o 192.168.1.50).",
            "secret": "Secreto con la contraseña si el broker requiere auth.",
        },
    ),
    "input.imap": NodeSpec(
        category="input",
        runtime_fn="imap_read",
        required=frozenset({"host", "username"}),
        optional={
            "port": 993,
            "secret": "",
            "folder": "INBOX",
            "filter": "UNSEEN",
            "limit": 5,
            "mark_as_read": False,
            "timeout": 15.0,
            "clean_body": True,
            "strip_signatures": True,
            "max_body_chars": 3000,
        },
        label="Lector Correo (IMAP)",
        output="lista de correos entrantes con asunto, emisor y cuerpo",
        choices_from={"secret": "secrets"},
        choices={
            "filter": [
                {"value": "UNSEEN", "label": "Solo no leídos (UNSEEN)"},
                {"value": "ALL", "label": "Todos los recientes (ALL)"},
                {"value": "FLAGGED", "label": "Destacados (FLAGGED)"},
            ],
        },
        help={
            "host": "Servidor IMAP (ej. imap.gmail.com).",
            "username": "Usuario o email de acceso.",
            "secret": "Secreto con la contraseña o App Password.",
            "folder": "Buzón a consultar (por defecto INBOX).",
            "limit": "Número máximo de correos a recuperar.",
            "mark_as_read": "Si es verdadero, marca los mensajes como leídos.",
            "clean_body": "Limpia HTML, estilos ocultos y caracteres invisibles anti prompt injection.",
            "strip_signatures": "Elimina firmas mecánicas y cláusulas legales/RGPD del cuerpo.",
            "max_body_chars": "Límite máximo de caracteres por cuerpo de correo.",
        },
    ),
    "input.sitemap": NodeSpec(
        category="input",
        runtime_fn="sitemap_read",
        required=frozenset({"url"}),
        optional={
            "max_urls": 50,
            "filter_hours": 0.0,
            "filter_keyword": "",
            "follow_sub_sitemaps": True,
        },
        label="Lector de Sitemap (XML)",
        output="lista de URLs encontradas con fecha lastmod",
        help={
            "url": "URL del archivo sitemap.xml (ej. https://ejemplo.com/sitemap.xml).",
            "max_urls": "Número máximo de URLs a recuperar.",
            "filter_hours": "Horas recientes según fecha lastmod (0 = sin límite, ej. 24).",
            "filter_keyword": "Filtrar URLs que contengan esta palabra o segmento.",
            "follow_sub_sitemaps": "Si es un sitemapindex, desciende automáticamente a sub-sitemaps.",
        },
    ),
    "input.weather": NodeSpec(
        category="input",
        runtime_fn="weather_forecast",
        optional={
            "city": "Valencia",
            "latitude": 0.0,
            "longitude": 0.0,
            "forecast_days": 1,
            "format": "summary",
        },
        label="Meteorología (Open-Meteo)",
        output="resumen del tiempo y previsión",
        choices={
            "format": [
                {"value": "summary", "label": "Resumen estructurado (dict)"},
                {"value": "markdown", "label": "Texto formateado (Markdown)"},
                {"value": "raw", "label": "JSON crudo Open-Meteo"},
            ]
        },
        help={
            "city": "Ciudad a consultar (geocodificación automática sin API key).",
            "latitude": "Latitud exacta (opcional si se indica ciudad).",
            "longitude": "Longitud exacta (opcional si se indica ciudad).",
            "forecast_days": "Días de previsión a incluir (1 a 7).",
        },
    ),
    # -- Transformaciones ---------------------------------------------------
    "transform.delay": NodeSpec(
        category="transform",
        runtime_fn="delay_execution",
        optional={
            "seconds": 2.0,
            "jitter": 0.0,
        },
        label="Pausa / Espera (Delay)",
        output="mismos datos recibidos (sin modificar)",
        help={
            "seconds": "Segundos de pausa antes de emitir los datos al siguiente nodo.",
            "jitter": "Variación aleatoria máxima en segundos (0 a jitter) para evitar detección en scraping.",
        },
    ),
    "transform.datetime": NodeSpec(
        category="transform",
        runtime_fn="datetime_ops",
        optional={
            "mode": "format",
            "preset": "es_long",
            "format_str": "%Y-%m-%d %H:%M:%S",
            "offset_days": 0,
            "offset_hours": 0,
            "offset_minutes": 0,
            "timezone": "Europe/Madrid",
        },
        label="Operaciones de Fecha y Hora",
        output="fecha formateada (texto) y metadatos (dict)",
        choices={
            "mode": [
                {"value": "format", "label": "Formatear fecha recibida / actual"},
                {"value": "now", "label": "Hora actual del sistema"},
                {"value": "offset", "label": "Aplicar offset (sumar/restar tiempo)"},
            ],
            "preset": [
                {"value": "es_long", "label": "Largo español (ej: Domingo, 13 de Septiembre...)"},
                {"value": "es_short", "label": "Corto español (ej: 13 de Sep de 2026 14:30)"},
                {"value": "iso", "label": "Estándar ISO 8601"},
                {"value": "date_only", "label": "Solo fecha (YYYY-MM-DD)"},
                {"value": "time_only", "label": "Solo hora (HH:MM:SS)"},
                {"value": "custom", "label": "Personalizado con format_str"},
            ],
        },
        help={
            "mode": "Modo de operación.",
            "preset": "Formato predeterminado de salida.",
            "offset_days": "Días a sumar (positivo) o restar (negativo).",
            "offset_hours": "Horas a sumar o restar.",
            "timezone": "Zona horaria IANA (ej. Europe/Madrid, UTC, America/Buenos_Aires).",
        },
    ),
    "transform.template": NodeSpec(
        category="transform",
        runtime_fn="template",
        required=frozenset({"template"}),
        label="Plantilla de Texto",
        output="texto con variables interpoladas",
        help={
            "template": "Plantilla con placeholders como {entrada}, {fecha}, {hora}, {campo}.",
        },
    ),
    "transform.array_ops": NodeSpec(
        category="transform",
        runtime_fn="array_ops",
        optional={"operation": "head", "count": 10, "field": "", "order": "asc", "join_sep": "\\n"},
        label="Operaciones de Lista",
        output="lista filtrada/ordenada o texto unido",
        choices={
            "operation": [
                {"value": "head", "label": "Primeros N elementos"},
                {"value": "tail", "label": "Últimos N elementos"},
                {"value": "sort", "label": "Ordenar por campo"},
                {"value": "dedup", "label": "Desduplicar elementos"},
                {"value": "join", "label": "Unir en un solo texto (Join)"},
            ],
            "order": ["asc", "desc"],
        },
        help={
            "field": "Campo sobre el que ordenar o desduplicar (si son diccionarios).",
            "join_sep": "Separador para la operación join (por defecto salto de línea).",
        },
    ),
    "transform.diff": NodeSpec(
        category="transform",
        runtime_fn="diff_data",
        optional={"key_field": "", "mode": "added", "state_key": ""},
        label="Detección de Cambios (Diff)",
        output="elementos nuevos o cambiados respecto a la corrida previa",
        choices={
            "mode": [
                {"value": "added", "label": "Solo elementos nuevos (added)"},
                {"value": "modified", "label": "Solo modificados (modified)"},
                {"value": "removed", "label": "Solo eliminados (removed)"},
                {"value": "all", "label": "Todos los cambios (dict con added/modified/removed)"},
            ],
        },
        help={
            "key_field": "Campo identificador único (ej: url, id, title). Si está vacío se detecta automáticamente.",
            "mode": "Qué conjunto emitir: nuevos, modificados, eliminados o resumen completo.",
            "state_key": "Identificador opcional de la memoria de estado.",
        },
    ),
    "transform.fuzzy_dedup": NodeSpec(
        category="transform",
        runtime_fn="fuzzy_dedup",
        optional={"field": "title", "threshold": 0.80, "merge_strategy": "keep_first"},
        label="Deduplicación Difusa",
        output="lista de elementos deduplicados por similitud léxica",
        choices={
            "merge_strategy": [
                {"value": "keep_first", "label": "Conservar el primero y descartar duplicados"},
                {"value": "merge_list", "label": "Conservar el primero y adjuntar lista de duplicados"},
                {"value": "group", "label": "Agrupar en clusters de duplicados"},
            ],
        },
        help={
            "field": "Campo a comparar para medir la similitud (ej: title, nombre).",
            "threshold": "Umbral de similitud entre 0.0 y 1.0 (0.80 es recomendado).",
            "merge_strategy": "Estrategia al encontrar duplicados.",
        },
    ),
    "transform.aggregate": NodeSpec(
        category="transform",
        runtime_fn="aggregate",
        optional={"field": "", "operation": "avg", "group_by": ""},
        label="Agregador Estadístico",
        output="cálculo numérico o resumen estadístico",
        choices={
            "operation": [
                {"value": "avg", "label": "Media aritmética (promedio)"},
                {"value": "sum", "label": "Suma total"},
                {"value": "min", "label": "Valor mínimo"},
                {"value": "max", "label": "Valor máximo"},
                {"value": "count", "label": "Conteo de elementos"},
                {"value": "median", "label": "Mediana"},
                {"value": "summary", "label": "Resumen completo (todas las métricas)"},
            ],
        },
        help={
            "field": "Campo numérico a evaluar en cada objeto (dejar vacío si los elementos son números directos).",
            "operation": "Métrica estadística a calcular.",
            "group_by": "Campo opcional para agrupar las estadísticas.",
        },
    ),
    "transform.table": NodeSpec(
        category="transform",
        runtime_fn="format_table",
        optional={"format": "markdown", "columns": "", "headers": "", "max_rows": 100},
        label="Formateador de Tablas",
        output="tabla formateada en Markdown o HTML",
        choices={
            "format": [
                {"value": "markdown", "label": "Tabla Markdown"},
                {"value": "html", "label": "Tabla HTML (estilos inline)"},
            ],
        },
        help={
            "columns": "Campos a incluir en la tabla separados por comas (ej. title,price,store).",
            "headers": "Encabezados personalizados separados por comas (ej. Título,Precio,Tienda).",
            "max_rows": "Número máximo de filas a renderizar.",
        },
    ),
    "transform.keywords_rake": NodeSpec(
        category="transform",
        runtime_fn="keywords_rake",
        optional={"max_keywords": 5, "min_freq": 1, "max_words": 3, "language": "es", "output_format": "list"},
        label="Palabras Clave (RAKE)",
        output="frases y conceptos clave extraídos del texto",
        choices={
            "language": [
                {"value": "es", "label": "Español"},
                {"value": "en", "label": "Inglés"},
            ],
            "output_format": [
                {"value": "list", "label": "Lista estructurada"},
                {"value": "text", "label": "Texto separado por comas"},
            ],
        },
        help={
            "max_keywords": "Cantidad máxima de frases clave a devolver.",
            "max_words": "Longitud máxima en palabras de cada frase clave.",
            "min_freq": "Frecuencia mínima de aparición.",
        },
    ),
    "transform.regex": NodeSpec(
        category="transform",
        runtime_fn="regex",
        required=frozenset({"pattern"}),
        optional={"action": "find_all", "replace_with": "", "field": ""},
        label="Expresión Regular (Regex)",
        output="lista de coincidencias o texto modificado",
        choices={
            "action": [
                {"value": "find_all", "label": "Buscar todas las coincidencias"},
                {"value": "search", "label": "Buscar primera coincidencia"},
                {"value": "sub", "label": "Sustituir coincidencias"},
            ]
        },
        help={
            "pattern": "Expresión regular (ej. [0-9]+).",
            "replace_with": "Texto de sustitución si la acción es 'sub'.",
        },
    ),
    "transform.router": NodeSpec(
        category="transform",
        runtime_fn="router",
        optional={"condition": "truthy", "field": "", "value": ""},
        label="Bifurcador Condicional (Router)",
        output="emite por puerto OK si cumple la condición, o por ERROR si no",
        choices={
            "condition": [
                {"value": "truthy", "label": "Verdadero o con contenido"},
                {"value": "falsy", "label": "Falso o vacío"},
                {"value": "equals", "label": "Igual al valor"},
                {"value": "not_equals", "label": "Distinto del valor"},
                {"value": "contains", "label": "Contiene texto"},
            ]
        },
        help={
            "field": "Campo del diccionario a comprobar (opcional).",
            "value": "Valor a comparar (para equals, not_equals o contains).",
        },
    ),
    "transform.agent": NodeSpec(
        category="transform",
        runtime_fn="agent",
        required=frozenset({"prompt"}),
        optional={
            "per_item": False,
            "as_field": "",
            "tokens": DEFAULT_AGENT_TOKENS,
            "max_context": 0,
            "model": "",
            "url": "",
            "secret": "",
            "temperature": 0.7,
            "system_prompt": "",
            "json_mode": False,
        },
        label="Agente LLM",
        output="texto generado; lista si per_item",
        choices_from={"secret": "secrets"},
        help={
            "prompt": "Instrucciones para el LLM.",
            "system_prompt": "Prompt de sistema para guiar el comportamiento (opcional).",
            "json_mode": "Forzar respuesta en formato JSON estructurado.",
            "temperature": "Creatividad (0.0 muy determinista, 1.0 muy creativo).",
            "per_item": "Ejecuta una llamada al modelo por cada elemento de una lista.",
            "as_field": "Si se activa per_item, almacena la respuesta en este campo del objeto original en lugar de sustituirlo.",
            "tokens": "Tokens máximos a generar en la respuesta (max_tokens).",
            "max_context": "Límite manual de tokens de la ventana de contexto (0 para desactivar; compacta la entrada con Luhn+ al alcanzar el 90%).",
            "model": "Nombre del modelo (vacío usa el configurado por defecto).",
            "url": "URL base de la API OpenAI (vacío usa la global).",
            "secret": "Secreto con la API Key (si aplica).",
        },
    ),
    "transform.coder": NodeSpec(
        category="transform",
        runtime_fn="coder_agent",
        required=frozenset({"prompt"}),
        optional={
            "workspace_dir": ".",
            "tools_preset": "coding",
            "custom_tools": "",
            "max_turns": 5,
            "max_tool_chars": 4000,
            "max_context": 0,
            "output_format": "full",
            "model": "",
            "url": "",
            "secret": "",
            "temperature": 0.1,
            "system_prompt": "",
            "timeout": 120.0,
        },
        label="Agente Programador (Coder)",
        output="dict con resumen, turnos, archivos modificados y registro de herramientas (o texto)",
        choices={
            "tools_preset": ["coding", "read_only", "all", "custom"],
            "output_format": ["full", "text"],
        },
        choices_from={"secret": "secrets"},
        help={
            "prompt": "Tarea o instrucciones de programación a resolver por el agente.",
            "workspace_dir": "Directorio raíz relativo o absoluto sobre el que operarán las herramientas.",
            "tools_preset": "Conjunto de herramientas: coding (read, bash, edit, write), read_only (read, grep, find, ls), all (todas) o custom.",
            "custom_tools": "Lista separada por comas de herramientas si tools_preset es custom.",
            "max_turns": "Número máximo de turnos de razonamiento e invocación de herramientas.",
            "max_tool_chars": "Límite de caracteres en la salida de cada herramienta para proteger el contexto.",
            "max_context": "Límite de tokens de la ventana de contexto (0 para autodetección desde el servidor; compacta con Luhn+ al 90%).",
            "output_format": "Formato de salida: 'full' (objeto estructurado con resumen y métricas) o 'text' (solo la respuesta textual).",
            "model": "Nombre del modelo en el servidor de inferencia (vacío usa DEDALO_LLM_MODEL).",
            "url": "URL base compatible con OpenAI (vacío usa DEDALO_LLM_URL).",
            "secret": "Nombre del secreto con la API Key (si aplica).",
            "temperature": "Temperatura para el modelo (0.0 para máxima precisión).",
            "system_prompt": "Prompt de sistema para guiar el comportamiento (opcional).",
            "timeout": "Tiempo máximo en segundos por turno HTTP.",
        },
    ),
    "transform.clean_email": NodeSpec(
        category="transform",
        runtime_fn="clean_email_node",
        optional={
            "strip_signatures": True,
            "strip_disclaimers": True,
            "max_chars": 3000,
        },
        label="Limpiar Correo",
        output="texto o lista de correos con HTML, firmas e inyecciones eliminadas",
        help={
            "strip_signatures": "Elimina firmas mecánicas (ej. 'Enviado desde mi iPhone', '-- ').",
            "strip_disclaimers": "Elimina avisos legales y cláusulas de confidencialidad/RGPD.",
            "max_chars": "Longitud máxima de caracteres permitidos por correo.",
        },
    ),
    "transform.summarize": NodeSpec(
        category="transform",
        runtime_fn="summarize_text",
        optional={
            "engine": "luhn+",
            "length": "medium",
            "format": "paragraph",
            "prompt": "",
            "field": "",
            "sentences": 0,
            "ratio": 0.0,
        },
        label="Resumir texto",
        output="texto resumido",
        choices={
            "engine": [
                {"value": "luhn+", "label": "Luhn+ (stopwords, sesgo y diversidad)"},
                {"value": "textrank", "label": "TextRank (grafo y centralidad)"},
                {"value": "luhn", "label": "Luhn clásico (frecuencia simple)"},
                {"value": "llm", "label": "LLM (generativo)"},
            ],
            "length": ["short", "medium", "long", "full"],
            "format": [
                {"value": "paragraph", "label": "Párrafo continuo"},
                {"value": "bullets", "label": "Lista con viñetas (- ...)"},
            ],
        },
        show_when={"prompt": {"engine": ["llm"]}},
        help={
            "engine": "Motor de resumen: Luhn+ (optimizado), TextRank (grafo léxico), Luhn clásico o LLM.",
            "format": "Formato de salida: 'paragraph' (texto corrido) o 'bullets' (viñetas).",
            "length": "Longitud predefinida (short, medium, long, full). Se ignora si sentences > 0 o ratio > 0.",
            "field": "Campo específico si la entrada es un diccionario (ej. 'extract', o notación punto 'query.pages.0.extract').",
            "sentences": "Número exacto de frases a extraer (0 usa 'length').",
            "ratio": "Porcentaje de frases a extraer (0.0 a 1.0, ej. 0.3 para 30%).",
        },
    ),
    "transform.threshold": NodeSpec(
        category="transform",
        runtime_fn="threshold",
        required=frozenset({"comparison", "limit"}),
        label="Comparar con límite",
        output="valor o SKIPPED si no se cumple",
        choices={
            "comparison": [
                {"value": "menor_que", "label": "menor que (número)"},
                {"value": "menor_o_igual", "label": "menor o igual que (número)"},
                {"value": "mayor_que", "label": "mayor que (número)"},
                {"value": "mayor_o_igual", "label": "mayor o igual que (número)"},
                {"value": "igual", "label": "igual a (número)"},
                {"value": "distinto", "label": "distinto de (número)"},
                {"value": "contiene", "label": "contiene texto"},
                {"value": "no_contiene", "label": "no contiene texto"},
                {"value": "empieza_por", "label": "empieza por texto"},
                {"value": "termina_por", "label": "termina por texto"},
                {"value": "regex", "label": "coincide con regex"},
                {"value": "no_vacio", "label": "no está vacío"},
                {"value": "es_vacio", "label": "está vacío"},
            ]
        },
        help={"limit": "Si no se cumple la condición, el flujo se detiene aquí en silencio."},
    ),
    "transform.filter": NodeSpec(
        category="transform",
        runtime_fn="filter_items",
        required=frozenset({"contains"}),
        optional={"field": "", "mode": "contains"},
        label="Filtrar elementos",
        output="lista filtrada",
        choices={
            "mode": [
                {"value": "contains", "label": "Contiene texto"},
                {"value": "not_contains", "label": "No contiene texto"},
                {"value": "regex", "label": "Expresión regular"},
                {"value": "gt", "label": "Mayor que (número)"},
                {"value": "lt", "label": "Menor que (número)"},
            ]
        },
        help={
            "contains": "Valor o patrón a buscar.",
            "field": "Campo del objeto donde buscar (opcional).",
        },
    ),
    "transform.clean_html": NodeSpec(
        category="transform",
        runtime_fn="clean_html",
        optional={"url": ""},
        label="Limpiar HTML",
        output="texto plano sin etiquetas",
    ),
    "transform.html_extract": NodeSpec(
        category="transform",
        runtime_fn="html_extract",
        required=frozenset({"selector"}),
        optional={
            "fields": "",
            "output_format": "records",
            "item_template": "",
            "header_text": "",
            "base_url": "",
            "limit": 10,
        },
        label="Extraer con Selector CSS",
        output="lista de diccionarios, textos o informe formateado",
        choices={
            "output_format": [
                {"value": "records", "label": "Registros (lista de diccionarios)"},
                {"value": "text", "label": "Texto formateado (usando plantilla)"},
                {"value": "list", "label": "Lista simple de valores"},
            ],
        },
        help={
            "selector": "Selector CSS para los elementos contenedores (ej. 'div.new', 'article', 'h2 a').",
            "fields": "Mapeo JSON o clave=selector de subcampos (ej. {'title': 'h2 a', 'link': 'h2 a@href'}).",
            "output_format": "Formato de salida deseado.",
            "item_template": "Plantilla por elemento si salida es texto (ej. '### {title}\\n{link}\\n{summary}').",
            "header_text": "Encabezado opcional al inicio del texto formateado (admite {fecha}).",
            "base_url": "URL base para convertir enlaces relativos (/noticias/...) en absolutos.",
            "limit": "Número máximo de elementos a extraer (0 para todos).",
        },
    ),
    "transform.merge": NodeSpec(
        category="transform",
        runtime_fn="merge",
        label="Combinar entradas",
        output="lista con los resultados de las entradas",
    ),
    "transform.script": NodeSpec(
        category="transform",
        runtime_fn="script",
        required=frozenset({"code"}),
        label="Código Python",
        code_field="code",
        output="el valor retornado por tu código",
    ),
    # -- Salidas ------------------------------------------------------------
    "output.file": NodeSpec(
        category="output",
        runtime_fn="file",
        optional={
            "filename": "salida.md",
            "directory": "",
            "content": "inputs",
            "message": "",
            "mode": "sobrescribir",
            "format": "raw",
        },
        label="Guardar en fichero",
        output="ruta del fichero escrito",
        choices={
            "content": [
                {"value": "inputs", "label": "Contenido de las entradas"},
                {"value": "static", "label": "Mensaje fijo"},
            ],
            "mode": [
                {"value": "sobrescribir", "label": "Sobrescribir"},
                {"value": "añadir", "label": "Añadir al final"},
                {"value": "numerar", "label": "Numerar si ya existe"},
            ],
            "format": [
                {"value": "raw", "label": "Texto / Crudo"},
                {"value": "json_pretty", "label": "JSON Formateado"},
            ],
        },
        show_when={"message": {"content": ["static"]}},
        help={"filename": "Admite {fecha}, {hora}, {timestamp}."},
    ),
    "output.git": NodeSpec(
        category="output",
        runtime_fn="git_commit_push",
        optional={
            "repo_path": ".",
            "message": "update: automated commit from dedalo",
            "files": ".",
            "push": False,
            "branch": "main",
            "remote": "origin",
            "author_name": "Dedalo Bot",
            "author_email": "dedalo@local",
            "timeout": 30.0,
        },
        label="Publicar en Git",
        output="dict con commit_hash, message y estado de push",
        help={
            "repo_path": "Ruta local al repositorio Git.",
            "message": "Mensaje del commit a generar.",
            "files": "Rutas o patrones de archivos a añadir (por defecto . para todo el árbol).",
            "push": "Si es verdadero, ejecuta git push hacia el repositorio remoto configurado.",
            "branch": "Rama remota hacia la que hacer push (por defecto main).",
            "remote": "Nombre del remoto configurado (por defecto origin).",
            "author_name": "Nombre del autor del commit.",
            "author_email": "Correo del autor del commit.",
        },
    ),
    "output.sqlite_exec": NodeSpec(
        category="output",
        runtime_fn="sqlite_exec",
        required=frozenset({"query"}),
        optional={"path": "data/store.db"},
        label="Ejecutar en SQLite (INSERT/UPDATE)",
        output="número de filas afectadas",
        help={
            "query": "Sentencia SQL (INSERT INTO ... VALUES (:campo1, :campo2)).",
            "path": "Ruta de la base de datos SQLite.",
        },
    ),
    "output.email_smtp": NodeSpec(
        category="output",
        runtime_fn="email_smtp",
        required=frozenset({"to_addr"}),
        optional={
            "host": "smtp.gmail.com",
            "port": 587,
            "user": "",
            "secret": "",
            "from_addr": "",
            "subject": "Aviso de Dédalo",
            "use_tls": True,
            "is_html": False,
        },
        label="Enviar Correo (SMTP)",
        output="confirmación de envío",
        choices_from={"secret": "secrets"},
        help={
            "to_addr": "Dirección del destinatario.",
            "secret": "Secreto que contiene la contraseña o App Password.",
            "subject": "Asunto del correo electrónico.",
        },
    ),
    "output.ntfy": NodeSpec(
        category="output",
        runtime_fn="ntfy",
        required=frozenset({"topic"}),
        optional={
            "server": "https://ntfy.sh",
            "title": "Dédalo",
            "priority": "3",
            "tags": "robot",
            "secret": "",
        },
        label="Notificación Push (ntfy.sh)",
        output="confirmación de envío",
        choices={
            "priority": [
                {"value": "1", "label": "1 - Mínima"},
                {"value": "2", "label": "2 - Baja"},
                {"value": "3", "label": "3 - Normal"},
                {"value": "4", "label": "4 - Alta"},
                {"value": "5", "label": "5 - Urgente"},
            ]
        },
        choices_from={"secret": "secrets"},
        help={
            "topic": "Nombre del canal/topic de ntfy.",
            "tags": "Iconos o tags separados por comas (ej. warning,skull,bell).",
        },
    ),
    "output.discord": NodeSpec(
        category="output",
        runtime_fn="discord",
        optional={
            "webhook_url": "",
            "secret": "",
            "username": "Dédalo",
            "title": "",
            "content": "",
            "use_embed": False,
        },
        label="Discord Webhook",
        output="confirmación de envío",
        choices_from={"secret": "secrets"},
        help={
            "webhook_url": "URL del webhook de Discord.",
            "secret": "O selecciona un secreto con la URL.",
            "content": "Mensaje (por defecto el contenido de las entradas).",
        },
    ),
    "output.slack": NodeSpec(
        category="output",
        runtime_fn="slack",
        optional={
            "webhook_url": "",
            "secret": "",
            "channel": "",
            "text": "",
        },
        label="Slack Webhook",
        output="confirmación de envío",
        choices_from={"secret": "secrets"},
        help={
            "webhook_url": "URL del Incoming Webhook de Slack.",
            "secret": "O selecciona un secreto con la URL.",
            "text": "Mensaje a enviar (admite {entrada}).",
        },
    ),
    "output.mqtt": NodeSpec(
        category="output",
        runtime_fn="mqtt_publish",
        required=frozenset({"topic"}),
        optional={
            "broker": "localhost",
            "port": 1883,
            "payload": "{entrada}",
            "retain": False,
            "qos": 0,
            "username": "",
            "secret": "",
            "timeout": 5.0,
        },
        label="MQTT Publicar",
        output="confirmación de publicación",
        choices_from={"secret": "secrets"},
        help={
            "topic": "Topic MQTT donde publicar.",
            "payload": "Contenido del mensaje (admite {entrada}).",
            "broker": "Dirección del broker (ej. localhost o 192.168.1.50).",
            "secret": "Secreto con la contraseña si el broker requiere auth.",
        },
    ),
    "output.command": NodeSpec(
        category="output",
        runtime_fn="command_action",
        required=frozenset({"cmd"}),
        optional={"timeout": 15.0, "cwd": ""},
        label="Ejecutar Acción (Shell)",
        output="dict con stdout, stderr, exit_code",
        help={
            "cmd": "Comando de acción a ejecutar en la shell (ej. systemctl restart servicio).",
        },
    ),
    "output.rest_webhook": NodeSpec(
        category="output",
        runtime_fn="rest_webhook",
        required=frozenset({"url"}),
        optional={
            "method": "POST",
            "body": "",
            "headers": "",
            "secret": "",
            "auth_header": "Authorization",
        },
        label="Webhook REST",
        output="confirmación de envío",
        choices={"method": ["POST", "PUT", "PATCH"]},
        choices_from={"secret": "secrets"},
        help={
            "url": "Destino del webhook (Discord, Slack, n8n, etc.).",
            "body": "JSON a enviar. Usa {entrada} para inyectar los datos.",
        },
    ),
    "output.telegram": NodeSpec(
        category="output",
        runtime_fn="telegram",
        optional={
            "chat_id": "",
            "token_secret": "telegram-token",
            "parse_mode": "HTML",
            "silent": False,
        },
        label="Telegram",
        output="confirmación de envío",
        choices={
            "parse_mode": [
                {"value": "HTML", "label": "HTML"},
                {"value": "Markdown", "label": "Markdown"},
                {"value": "texto", "label": "Texto sin formato"},
            ]
        },
        choices_from={"token_secret": "secrets"},
        help={
            "token_secret": "Nombre del secreto que contiene el token del bot.",
            "chat_id": "ID del chat o canal receptor.",
        },
    ),
    "output.healthcheck": NodeSpec(
        category="output",
        runtime_fn="healthcheck_ping",
        required=frozenset({"url"}),
        optional={
            "method": "GET",
            "send_status": "auto",
            "send_payload": False,
        },
        label="Ping de Monitorización (Heartbeat)",
        output="confirmación del ping HTTP (dict)",
        choices={
            "method": [
                {"value": "GET", "label": "GET"},
                {"value": "POST", "label": "POST"},
            ],
            "send_status": [
                {"value": "auto", "label": "Automático (éxito si nodos previos OK, fallo si error)"},
                {"value": "success", "label": "Forzar señal de éxito"},
                {"value": "fail", "label": "Forzar señal de fallo"},
            ],
        },
        help={
            "url": "URL de ping (Healthchecks.io, Uptime Kuma push o similar).",
            "method": "Método HTTP para la petición de heartbeat.",
            "send_status": "Estado a reportar al monitor.",
            "send_payload": "Si es True, envía el payload o extracto de datos en el cuerpo POST.",
        },
    ),
    "output.alert": NodeSpec(
        category="output",
        runtime_fn="alert",
        optional={"text": "", "urgency": "info"},
        label="Registro / Alerta",
        output="línea registrada",
        choices={
            "urgency": [
                {"value": "info", "label": "Informativo"},
                {"value": "alerta", "label": "Alerta"},
                {"value": "error", "label": "Crítico"},
            ]
        },
    ),
}


def _topological_order(graph: Graph) -> list[Node] | None:
    """Algoritmo de Kahn estable para obtener el orden de ejecución de los nodos."""
    remaining = {n.id: n for n in graph.nodes}
    indegree = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        if e.target in indegree:
            indegree[e.target] += 1

    ordered: list[Node] = []
    while remaining:
        ready = [n for n in graph.nodes if n.id in remaining and indegree[n.id] == 0]
        if not ready:
            return None  # Ciclo detectado
        chosen = ready[0]
        ordered.append(chosen)
        del remaining[chosen.id]
        for e in graph.edges:
            if e.source == chosen.id and e.target in indegree:
                indegree[e.target] -= 1
    return ordered


def get_upstream_nodes(graph: Graph, target_node_id: str) -> list[Node]:
    """Devuelve los nodos necesarios para ejecutar target_node_id en orden topológico."""
    full_order = _topological_order(graph)
    if not full_order:
        return []

    needed: set[str] = {target_node_id}
    queue = [target_node_id]
    while queue:
        curr = queue.pop(0)
        for e in graph.incoming(curr):
            if e.source not in needed:
                needed.add(e.source)
                queue.append(e.source)

    return [n for n in full_order if n.id in needed]


def get_node_inputs_from_results(graph: Graph, node_id: str, results: dict[str, Any]) -> list[Any]:
    """Obtiene las entradas de un nodo a partir de los resultados previos de sus fuentes."""
    incoming = graph.incoming(node_id)
    inputs_list = []
    for edge in incoming:
        if edge.source in results:
            inputs_list.append(results[edge.source])
    return inputs_list


def validate(graph: Graph) -> list[str]:
    """Comprueba todos los problemas del grafo antes de compilarlo."""
    errors: list[str] = []
    node_ids = {n.id for n in graph.nodes}

    seen_ids: set[str] = set()
    for n in graph.nodes:
        if n.id in seen_ids:
            errors.append(f"id de nodo repetido: «{n.id}»")
        seen_ids.add(n.id)
        if not _ID_RE.match(n.id):
            errors.append(
                f"id de nodo inválido: «{n.id}» -- debe ser un identificador Python válido"
            )
        if n.type not in NODE_TYPES:
            errors.append(f"«{n.id}»: tipo de nodo desconocido «{n.type}»")
            continue
        spec = NODE_TYPES[n.type]
        missing = spec.required - set(n.config)
        if missing:
            errors.append(
                f"«{n.id}» ({n.type}): falta configuración obligatoria: {', '.join(sorted(missing))}"
            )

    for e in graph.edges:
        if e.source not in node_ids:
            errors.append(f"arista con origen desconocido: «{e.source}»")
        if e.target not in node_ids:
            errors.append(f"arista con destino desconocido: «{e.target}»")
        if e.port not in PORTS:
            errors.append(f"puerto de arista inválido: «{e.port}» (solo {PORTS})")

    if _topological_order(graph) is None:
        errors.append("el grafo contiene un ciclo -- no se puede ejecutar en ningún orden")

    # Validación de referencias en nodos script (entrada["id"])
    for n in graph.nodes:
        spec = NODE_TYPES.get(n.type)
        if not spec or not spec.code_field:
            continue
        sources = {e.source for e in graph.incoming(n.id)}
        for ref in _ENTRADA_REF_RE.findall(n.config.get(spec.code_field, "") or ""):
            if ref and ref not in sources:
                errors.append(
                    f"«{n.id}»: el código lee entrada[«{ref}»], pero ese nodo no está conectado. Conectados: {', '.join(sorted(sources)) or 'ninguno'}"
                )

    for n in graph.nodes:
        incoming = graph.incoming(n.id)
        if len({e.source for e in incoming}) > 1:
            policy = n.config.get("fan_in")
            if policy not in FAN_IN_POLICIES:
                errors.append(
                    f"«{n.id}»: tiene varias aristas entrantes y requiere config.fan_in = 'all' o 'any'"
                )

    return errors


def _guard_expression(graph: Graph, node: Node) -> str | None:
    """Expresión condicional para determinar si el nodo debe ejecutarse."""
    incoming = graph.incoming(node.id)
    if not incoming:
        return None

    def term(edge: Edge) -> str:
        attr = "ok" if edge.port == "ok" else "failed"
        return f"r_{edge.source}.{attr}"

    if len(incoming) == 1:
        return term(incoming[0])

    policy = node.config.get("fan_in", "all")
    joiner = " and " if policy == "all" else " or "
    return "(" + joiner.join(term(e) for e in incoming) + ")"


def _inputs_expression(graph: Graph, node: Node) -> str:
    incoming = graph.incoming(node.id)
    return "[" + ", ".join(f"r_{e.source}" for e in incoming) + "]"


def _render_config_kwargs(node: Node, spec: NodeSpec) -> str:
    values = {**spec.optional, **node.config}
    values.pop("fan_in", None)
    values.pop(spec.code_field, None)
    return ", ".join(f"{k}={v!r}" for k, v in sorted(values.items()))


def _render_code_node(graph: Graph, node: Node, spec: NodeSpec) -> list[str]:
    body = textwrap.dedent(node.config.get(spec.code_field, "") or "").strip("\n")
    sources = [e.source for e in graph.incoming(node.id)]
    lines = [f"def node_{node.id}(ctx, inputs):", "    def _cuerpo(entrada, ctx):"]
    lines += [f"        {line}" if line.strip() else "" for line in body.splitlines()] or [
        "        return None"
    ]
    lines.append(f"    return runtime.script(ctx, inputs, _cuerpo, sources={sources!r})")
    return lines


def compile(graph: Graph) -> str:
    """Compila el grafo a código fuente Python `run(ctx)`."""
    errors = validate(graph)
    if errors:
        raise GraphError(errors)

    order = _topological_order(graph)
    assert order is not None

    lines: list[str] = [
        "# GENERADO POR DÉDALO -- no editar a mano, se regenera desde el grafo.",
        f"# dedalo.VERSION = {VERSION}",
        "from dedalo import runtime",
        "from dedalo.runtime import SKIPPED",
        "",
        "",
    ]

    for node in order:
        spec = NODE_TYPES[node.type]
        if spec.code_field:
            lines += _render_code_node(graph, node, spec)
        else:
            kwargs = _render_config_kwargs(node, spec)
            call = f"runtime.{spec.runtime_fn}(ctx, inputs" + (
                f", {kwargs})" if kwargs else ")"
            )
            lines.append(f"def node_{node.id}(ctx, inputs):")
            lines.append(f"    return {call}")
        lines.append("")
        lines.append("")

    lines.append("def run(ctx):")
    for node in order:
        guard = _guard_expression(graph, node)
        inputs_expr = _inputs_expression(graph, node)
        call = f"node_{node.id}(ctx, {inputs_expr})"
        if guard is None:
            lines.append(f"    r_{node.id} = {call}")
        else:
            lines.append(f"    r_{node.id} = {call} if {guard} else SKIPPED")

    results_dict = "{" + ", ".join(f"{n.id!r}: r_{n.id}" for n in order) + "}"
    lines.append(f"    return runtime.summarize({results_dict}, ctx)")
    lines.append("")

    source = "\n".join(lines)
    try:
        ast.parse(source)
        _compile_source(source, "<dedalo>", "exec")
    except SyntaxError as exc:
        raise GraphError([f"el código generado no es Python válido: {exc}"]) from exc

    return source
