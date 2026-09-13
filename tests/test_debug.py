import pytest
from fastapi.testclient import TestClient
from dedalo import history, runtime
from dedalo.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_execute_single_node_html_extract():
    html_doc = """
    <html>
        <body>
            <ul class="games">
                <li><a class="title" href="/game1">Catan</a><span class="price">35€</span></li>
                <li><a class="title" href="/game2">Carcassonne</a><span class="price">25€</span></li>
            </ul>
        </body>
    </html>
    """
    res = runtime.execute_single_node(
        node_type="transform.html_extract",
        config={
            "selector": "a.title",
            "limit": 10,
        },
        inputs=html_doc,
    )
    assert res.ok
    assert res.data == ["Catan", "Carcassonne"]
    assert res.duration >= 0


def test_execute_single_node_regex():
    text = "Pedido #12345 con importe 99€ y pedido #67890 con importe 150€"
    res = runtime.execute_single_node(
        node_type="transform.regex",
        config={
            "pattern": r"#\d+",
            "action": "find_all",
        },
        inputs=text,
    )
    assert res.ok
    assert res.data == ["#12345", "#67890"]


def test_execute_single_node_script():
    data = [{"title": "Juego A", "price": 40}, {"title": "Juego B", "price": 20}]
    code = """
items = entrada.all_ok()
return [item["title"].upper() for item in items if item["price"] > 25]
"""
    res = runtime.execute_single_node(
        node_type="transform.script",
        code=code,
        inputs=data,
    )
    assert res.ok
    assert res.data == ["JUEGO A"]


def test_api_node_preview(client):
    resp = client.post(
        "/api/node/preview",
        json={
            "type": "transform.template",
            "config": {"template": "Hola {nombre}, tienes {items} mensajes"},
            "input_data": {"nombre": "Raúl", "items": 5},
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "Hola Raúl, tienes 5 mensajes" in data["data"]


def test_api_flow_step_and_run_to(client):
    # Crear un grafo: input.command -> transform.regex -> transform.template
    graph = {
        "name": "test-debug-flow",
        "nodes": [
            {
                "id": "cmd_1",
                "type": "input.command",
                "label": "Eco",
                "config": {"cmd": "echo 'Temperatura: 42.5C'"},
                "x": 0,
                "y": 0,
            },
            {
                "id": "regex_1",
                "type": "transform.regex",
                "label": "Extraer Temp",
                "config": {"pattern": r"\d+\.\d+", "action": "find_all"},
                "x": 100,
                "y": 0,
            },
            {
                "id": "tpl_1",
                "type": "transform.template",
                "label": "Formatear",
                "config": {"template": "Alerta: {entrada}"},
                "x": 200,
                "y": 0,
            },
        ],
        "edges": [
            {"source": "cmd_1", "target": "regex_1", "port": "ok"},
            {"source": "regex_1", "target": "tpl_1", "port": "ok"},
        ],
    }

    # 1. Probar "run_to" hasta regex_1
    resp = client.post(
        "/api/flow/step",
        json={
            "graph": graph,
            "target_node_id": "regex_1",
            "mode": "run_to",
            "cached_results": {},
        },
    )
    assert resp.status_code == 200
    res_data = resp.json()
    assert res_data["status"] == "ok"
    assert "cmd_1" in res_data["all_results"]
    assert "regex_1" in res_data["all_results"]
    assert res_data["all_results"]["regex_1"]["data"] == ["42.5"]
    assert "Temperatura: 42.5C" in str(res_data["all_results"]["regex_1"]["inputs"])

    cached = res_data["all_results"]

    # 2. Probar "step" únicamente en tpl_1 usando cached_results
    resp2 = client.post(
        "/api/flow/step",
        json={
            "graph": graph,
            "target_node_id": "tpl_1",
            "mode": "step",
            "cached_results": cached,
        },
    )
    assert resp2.status_code == 200
    res2_data = resp2.json()
    assert res2_data["status"] == "ok"
    assert "Alerta:" in str(res2_data["step"]["data"])
    assert "42.5" in str(res2_data["step"]["data"])
    assert res2_data["step"]["inputs"] == ["42.5"]


def test_api_run_with_inputs_and_history(client, tmp_path, monkeypatch):
    test_db = tmp_path / "test_history.db"
    monkeypatch.setattr(history, "DB_PATH", test_db)

    graph = {
        "name": "flow-full-trace",
        "nodes": [
            {
                "id": "cmd_1",
                "type": "input.command",
                "label": "Eco",
                "config": {"cmd": "echo 'salida-uno'"},
                "x": 0,
                "y": 0,
            },
            {
                "id": "tpl_1",
                "type": "transform.template",
                "label": "Tpl",
                "config": {"template": "Prefijo: {entrada}"},
                "x": 100,
                "y": 0,
            },
        ],
        "edges": [
            {"source": "cmd_1", "target": "tpl_1", "port": "ok"},
        ],
    }

    resp = client.post("/api/run", json={"graph": graph})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"

    # Verificar que /api/run incluye inputs para cada nodo
    results = data["results"]
    assert "cmd_1" in results
    assert "tpl_1" in results
    assert "salida-uno" in str(results["tpl_1"]["inputs"])

    # Verificar que el historial SQLite guarda inputs completos
    run_id = data["run_id"]
    run_detail = history.get_run(run_id, db_path=test_db)
    assert run_detail is not None
    assert len(run_detail["steps"]) == 2
    step_tpl = next(s for s in run_detail["steps"] if s["node_id"] == "tpl_1")
    assert step_tpl["inputs"] is not None
    assert "salida-uno" in step_tpl["inputs"]

