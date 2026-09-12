"""Plantillas de inicio para Dédalo Standalone."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    name: str
    title: str
    description: str
    teaches: str
    graph: dict


def _graph(name: str, nodes: list[dict], edges: list[dict]) -> dict:
    return {"version": 1, "name": name, "nodes": nodes, "edges": edges}


def _node(node_id: str, node_type: str, config: dict | None = None, x: int = 0, y: int = 0, label: str = "") -> dict:
    return {"id": node_id, "type": node_type, "label": label, "config": config or {}, "x": x, "y": y}


def _edge(source: str, target: str, port: str = "ok") -> dict:
    return {"source": source, "target": target, "port": port}


TEMPLATES: tuple[Template, ...] = (
    Template(
        name="haiku-diario",
        title="Haiku con LLM",
        description="Genera un haiku mediante un LLM externo y lo guarda en un archivo.",
        teaches="Lo mínimo que puede ser un flujo con IA.",
        graph=_graph(
            "haiku-diario",
            [
                _node(
                    "poeta",
                    "transform.agent",
                    {
                        "prompt": "Eres un poeta. Escribe un haiku breve y evocador sobre el amanecer o la naturaleza. Responde únicamente con el haiku.",
                    },
                    x=80,
                    y=120,
                ),
                _node(
                    "guardar",
                    "output.file",
                    {
                        "filename": "haiku-{fecha}.md",
                        "mode": "sobrescribir",
                    },
                    x=450,
                    y=120,
                ),
            ],
            [_edge("poeta", "guardar")],
        ),
    ),
    Template(
        name="vigilancia-disco",
        title="Vigilancia de disco",
        description="Comprueba el espacio libre y solo avisa si baja del 10%.",
        teaches="Flujos de supervisión silenciosa con nodos de umbral (threshold).",
        graph=_graph(
            "vigilancia-disco",
            [
                _node("disco", "input.disk_free", {}, x=80, y=120),
                _node(
                    "limite",
                    "transform.threshold",
                    {"comparison": "menor_que", "limit": 10},
                    x=350,
                    y=120,
                ),
                _node(
                    "aviso",
                    "output.alert",
                    {"text": "Alerta crítica: Espacio en disco inferior al 10%", "urgency": "error"},
                    x=620,
                    y=120,
                ),
            ],
            [_edge("disco", "limite"), _edge("limite", "aviso")],
        ),
    ),
    Template(
        name="resumen-web",
        title="Resumidor de páginas web",
        description="Descarga una web, limpia el HTML y crea un resumen.",
        teaches="Pipeline de transformación encadenado.",
        graph=_graph(
            "resumen-web",
            [
                _node(
                    "pagina",
                    "input.web_read",
                    {"url": "https://example.com", "length": "medium"},
                    x=80,
                    y=120,
                ),
                _node(
                    "resumir",
                    "transform.summarize",
                    {"engine": "luhn", "length": "short"},
                    x=380,
                    y=120,
                ),
                _node(
                    "salida",
                    "output.file",
                    {"filename": "resumen.txt", "mode": "sobrescribir"},
                    x=680,
                    y=120,
                ),
            ],
            [_edge("pagina", "resumir"), _edge("resumir", "salida")],
        ),
    ),
    Template(
        name="scanner-noticias-rss",
        title="Escáner de noticias RSS",
        description="Lee las noticias nuevas de un feed RSS y las filtra por palabras clave.",
        teaches="Manejo de estado persistente entre ejecuciones y filtrado de listas.",
        graph=_graph(
            "scanner-noticias-rss",
            [
                _node(
                    "feed",
                    "input.rss",
                    {"url": "https://feeds.bbci.co.uk/news/world/rss.xml", "only_new": True},
                    x=80,
                    y=120,
                ),
                _node(
                    "filtrar",
                    "transform.filter",
                    {"contains": "tech", "field": "title"},
                    x=380,
                    y=120,
                ),
                _node(
                    "almacenar",
                    "output.file",
                    {"filename": "noticias-tecnologia.json", "mode": "añadir"},
                    x=680,
                    y=120,
                ),
            ],
            [_edge("feed", "filtrar"), _edge("filtrar", "almacenar")],
        ),
    ),
)


def list_templates() -> list[dict]:
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "teaches": t.teaches,
        }
        for t in TEMPLATES
    ]


def get_template(name: str) -> dict | None:
    for t in TEMPLATES:
        if t.name == name:
            return t.graph
    return None
