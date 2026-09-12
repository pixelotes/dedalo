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
    "input.http_call": NodeSpec(
        category="input",
        runtime_fn="http_call",
        required=frozenset({"url"}),
        optional={
            "method": "GET",
            "headers": "",
            "secret": "",
            "auth_header": "Authorization",
        },
        label="Traer de una API",
        output="dict si devuelve JSON, texto si no",
        choices={"method": ["GET", "POST", "PUT", "DELETE"]},
        choices_from={"secret": "secrets"},
        help={
            "url": "URL de la API REST a consultar.",
            "secret": "Nombre del secreto con el token de autenticación.",
            "auth_header": "Cabecera donde va el secreto (por defecto Authorization).",
        },
    ),
    "input.rss": NodeSpec(
        category="input",
        runtime_fn="rss",
        required=frozenset({"url"}),
        optional={"only_new": False, "corte": "completo"},
        label="Feed RSS / Atom",
        output="lista de noticias (title, link, guid, date, description)",
        choices={
            "corte": [
                {"value": "300", "label": "300 caracteres"},
                {"value": "600", "label": "600 caracteres"},
                {"value": "1000", "label": "1000 caracteres"},
                {"value": "completo", "label": "Completo"},
            ]
        },
        help={
            "only_new": "Salta las noticias ya vistas en ejecuciones anteriores.",
            "corte": "Límite de caracteres por descripción.",
        },
    ),
    "input.web_read": NodeSpec(
        category="input",
        runtime_fn="web_read",
        required=frozenset({"url"}),
        optional={"length": "medium"},
        label="Extraer página web",
        output="texto limpio y condensado",
        choices={"length": ["short", "medium", "long", "full"]},
    ),
    "input.csv_file": NodeSpec(
        category="input",
        runtime_fn="csv_file",
        required=frozenset({"path"}),
        label="Fichero CSV",
        output="lista de diccionarios (filas)",
    ),
    "input.json_file": NodeSpec(
        category="input",
        runtime_fn="json_file",
        required=frozenset({"path"}),
        label="Fichero JSON",
        output="dict o lista según el archivo",
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
        },
        label="Agente LLM",
        output="texto generado; lista si per_item",
        choices_from={"secret": "secrets"},
        help={
            "prompt": "Instrucciones para el LLM.",
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
            "engine": "luhn",
            "length": "medium",
            "prompt": "",
        },
        label="Resumir texto",
        output="texto resumido",
        choices={
            "engine": [
                {"value": "luhn", "label": "Luhn (extractivo determinista)"},
                {"value": "llm", "label": "LLM (generativo)"},
            ],
            "length": ["short", "medium", "long", "full"],
        },
        show_when={"prompt": {"engine": ["llm"]}},
    ),
    "transform.threshold": NodeSpec(
        category="transform",
        runtime_fn="threshold",
        required=frozenset({"comparison", "limit"}),
        label="Comparar con límite",
        output="número o SKIPPED si no se cumple",
        choices={
            "comparison": [
                {"value": "menor_que", "label": "menor que"},
                {"value": "menor_o_igual", "label": "menor o igual que"},
                {"value": "mayor_que", "label": "mayor que"},
                {"value": "mayor_o_igual", "label": "mayor o igual que"},
            ]
        },
        help={"limit": "Si no se cumple la condición, el flujo se detiene aquí en silencio."},
    ),
    "transform.filter": NodeSpec(
        category="transform",
        runtime_fn="filter_items",
        required=frozenset({"contains"}),
        optional={"field": ""},
        label="Filtrar elementos",
        output="lista filtrada",
    ),
    "transform.clean_html": NodeSpec(
        category="transform",
        runtime_fn="clean_html",
        optional={"url": ""},
        label="Limpiar HTML",
        output="texto plano sin etiquetas",
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
        },
        show_when={"message": {"content": ["static"]}},
        help={"filename": "Admite {fecha} (año-mes-día)."},
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
        optional={"chat_id": "", "token_secret": "telegram-token"},
        label="Telegram",
        output="confirmación de envío",
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
