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
    results = res.json()["results"]
    assert "disco" in results
    assert results["disco"]["status"] == "ok"
    assert isinstance(results["disco"]["data"], (int, float))
