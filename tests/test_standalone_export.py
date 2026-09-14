"""Tests para la exportación de flujos a scripts Python autónomos (.py)."""

import ast
import json
import subprocess
import sys
from fastapi.testclient import TestClient

from dedalo import compiler, store
from dedalo.app import app


def _node(nid: str, ntype: str, config: dict = None) -> compiler.Node:
    return compiler.Node(id=nid, type=ntype, config=config or {})


def _edge(src: str, tgt: str, port: str = "ok") -> compiler.Edge:
    return compiler.Edge(source=src, target=tgt, port=port)


def test_standalone_basic_compilation():
    g = compiler.Graph(
        name="flujo_basico",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "transform.threshold", {"comparison": "menor_que", "limit": 20}),
            _node("n3", "output.alert", {"text": "alerta de espacio"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n3")],
    )
    src = compiler.compile_standalone(g)

    # Debe ser Python sintácticamente válido
    tree = ast.parse(src)
    assert tree is not None

    # Metadatos PEP 723 y docstring
    assert "# /// script" in src
    assert "# requires-python = \">=3.11\"" in src
    assert "Flujo Dédalo: flujo_basico" in src

    # Switches estándar
    assert "--create-venv" in src
    assert "--json" in src
    assert "--verbose" in src
    assert "--input" in src
    assert "def setup_virtual_environment" in src

    # NO debe incluir switches de LLM ni de secretos porque este flujo no los requiere
    assert "--llm-url" not in src
    assert "--llm-model" not in src
    assert "--secret-" not in src

    # CERO dependencia de Dédalo
    assert "from dedalo import" not in src
    assert '"dedalo"' not in src


def test_standalone_zero_dedalo_dependency():
    g = compiler.Graph(
        name="flujo_sin_dedalo",
        nodes=[
            _node("n1", "input.http_call", {"url": "https://example.com"}),
            _node("n2", "transform.html_extract", {"selector": "h1"}),
            _node("n3", "output.file", {"filename": "test.txt"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n3")],
    )
    src = compiler.compile_standalone(g)
    ast.parse(src)

    assert "from dedalo" not in src
    assert "import dedalo" not in src
    assert '"httpx>=0.27.0"' in src
    assert '"dedalo"' not in src



def test_standalone_with_secrets():
    g = compiler.Graph(
        name="flujo_secretos",
        nodes=[
            _node("n1", "input.webhook"),
            _node("n2", "output.telegram", {"chat_id": "12345", "token_secret": "bot_telegram"}),
            _node("n3", "output.discord", {"secret": "discord_hook"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n1", "n3")],
    )
    src = compiler.compile_standalone(g)
    ast.parse(src)

    # Comprobar switches dinámicos de secretos
    assert "--secret-bot-telegram" in src
    assert "DEDALO_SECRET_BOT_TELEGRAM" in src
    assert "--secret-discord-hook" in src
    assert "DEDALO_SECRET_DISCORD_HOOK" in src

    # No tiene LLM
    assert "--llm-url" not in src


def test_standalone_with_llm():
    g = compiler.Graph(
        name="flujo_ia",
        nodes=[
            _node("n1", "input.webhook"),
            _node("n2", "transform.agent", {"prompt": "Resume el texto"}),
        ],
        edges=[_edge("n1", "n2")],
    )
    src = compiler.compile_standalone(g)
    ast.parse(src)

    # Switches de LLM
    assert "--llm-url" in src
    assert "--llm-model" in src
    assert "--llm-key" in src
    assert "--llm-temperature" in src
    assert "DEDALO_LLM_URL" in src
    assert "DEDALO_LLM_MODEL" in src


def test_standalone_cli_help_execution(tmp_path):
    g = compiler.Graph(
        name="test_cli_help",
        nodes=[
            _node("n1", "input.webhook"),
            _node("n2", "output.telegram", {"chat_id": "99", "token_secret": "token_bot"}),
            _node("n3", "transform.agent", {"prompt": "Hola"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n1", "n3")],
    )
    src = compiler.compile_standalone(g)
    script_path = tmp_path / "flujo_test.py"
    script_path.write_text(src, encoding="utf-8")

    # Ejecutar python flujo_test.py --help
    res = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "--create-venv" in res.stdout
    assert "--secret-token-bot" in res.stdout
    assert "--llm-url" in res.stdout


def test_standalone_cli_execution_with_payload(tmp_path):
    g = compiler.Graph(
        name="test_cli_run",
        nodes=[
            _node("hook", "input.webhook"),
            _node("tmpl", "transform.template", {"template": "Recibido: {mensaje}"}),
        ],
        edges=[_edge("hook", "tmpl")],
    )
    src = compiler.compile_standalone(g)
    script_path = tmp_path / "flujo_run.py"
    script_path.write_text(src, encoding="utf-8")

    # Ejecutar pasando JSON con --input y flag --json
    payload = json.dumps({"mensaje": "Hola Mundo"})
    res = subprocess.run(
        [sys.executable, str(script_path), "--json", "-i", payload],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert "hook" in data
    assert "tmpl" in data
    assert data["tmpl"]["status"] == "ok"
    assert data["tmpl"]["data"] == "Recibido: Hola Mundo"


def test_api_export_standalone_py_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FLOWS_DIR", tmp_path / "flows")
    client = TestClient(app)

    flow_payload = {
        "name": "flujo_exportable",
        "nodes": [
            {"id": "n1", "type": "input.disk_free", "config": {}, "x": 0, "y": 0},
            {"id": "n2", "type": "output.alert", "config": {"text": "Espacio ok"}, "x": 100, "y": 0},
        ],
        "edges": [{"source": "n1", "target": "n2", "port": "ok"}],
    }

    # Guardar flujo
    save_res = client.post(
        "/api/flow",
        json={"id": "flujo-exportable", "graph": flow_payload},
    )
    assert save_res.status_code == 200

    # Exportar vía GET /api/flows/{flow_id}/export-py
    res = client.get("/api/flows/flujo-exportable/export-py")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/x-python")
    assert "attachment; filename=\"flujo-exportable.py\"" in res.headers["content-disposition"]
    assert "--create-venv" in res.text
    assert "def run(ctx):" in res.text

    # Probar endpoint alternativo con query param /api/flow/export-py?id=...
    res_alt = client.get("/api/flow/export-py?id=flujo-exportable")
    assert res_alt.status_code == 200
    assert res_alt.text == res.text
