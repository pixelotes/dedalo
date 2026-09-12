"""Tests para la API FastAPI de Dédalo Standalone."""

from fastapi.testclient import TestClient
from dedalo.app import app

client = TestClient(app)


def test_get_catalog():
    res = client.get("/api/catalog")
    assert res.status_code == 200
    data = res.json()
    assert "node_types" in data
    assert "input.http_call" in data["node_types"]
    assert "transform.agent" in data["node_types"]
    assert "output.file" in data["node_types"]


def test_get_templates():
    res = client.get("/api/templates")
    assert res.status_code == 200
    data = res.json()
    assert len(data) >= 3
    assert any(t["name"] == "haiku-diario" for t in data)


def test_compile_api():
    graph = {
        "version": 1,
        "name": "test_api_flow",
        "nodes": [
            {"id": "disco", "type": "input.disk_free", "config": {}},
            {"id": "aviso", "type": "output.alert", "config": {"text": "ok"}},
        ],
        "edges": [
            {"source": "disco", "target": "aviso", "port": "ok"},
        ],
    }
    res = client.post("/api/compile", json={"graph": graph})
    assert res.status_code == 200
    assert "def run(ctx):" in res.json()["code"]


def test_run_preview_api():
    graph = {
        "version": 1,
        "name": "test_run",
        "nodes": [
            {"id": "disco", "type": "input.disk_free", "config": {}},
        ],
        "edges": [],
    }
    res = client.post("/api/run", json={"graph": graph})
    assert res.status_code == 200
    data = res.json()
    assert "run_id" in data
    assert "disco" in data["results"]
    assert data["results"]["disco"]["status"] == "ok"
    assert isinstance(data["results"]["disco"]["data"], (int, float))

    # Test history listing
    h_res = client.get("/api/history?flow_id=test_run")
    assert h_res.status_code == 200
    runs = h_res.json()
    assert len(runs) >= 1
    assert runs[0]["id"] == data["run_id"]
    assert runs[0]["status"] == "ok"

    # Test history run detail
    r_res = client.get(f"/api/history/run?id={data['run_id']}")
    assert r_res.status_code == 200
    detail = r_res.json()
    assert detail["id"] == data["run_id"]
    assert len(detail["steps"]) >= 1
    assert detail["steps"][0]["node_id"] == "disco"
    assert detail["steps"][0]["status"] == "ok"

    # Test history purge endpoint
    p_res = client.post("/api/history/purge?days=30")
    assert p_res.status_code == 200
    assert "deleted" in p_res.json()


def test_webhook_api():
    # 1. Guardar un flujo con input.webhook -> transform.template
    flow_id = "test-hook-flow"
    graph = {
        "version": 1,
        "name": flow_id,
        "nodes": [
            {"id": "hook", "type": "input.webhook", "config": {}},
            {"id": "tmpl", "type": "transform.template", "config": {"template": "Recibido de {sensor}: {valor}C"}},
        ],
        "edges": [
            {"source": "hook", "target": "tmpl", "port": "ok"},
        ],
    }
    save_res = client.post("/api/flow", json={"id": flow_id, "graph": graph, "meta": {}})
    assert save_res.status_code == 200

    # 2. Llamar al webhook con datos JSON
    hook_res = client.post(f"/api/webhook/{flow_id}", json={"sensor": "bme280", "valor": 26.8})
    assert hook_res.status_code == 200
    data = hook_res.json()
    assert data["status"] == "ok"
    assert "tmpl" in data["results"]
    assert data["results"]["tmpl"]["status"] == "ok"
    assert data["results"]["tmpl"]["data"] == "Recibido de bme280: 26.8C"

    # Limpiar flujo de prueba
    client.delete(f"/api/flow?id={flow_id}")

