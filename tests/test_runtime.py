"""Tests para el runtime de Dédalo Standalone."""

from dedalo import runtime


def test_threshold():
    ctx = None
    res_in = runtime.NodeResult("ok", data=15.0)

    # Menor que 20 -> OK
    r1 = runtime.threshold(ctx, [res_in], comparison="menor_que", limit=20.0)
    assert r1.ok
    assert r1.data == 15.0

    # Menor que 10 -> SKIPPED
    r2 = runtime.threshold(ctx, [res_in], comparison="menor_que", limit=10.0)
    assert r2.skipped


def test_filter_items():
    ctx = None
    data = [
        {"title": "Lanzamiento de Python 3.13", "tag": "dev"},
        {"title": "Nueva Raspberry Pi 5", "tag": "hardware"},
        {"title": "Actualización de Linux", "tag": "dev"},
    ]
    res_in = runtime.NodeResult("ok", data=data)

    rf = runtime.filter_items(ctx, [res_in], contains="python", field="title")
    assert rf.ok
    assert len(rf.data) == 1
    assert rf.data[0]["title"] == "Lanzamiento de Python 3.13"


def test_script_node():
    ctx = None
    inp1 = runtime.NodeResult("ok", data="hola")
    inp2 = runtime.NodeResult("ok", data="mundo")

    def _mi_logica(entrada, c):
        return f"{entrada['n1'].upper()} {entrada['n2'].upper()}"

    r = runtime.script(ctx, [inp1, inp2], _mi_logica, sources=["n1", "n2"])
    assert r.ok
    assert r.data == "HOLA MUNDO"


def test_merge_node():
    ctx = None
    inp1 = runtime.NodeResult("ok", data="primero")
    inp2 = runtime.NodeResult("error", error="fallo")
    inp3 = runtime.NodeResult("ok", data="tercero")

    r = runtime.merge(ctx, [inp1, inp2, inp3])
    assert r.ok
    assert r.data == ["primero", "tercero"]


def test_webhook_trigger():
    class DummyCtx:
        payload = {"sensor": "temp", "val": 24.5}

    r1 = runtime.webhook_trigger(DummyCtx(), [])
    assert r1.ok
    assert r1.data["sensor"] == "temp"

    r2 = runtime.webhook_trigger(None, [])
    assert r2.ok
    assert "event" in r2.data


def test_sqlite_nodes(tmp_path):
    db_file = tmp_path / "test.db"
    # 1. Crear tabla
    r_create = runtime.sqlite_exec(None, [], path=str(db_file), query="CREATE TABLE logs (msg TEXT, level INT)")
    assert r_create.ok

    # 2. Insertar fila con datos desde input
    inp = runtime.NodeResult("ok", data={"msg": "Alerta crítica", "level": 2})
    r_insert = runtime.sqlite_exec(None, [inp], path=str(db_file), query="INSERT INTO logs VALUES (:msg, :level)")
    assert r_insert.ok
    assert r_insert.data["rows_affected"] == 1

    # 3. Consultar
    r_query = runtime.sqlite_query(None, [], path=str(db_file), query="SELECT * FROM logs")
    assert r_query.ok
    assert len(r_query.data) == 1
    assert r_query.data[0]["msg"] == "Alerta crítica"
    assert r_query.data[0]["level"] == 2


def test_command_nodes():
    r = runtime.command_input(None, [], cmd='echo "Hola desde shell"')
    assert r.ok
    assert "Hola desde shell" in r.data["stdout"]
    assert r.data["exit_code"] == 0


def test_pi_health():
    r = runtime.pi_health(None, [])
    assert r.ok
    assert "timestamp" in r.data
    assert "disk_percent_used" in r.data


def test_directory_scan(tmp_path):
    (tmp_path / "doc1.txt").write_text("a", encoding="utf-8")
    (tmp_path / "doc2.log").write_text("b", encoding="utf-8")
    r = runtime.directory_scan(None, [], path=str(tmp_path), pattern="*.txt")
    assert r.ok
    assert len(r.data) == 1
    assert r.data[0]["name"] == "doc1.txt"


def test_template():
    inp = runtime.NodeResult("ok", data={"host": "rpi-server", "cpu": 45})
    tmpl = "Alerta en {host}: CPU al {cpu}% (hoy: {fecha})"
    r = runtime.template(None, [inp], template=tmpl)
    assert r.ok
    assert "Alerta en rpi-server: CPU al 45%" in r.data


def test_array_ops():
    data = [
        {"id": 1, "score": 90},
        {"id": 2, "score": 40},
        {"id": 3, "score": 75},
        {"id": 1, "score": 90},  # Duplicado
    ]
    inp = runtime.NodeResult("ok", data=data)

    # Sort desc
    r_sort = runtime.array_ops(None, [inp], operation="sort", field="score", order="desc")
    assert r_sort.ok
    assert r_sort.data[0]["score"] == 90

    # Dedup
    r_dedup = runtime.array_ops(None, [inp], operation="dedup", field="id")
    assert r_dedup.ok
    assert len(r_dedup.data) == 3

    # Head
    r_head = runtime.array_ops(None, [inp], operation="head", count=2)
    assert r_head.ok
    assert len(r_head.data) == 2

    # Join
    texts = ["rojo", "verde", "azul"]
    r_join = runtime.array_ops(None, [runtime.NodeResult("ok", data=texts)], operation="join", join_sep=", ")
    assert r_join.ok
    assert r_join.data == "rojo, verde, azul"


def test_regex():
    inp = runtime.NodeResult("ok", data="Pedido #1234 y Pedido #5678 completados.")
    r_all = runtime.regex(None, [inp], pattern=r"#(\d+)", action="find_all")
    assert r_all.ok
    assert r_all.data == ["1234", "5678"]

    r_sub = runtime.regex(None, [inp], pattern=r"#\d+", action="sub", replace_with="[OCULTO]")
    assert r_sub.ok
    assert r_sub.data == "Pedido [OCULTO] y Pedido [OCULTO] completados."


def test_router():
    inp_ok = runtime.NodeResult("ok", data={"status": "active"})
    r1 = runtime.router(None, [inp_ok], condition="equals", field="status", value="active")
    assert r1.ok

    r2 = runtime.router(None, [inp_ok], condition="equals", field="status", value="inactive")
    assert r2.failed  # Emite por rama de error


def test_threshold_text():
    inp = runtime.NodeResult("ok", data="ERROR: disco lleno")
    r1 = runtime.threshold(None, [inp], comparison="contiene", limit="disco")
    assert r1.ok

    r2 = runtime.threshold(None, [inp], comparison="contiene", limit="memoria")
    assert r2.skipped

    r3 = runtime.threshold(None, [inp], comparison="no_vacio")
    assert r3.ok
