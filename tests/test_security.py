"""Tests de las capas de seguridad: cabeceras, límite de cuerpo, token de webhook y purga."""

import threading

import pytest
from fastapi.testclient import TestClient

from dedalo import app as app_module
from dedalo import history
from dedalo.app import app

client = TestClient(app)

FLOW_ID = "test-security-hook"


@pytest.fixture
def hook_flow():
    graph = {
        "version": 1,
        "name": FLOW_ID,
        "nodes": [{"id": "hook", "type": "input.webhook", "config": {}}],
        "edges": [],
    }
    assert client.post("/api/flow", json={"id": FLOW_ID, "graph": graph, "meta": {}}).status_code == 200
    yield FLOW_ID
    client.delete(f"/api/flow?id={FLOW_ID}")


def test_security_headers_on_every_response():
    for res in (client.get("/api/catalog"), client.get("/api/nope")):
        assert res.headers["x-frame-options"] == "DENY"
        assert res.headers["x-content-type-options"] == "nosniff"
        assert res.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    # También en los rechazos de la capa de host
    assert client.get("/api/catalog", headers={"Host": "evil.example"}).headers["x-frame-options"] == "DENY"


def test_body_limit_by_content_length(monkeypatch):
    big = b"x" * (app_module.MAX_BODY_BYTES + 1)
    res = client.post("/api/flows/import", content=big)
    assert res.status_code == 413


def test_body_limit_streamed_without_content_length():
    limit = app_module.MAX_BODY_BYTES

    def chunks():
        yield b"x" * limit
        yield b"x"

    res = client.post("/api/flows/import", content=chunks())  # sin Content-Length (chunked)
    assert res.status_code == 413


def test_webhook_token_required_when_configured(hook_flow, monkeypatch):
    monkeypatch.setenv("DEDALO_WEBHOOK_TOKEN", "s3cr3t")
    assert client.post(f"/api/webhook/{hook_flow}", json={}).status_code == 401
    assert client.post(f"/api/webhook/{hook_flow}", json={}, headers={"Authorization": "Bearer mal"}).status_code == 401
    assert client.post("/api/webhook/no-existe", json={}).status_code == 401  # no revela qué flujos existen

    ok = client.post(f"/api/webhook/{hook_flow}", json={"a": 1}, headers={"Authorization": "Bearer s3cr3t"})
    assert ok.status_code == 200
    assert client.post(f"/api/webhook/{hook_flow}", json={}, headers={"X-Dedalo-Token": "s3cr3t"}).status_code == 200


def test_webhook_token_not_leaked_to_flow(hook_flow, monkeypatch):
    monkeypatch.setenv("DEDALO_WEBHOOK_TOKEN", "s3cr3t")
    res = client.get(f"/api/webhook/{hook_flow}?token=s3cr3t&dato=1")
    assert res.status_code == 200
    assert "s3cr3t" not in res.text  # ni en la respuesta ni, por tanto, en el historial
    assert res.json()["results"]["hook"]["data"] == {"dato": "1"}


def test_webhook_open_without_configured_token(hook_flow, monkeypatch):
    monkeypatch.delenv("DEDALO_WEBHOOK_TOKEN", raising=False)
    assert client.post(f"/api/webhook/{hook_flow}", json={}).status_code == 200


def test_webhook_runs_off_the_event_loop(hook_flow, monkeypatch):
    seen = []
    original = app_module._run_webhook_sync

    def spy(*args, **kwargs):
        seen.append(threading.current_thread() is threading.main_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(app_module, "_run_webhook_sync", spy)
    assert client.post(f"/api/webhook/{hook_flow}", json={}).status_code == 200
    assert seen and not any(seen)


def test_purge_days_must_be_positive():
    assert client.post("/api/history/purge?days=-1").status_code == 422
    assert client.post("/api/history/purge?days=0").status_code == 422
    assert client.post("/api/history/purge?days=30").status_code == 200


def test_init_db_runs_schema_once(tmp_path, monkeypatch):
    calls = []
    real = history._init_schema
    monkeypatch.setattr(history, "_init_schema", lambda p=None: (calls.append(1), real(p))[1])
    db = tmp_path / "h.db"
    history.init_db(db)
    history.init_db(db)
    history.list_runs(db_path=db)
    assert len(calls) == 1
    db.unlink()  # si la base desaparece, se vuelve a crear
    history.init_db(db)
    assert len(calls) == 2


def test_body_limit_streamed_on_webhook_does_not_run_flow(hook_flow):
    limit = app_module.MAX_BODY_BYTES

    def chunks():
        yield b"x" * limit
        yield b"x"

    before = len(history.list_runs(flow_id=hook_flow))
    assert client.post(f"/api/webhook/{hook_flow}", content=chunks()).status_code == 413
    assert len(history.list_runs(flow_id=hook_flow)) == before
