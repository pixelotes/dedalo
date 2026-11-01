"""Tests de la caché de list_flows, el scheduler concurrente y los errores 500 genéricos."""

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from dedalo import scheduler, store
from dedalo.app import app


@pytest.fixture
def flows_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "FLOWS_DIR", tmp_path)
    monkeypatch.setattr(store, "_LIST_CACHE", {})
    return tmp_path


def _write(d, fid, name, schedule=""):
    (d / f"{fid}.json").write_text(json.dumps({"nodes": []}))
    (d / f"{fid}.meta.json").write_text(json.dumps({"name": name, "schedule": schedule, "updated_at": 1}))


def test_list_flows_cache_hits_and_invalidates(flows_dir, monkeypatch):
    _write(flows_dir, "a", "Uno")
    assert [f["name"] for f in store.list_flows()] == ["Uno"]

    reads = []
    real = type(flows_dir).read_text
    monkeypatch.setattr(type(flows_dir), "read_text", lambda self, *a, **k: (reads.append(self.name), real(self, *a, **k))[1])
    store.list_flows()
    assert reads == []  # segunda llamada: sin releer ni parsear

    _write(flows_dir, "a", "Uno renombrado")  # el tamaño cambia -> invalida
    assert store.list_flows()[0]["name"] == "Uno renombrado"
    (flows_dir / "a.json").unlink()
    (flows_dir / "a.meta.json").unlink()
    assert store.list_flows() == []
    assert store._LIST_CACHE == {}


def test_list_flows_returns_independent_copies(flows_dir):
    _write(flows_dir, "a", "Uno")
    store.list_flows()[0]["last_run"] = "x"  # los llamadores añaden campos
    assert "last_run" not in store.list_flows()[0]


async def test_scheduler_runs_due_flows_concurrently_but_never_overlaps(flows_dir, monkeypatch):
    for fid in ("a", "b", "c"):
        _write(flows_dir, fid, fid, schedule="@every 1m")
    monkeypatch.setattr(scheduler, "RUN_HISTORY", {})
    monkeypatch.setattr(scheduler, "_should_run", lambda expr, last: True)

    active, peak, started = 0, 0, []
    lock = threading.Lock()

    def fake_run(fid, trigger):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            started.append(fid)
        time.sleep(0.3)
        with lock:
            active -= 1
        return {"duration": 0.3, "run_id": "r"}

    monkeypatch.setattr(scheduler, "run_flow_sync", fake_run)
    sched = scheduler.Scheduler(check_interval=0.05, max_concurrent=2)
    sched._running = True
    sched._task = asyncio.create_task(sched._loop())
    await asyncio.sleep(0.2)  # varias vueltas del bucle con los flujos aún en marcha
    await sched.stop()

    assert peak == 2  # concurrencia limitada por el semáforo
    assert sorted(started) == ["a", "b"]  # c espera su turno y ninguno se relanza mientras corre


def test_internal_errors_do_not_leak_details(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("/ruta/interna/secreta.py falló")

    monkeypatch.setattr(store, "save_flow", boom)
    res = TestClient(app).post("/api/flow", json={"id": "x", "graph": {"nodes": [], "edges": []}, "meta": {}})
    assert res.status_code == 500
    assert "interna" not in res.text and "logs" in res.text
