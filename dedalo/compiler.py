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
    # -- Transformaciones ---------------------------------------------------
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
            "tokens": DEFAULT_AGENT_TOKENS,
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
            "model": "Nombre del modelo (vacío usa el configurado por defecto).",
            "url": "URL base de la API OpenAI (vacío usa la global).",
            "secret": "Secreto con la API Key (si aplica).",
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
