"""Tests del nodo input.subflows: flujos que llaman a otros flujos en paralelo."""

import threading
import time

import pytest

from dedalo import compiler, history, runtime, scheduler, store


def _flow(fid: str, nodes: list[dict], edges: list[dict] | None = None) -> None:
    store.save_flow(fid, {"version": 1, "name": fid, "nodes": nodes, "edges": edges or []}, force=True)


def _script(code: str, node_id: str = "s") -> dict:
    return {"id": node_id, "type": "transform.script", "config": {"code": code}}


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    """Todas las rutas de datos de Dédalo son relativas (data/...): se aíslan en un directorio temporal."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    for t in threading.enumerate():  # un sub-flujo caducado sigue vivo: que no escriba fuera del test
        if t.name.startswith("dedalo-subflow"):
            t.join(10)


def _ctx(name: str = "morning") -> scheduler.FlowContext:
    return scheduler.FlowContext(flow_name=name)


def test_runs_flows_collects_data_and_isolates_errors():
    _flow("news", [_script("return 'titulares'")])
    _flow("weather", [_script("return {'temp': 21}")])
    _flow("broken", [_script("raise ValueError('boom')")])

    res = runtime.subflows_run(_ctx(), [], flows="news, weather\nbroken, nope")
    assert res.ok
    assert list(res.data) == ["news", "weather", "broken", "nope"]
    assert res.data["news"]["status"] == "ok" and res.data["news"]["data"] == "titulares"
    assert res.data["weather"]["data"] == {"temp": 21}
    assert res.data["broken"]["status"] == "error" and "boom" in res.data["broken"]["error"]
    assert res.data["broken"]["errors"]  # nodos que fallaron dentro del sub-flujo
    assert res.data["nope"]["status"] == "error" and "not found" in res.data["nope"]["error"]

    # cada sub-ejecución queda en el historial, enlazada al run padre
    runs = history.list_runs(flow_id="news")
    assert runs and runs[0]["trigger_type"] == "subflow"


def test_runs_in_parallel_and_respects_max_parallel():
    for fid in ("a", "b", "c"):
        _flow(fid, [_script("import time\ntime.sleep(0.6)\nreturn 1")])
    t0 = time.time()
    res = runtime.subflows_run(_ctx(), [], flows="a,b,c", max_parallel=3)
    assert all(r["status"] == "ok" for r in res.data.values()), res.data
    assert time.time() - t0 < 1.5  # en serie serían ~1.8 s

    t0 = time.time()
    runtime.subflows_run(_ctx(), [], flows="a,b,c", max_parallel=1)
    assert time.time() - t0 >= 1.7


def test_timeout_reports_late_flows_and_keeps_the_fast_ones():
    _flow("fast", [_script("return 'ya'")])
    _flow("slow", [_script("import time\ntime.sleep(2.2)\nreturn 'tarde'")])
    t0 = time.time()
    res = runtime.subflows_run(_ctx(), [], flows="fast,slow", timeout=1)
    assert time.time() - t0 < 2.0
    assert res.data["fast"]["status"] == "ok"
    assert res.data["slow"]["status"] == "timeout" and "1s" in res.data["slow"]["error"]


def test_queued_flows_that_miss_the_deadline_never_start(workdir):
    _flow("first", [_script("import time\ntime.sleep(1.5)\nreturn 1")])
    _flow("queued", [_script("return 2"), {"id": "o", "type": "output.file", "config": {"filename": "queued.txt", "directory": "q"}}],
          [{"source": "s", "target": "o", "port": "ok"}])
    res = runtime.subflows_run(_ctx(), [], flows="first,queued", timeout=1, max_parallel=1, skip_outputs=False)
    assert res.data["queued"]["status"] == "timeout"
    time.sleep(1.2)  # aunque 'first' termine después, 'queued' ya no debe arrancar
    assert not (workdir / "data/outputs/q/queued.txt").exists()


def test_skip_outputs_by_default_and_opt_out(workdir):
    _flow(
        "report",
        [_script("return 'hola'"), {"id": "o", "type": "output.file", "config": {"filename": "r.txt", "directory": "t"}}],
        [{"source": "s", "target": "o", "port": "ok"}],
    )
    target = workdir / "data/outputs/t/r.txt"
    res = runtime.subflows_run(_ctx(), [], flows="report")
    assert res.data["report"]["status"] == "ok" and res.data["report"]["data"] == "hola"  # sumidero = el script
    assert not target.exists()

    res = runtime.subflows_run(_ctx(), [], flows="report", skip_outputs=False)
    assert res.data["report"]["status"] == "ok" and target.exists()


def test_status_skipped_when_no_sink_produces_data():
    _flow(
        "gated",
        [_script("return 1", "a"), _script("return 2", "b")],
        [{"source": "a", "target": "b", "port": "error"}],  # b solo corre si a falla
    )
    res = runtime.subflows_run(_ctx(), [], flows="gated")
    assert res.data["gated"]["status"] == "skipped" and res.data["gated"]["data"] is None


def test_payload_reaches_the_subflow_and_several_sinks_return_a_dict():
    _flow("echo", [{"id": "w", "type": "input.webhook", "config": {}}])
    res = runtime.subflows_run(_ctx(), [], flows="echo", payload='{"day": "lunes"}')
    assert res.data["echo"]["data"] == {"day": "lunes"}
    assert runtime.subflows_run(_ctx(), [], flows="echo", payload="texto").data["echo"]["data"] == "texto"

    _flow("two", [_script("return 1", "a"), _script("return 2", "b")])
    assert runtime.subflows_run(_ctx(), [], flows="two").data["two"]["data"] == {"a": 1, "b": 2}


def test_cycles_and_depth_are_rejected():
    node = lambda target: [{"id": "n", "type": "input.subflows", "config": {"flows": target, "timeout": 5}}]  # noqa: E731
    _flow("x", node("y"))
    _flow("y", node("x"))
    out = scheduler.run_subflow_sync("x")
    assert out["status"] == "ok"
    inner = out["data"]["y"]["data"]["x"]
    assert inner["status"] == "error" and "cycle" in inner["error"]

    deep = scheduler.run_subflow_sync("x", parent=scheduler.FlowContext(flow_name="c", stack=("a", "b")))
    assert deep["status"] == "error" and "depth" in deep["error"]


def test_config_errors_and_standalone_is_blocked():
    assert "Missing the list of flows" in runtime.subflows_run(_ctx(), [], flows=" , ").error
    g = compiler.Graph(
        name="morning",
        nodes=[compiler.Node(id="sub", type="input.subflows", config={"flows": "news"})],
        edges=[],
    )
    assert compiler.validate(g) == []
    assert "runtime.subflows_run" in compiler.compile(g)
    with pytest.raises(compiler.GraphError):
        compiler.compile_standalone(g)


def test_without_outputs_keeps_everything_else():
    g = compiler.Graph(
        name="f",
        nodes=[
            compiler.Node(id="a", type="transform.script", config={"code": "return 1"}),
            compiler.Node(id="o", type="output.file", config={}),
        ],
        edges=[compiler.Edge(source="a", target="o")],
    )
    pruned = compiler.without_outputs(g)
    assert [n.id for n in pruned.nodes] == ["a"] and pruned.edges == []
    assert len(g.nodes) == 2  # el original no se toca


def test_morning_report_end_to_end():
    """El caso de uso: un informe que hace un script con lo que llega y oculta lo que falló."""
    _flow("news", [_script("return ['n1', 'n2']")])
    _flow("weather", [_script("raise RuntimeError('sin red')")])
    _flow(
        "morning",
        [
            {"id": "sub", "type": "input.subflows", "config": {"flows": "news, weather", "timeout": 10}},
            _script(
                "parts = []\n"
                "for name, r in entrada['sub'].items():\n"
                "    if r['status'] == 'ok':\n"
                "        parts.append(f'## {name}\\n' + '\\n'.join(r['data']))\n"
                "return '\\n\\n'.join(parts)",
                "report",
            ),
        ],
        [{"source": "sub", "target": "report", "port": "ok"}],
    )
    out = scheduler.run_flow_sync("morning", "manual")
    assert out["result"]["report"]["data"] == "## news\nn1\nn2"  # weather falló: su sección no aparece
    assert history.list_runs(flow_id="weather")[0]["trigger_type"].startswith("subflow:run_")
