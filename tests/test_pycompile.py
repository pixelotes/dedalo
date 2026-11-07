"""compile()/ast.parse() serializados: varios hilos no deben construir ASTs a la vez."""

import threading
import time

from dedalo import compiler, pycompile


def test_parse_and_compile_never_overlap(monkeypatch):
    """Determinista: con el candado, nunca hay dos compilaciones a la vez."""
    activos, maximo, guard = 0, 0, threading.Lock()

    def lento(*args, **kwargs):
        nonlocal activos, maximo
        with guard:
            activos += 1
            maximo = max(maximo, activos)
        time.sleep(0.01)
        with guard:
            activos -= 1
        return None

    monkeypatch.setattr(pycompile.ast, "parse", lento)
    monkeypatch.setattr(pycompile, "compile", lento, raising=False)
    hilos = [threading.Thread(target=pycompile.parse, args=("x = 1",)) for _ in range(6)]
    hilos += [threading.Thread(target=pycompile.compile_code, args=("x = 1", "<t>")) for _ in range(6)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert maximo == 1


def test_lock_is_reentrant():
    """compiler.compile() puede llamar a parse() mientras otra función ya tiene el candado."""
    with pycompile._LOCK:
        assert pycompile.parse("x = 1") is not None
        assert pycompile.compile_code("x = 1", "<t>") is not None


def test_compile_flows_from_many_threads(tmp_path, monkeypatch):
    """Estrés: compilar y ejecutar flujos desde varios hilos no falla."""
    monkeypatch.chdir(tmp_path)
    graph = compiler.Graph.from_dict({"version": 1, "name": "t", "nodes": [
        {"id": "a_1", "type": "transform.script", "config": {"code": "return [i * 2 for i in range(50)]"}},
        {"id": "b_1", "type": "transform.script", "config": {"code": 'return sum(entrada["a_1"])'}},
    ], "edges": [{"source": "a_1", "target": "b_1", "port": "ok"}]})
    errores = []

    def trabajo():
        try:
            for _ in range(30):
                source = compiler.compile(graph)
                ns = {}
                exec(pycompile.compile_code(source, "<t>"), ns)
        except Exception as exc:  # noqa: BLE001
            errores.append(repr(exc))

    hilos = [threading.Thread(target=trabajo) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert errores == []
