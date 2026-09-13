"""Tests para el compilador de grafos de Dédalo Standalone."""

import pytest
from dedalo import compiler


def _node(nid: str, ntype: str, config: dict = None) -> compiler.Node:
    return compiler.Node(id=nid, type=ntype, config=config or {})


def _edge(src: str, tgt: str, port: str = "ok") -> compiler.Edge:
    return compiler.Edge(source=src, target=tgt, port=port)


def test_linear_chain():
    g = compiler.Graph(
        name="cadena",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "transform.threshold", {"comparison": "menor_que", "limit": 20}),
            _node("n3", "output.alert", {"text": "alerta"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n3")],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "r_n1 = node_n1(ctx, [])" in src
    assert "r_n2 = node_n2(ctx, [r_n1]) if r_n1.ok else SKIPPED" in src
    assert "r_n3 = node_n3(ctx, [r_n2]) if r_n2.ok else SKIPPED" in src


def test_fan_in_policies():
    g_all = compiler.Graph(
        name="fan_all",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "input.cpu_load"),
            _node("n3", "transform.merge", {"fan_in": "all"}),
        ],
        edges=[_edge("n1", "n3"), _edge("n2", "n3")],
    )
    src_all = compiler.compile(g_all)
    assert "(r_n1.ok and r_n2.ok)" in src_all

    g_any = compiler.Graph(
        name="fan_any",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "input.cpu_load"),
            _node("n3", "transform.merge", {"fan_in": "any"}),
        ],
        edges=[_edge("n1", "n3"), _edge("n2", "n3")],
    )
    src_any = compiler.compile(g_any)
    assert "(r_n1.ok or r_n2.ok)" in src_any


def test_cycle_rejected():
    g = compiler.Graph(
        name="ciclo",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "transform.threshold", {"comparison": "menor_que", "limit": 20}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n1")],
    )
    with pytest.raises(compiler.GraphError) as exc:
        compiler.compile(g)
    assert any("ciclo" in e for e in exc.value.errors)


def test_script_node_validation():
    g = compiler.Graph(
        name="script_val",
        nodes=[
            _node("fuente", "input.disk_free"),
            _node("proc", "transform.script", {"code": "return entrada['no_existe']"}),
        ],
        edges=[_edge("fuente", "proc")],
    )
    with pytest.raises(compiler.GraphError) as exc:
        compiler.compile(g)
    assert any("no está conectado" in e for e in exc.value.errors)


def test_new_nodes_compile_and_execute(tmp_path):
    db_file = str(tmp_path / "comp_test.db")
    g = compiler.Graph(
        name="test_new_nodes",
        nodes=[
            _node("hook", "input.webhook"),
            _node("tmpl", "transform.template", {"template": "Evento: {evento}"}),
            _node("router", "transform.router", {"condition": "contains", "value": "alerta"}),
            _node("store", "output.sqlite_exec", {"path": db_file, "query": "CREATE TABLE IF NOT EXISTS t (m TEXT)"}),
        ],
        edges=[
            _edge("hook", "tmpl", "ok"),
            _edge("tmpl", "router", "ok"),
            _edge("router", "store", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.webhook_trigger" in src
    assert "runtime.template" in src
    assert "runtime.router" in src
    assert "runtime.sqlite_exec" in src

    # Ejecutar el código compilado
    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_web_search_node_compiles():
    g = compiler.Graph(
        name="test_search",
        nodes=[
            _node("buscar", "input.web_search", {"query": "noticias python", "engine": "duckduckgo", "limit": 3}),
            _node("guardar", "output.file", {"filename": "noticias.txt", "mode": "sobrescribir"}),
        ],
        edges=[
            _edge("buscar", "guardar", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.web_search" in src
    assert "query='noticias python'" in src
    assert "engine='duckduckgo'" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])

