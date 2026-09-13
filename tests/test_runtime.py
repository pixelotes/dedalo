"""Tests para el runtime de Dédalo Standalone."""

from dedalo import runtime
import re


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


def test_resolve_user_agent():
    assert "Chrome" in runtime._resolve_user_agent("chrome")
    assert "Safari" in runtime._resolve_user_agent("safari")
    assert "Firefox" in runtime._resolve_user_agent("firefox")
    assert "iPhone" in runtime._resolve_user_agent("mobile")
    assert "Dedalo" in runtime._resolve_user_agent("bot")
    assert runtime._resolve_user_agent("MiBot/2.0") == "MiBot/2.0"
    assert runtime._resolve_user_agent("") == ""


def test_http_call_httpx_mock(monkeypatch):
    class DummyResp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ping": "pong"}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert "headers" in kwargs
            assert kwargs["headers"].get("User-Agent") == "MiBot/1.0"
            return DummyResp()

    monkeypatch.setattr(runtime.httpx, "Client", DummyClient)

    res = runtime.http_call(
        None,
        [],
        url="https://api.example.com/test",
        engine="httpx",
        user_agent="MiBot/1.0",
    )
    assert res.ok
    assert res.data == {"ping": "pong"}


def test_http_call_curl_cffi_mock(monkeypatch):
    class DummyCurlResp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>Hola Mundo</body></html>"

    captured = {}

    def dummy_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return DummyCurlResp()

    import types
    dummy_curl = types.ModuleType("curl_cffi")
    dummy_curl_requests = types.ModuleType("curl_cffi.requests")
    dummy_curl_requests.request = dummy_request
    dummy_curl.requests = dummy_curl_requests

    monkeypatch.setattr(runtime, "_has_curl_cffi", lambda: True)
    monkeypatch.setitem(runtime.sys.modules, "curl_cffi", dummy_curl)
    monkeypatch.setitem(runtime.sys.modules, "curl_cffi.requests", dummy_curl_requests)

    res = runtime.http_call(
        None,
        [],
        url="https://ejemplo.com/pagina",
        engine="curl_cffi",
        impersonate="safari",
        user_agent="safari",
    )
    assert res.ok
    assert "Hola Mundo" in res.data
    assert captured["method"] == "GET"
    assert captured["kwargs"]["impersonate"] == "safari"
    assert "Safari" in captured["kwargs"]["headers"]["User-Agent"]


def test_http_call_curl_cffi_missing(monkeypatch):
    monkeypatch.setattr(runtime, "_has_curl_cffi", lambda: False)

    res = runtime.http_call(
        None,
        [],
        url="https://ejemplo.com",
        engine="curl_cffi",
    )
    assert res.failed
    assert "curl_cffi" in res.error


def test_web_read_passthrough(monkeypatch):
    called_with = {}

    def fake_http_call(ctx, inputs, *, url, engine, impersonate, user_agent, **kwargs):
        called_with["engine"] = engine
        called_with["impersonate"] = impersonate
        called_with["user_agent"] = user_agent
        return runtime.NodeResult("ok", data="<p>Texto de prueba con longitud suficiente</p>")

    monkeypatch.setattr(runtime, "http_call", fake_http_call)

    res = runtime.web_read(
        None,
        [],
        url="https://ejemplo.com",
        length="short",
        engine="curl_cffi",
        impersonate="firefox",
        user_agent="firefox",
    )
    assert res.ok
    assert called_with["engine"] == "curl_cffi"
    assert called_with["impersonate"] == "firefox"
    assert called_with["user_agent"] == "firefox"


def test_html_extract_records():
    html = """
    <div class="noticia">
        <h2><a href="/art1">Titulo 1</a></h2>
        <span class="cat">Juegos</span>
        <p class="desc">Descripcion de la noticia 1</p>
    </div>
    <div class="noticia">
        <h2><a href="/art2">Titulo 2</a></h2>
        <span class="cat">Hardware</span>
        <p class="desc">Descripcion de la noticia 2</p>
    </div>
    """
    inp = runtime.NodeResult("ok", data=html)
    fields = '{"title": "h2 a", "link": "h2 a@href", "category": "span.cat", "summary": "p.desc"}'
    res = runtime.html_extract(
        None,
        [inp],
        selector="div.noticia",
        fields=fields,
        base_url="https://ejemplo.com",
        output_format="records",
    )
    assert res.ok
    assert len(res.data) == 2
    assert res.data[0]["title"] == "Titulo 1"
    assert res.data[0]["link"] == "https://ejemplo.com/art1"
    assert res.data[0]["category"] == "Juegos"
    assert res.data[0]["summary"] == "Descripcion de la noticia 1"


def test_html_extract_text_template():
    html = """
    <article class="post">
        <h3><a href="/p1">Post Uno</a></h3>
    </article>
    <article class="post">
        <h3><a href="/p2">Post Dos</a></h3>
    </article>
    """
    inp = runtime.NodeResult("ok", data=html)
    res = runtime.html_extract(
        None,
        [inp],
        selector="article.post",
        fields='{"title": "h3 a", "url": "h3 a@href"}',
        base_url="https://blog.test",
        output_format="text",
        header_text="# Mi Blog",
        item_template="- [{title}]({url})",
    )
    assert res.ok
    assert "# Mi Blog" in res.data
    assert "- [Post Uno](https://blog.test/p1)" in res.data
    assert "- [Post Dos](https://blog.test/p2)" in res.data


def test_summarize_dict_field():
    article_dict = {
        "title": "Historia de la Computación",
        "extract": "La historia de la computación abarca varios siglos. En el siglo XIX Charles Babbage diseñó la máquina analítica. Ada Lovelace escribió el primer algoritmo para esta máquina. En el siglo XX Alan Turing formalizó el concepto de algoritmo con la máquina de Turing. Durante la Segunda Guerra Mundial se construyeron los primeros computadores electrónicos como Colossus y ENIAC. Más tarde llegaron los transistores y los circuitos integrados revolucionando el mundo.",
    }
    inp = runtime.NodeResult("ok", data=article_dict)
    res = runtime.summarize_text(None, [inp], engine="luhn", length="short", field="extract")
    assert res.ok
    assert "máquina" in res.data.lower() or "computación" in res.data.lower()


def test_summarize_custom_sentences_and_ratio():
    sentences = [
        f"Esta es la oración informativa número {i} sobre ciencia de datos."
        for i in range(10)
    ]
    text = " ".join(sentences)
    inp = runtime.NodeResult("ok", data=text)

    # Test sentences=4
    res_sentences = runtime.summarize_text(None, [inp], engine="luhn", sentences=4)
    assert res_sentences.ok
    # Deve devolver exactamente 4 oraciones
    assert len(re.split(r"(?<=[.?!])\s+", res_sentences.data)) == 4

    # Test ratio=0.3 (3 de 10)
    res_ratio = runtime.summarize_text(None, [inp], engine="luhn", ratio=0.3)
    assert res_ratio.ok
    assert len(re.split(r"(?<=[.?!])\s+", res_ratio.data)) == 3


def test_summarize_nested_field_and_abbreviations():
    nested_data = {
        "query": {
            "pages": [
                {
                    "title": "Arqueología",
                    "extract": "El arqueólogo Dr. Gómez visitó Roma en el siglo I a. C. para examinar ruinas de arqueología romana. Roma tiene gran historia de arqueología.",
                }
            ]
        }
    }
    inp = runtime.NodeResult("ok", data=nested_data)

    # Buscar por ruta dot-notation con índice de lista
    res_path = runtime.summarize_text(None, [inp], engine="luhn", field="query.pages.0.extract", length="full")
    assert res_path.ok
    # Dr. Gómez no debe haberse separado
    assert "Dr. Gómez" in res_path.data
    assert "siglo I a. C." in res_path.data

    # Buscar por clave extract recursiva
    res_recur = runtime.summarize_text(None, [inp], engine="luhn", field="extract", length="full")
    assert res_recur.ok
    assert "Dr. Gómez" in res_recur.data
    assert "siglo I a. C." in res_recur.data


def test_template_indexing_and_recursive_fallback():
    data = {
        "query": {
            "pages": [
                {
                    "title": "Milenio",
                    "canonicalurl": "https://es.wikipedia.org/wiki/Milenio",
                }
            ]
        }
    }
    inp1 = runtime.NodeResult("ok", data=data)
    inp2 = runtime.NodeResult("ok", data="Texto del resumen.")

    # Con ruta exacta con índice de lista
    tmpl1 = "Titulo: {query.pages.0.title} - URL: {query.pages.0.canonicalurl} - Resumen: {entrada}"
    res1 = runtime.template(None, [inp1, inp2], template=tmpl1)
    assert res1.ok
    assert "Titulo: Milenio - URL: https://es.wikipedia.org/wiki/Milenio - Resumen: Texto del resumen." == res1.data

    # Con búsqueda recursiva de respaldo
    tmpl2 = "Titulo: {title} - URL: {canonicalurl} - Resumen: {entrada}"
    res2 = runtime.template(None, [inp1, inp2], template=tmpl2)
    assert res2.ok
    assert "Titulo: Milenio - URL: https://es.wikipedia.org/wiki/Milenio - Resumen: Texto del resumen." == res2.data


def test_summarize_luhn_plus_and_bullets():
    text = (
        "El telescopio James Webb es el observatorio espacial más potente jamás construido por la NASA. "
        "Fue lanzado en diciembre de 2021 a bordo de un cohete Ariane 5 desde la Guayana Francesa. "
        "Su espejo primario de berilio bañado en oro le permite observar las primeras galaxias del universo. "
        "Además cuenta con un parasol del tamaño de una pista de tenis para mantener los instrumentos fríos. "
        "Los astrónomos utilizan sus datos infrarrojos para estudiar atmósferas de exoplanetas distantes."
    )
    inp = runtime.NodeResult("ok", data=text)

    # Luhn+ en formato viñetas
    res_bullets = runtime.summarize_text(None, [inp], engine="luhn+", sentences=2, format="bullets")
    assert res_bullets.ok
    assert res_bullets.data.startswith("- ")
    lines = [line.strip() for line in res_bullets.data.splitlines() if line.strip()]
    assert len(lines) == 2
    # El sesgo posicional prioriza la primera oración
    assert "James Webb" in lines[0]

    # Luhn+ en formato párrafo continuo
    res_para = runtime.summarize_text(None, [inp], engine="luhn+", sentences=2, format="paragraph")
    assert res_para.ok
    assert not res_para.data.startswith("- ")
    assert "James Webb" in res_para.data


def test_summarize_textrank():
    text = (
        "Python es un lenguaje de programación de alto nivel concebido por Guido van Rossum. "
        "Su filosofía enfatiza una sintaxis legible orientada a facilitar el desarrollo rápido de software. "
        "Python soporta múltiples paradigmas de programación incluyendo la orientación a objetos y programación funcional. "
        "Es ampliamente utilizado en ciencia de datos, inteligencia artificial y desarrollo web contemporáneo. "
        "La enorme biblioteca estándar de Python ha impulsado enormemente su popularidad global."
    )
    inp = runtime.NodeResult("ok", data=text)

    # TextRank en formato viñetas
    res_tr_bullets = runtime.summarize_text(None, [inp], engine="textrank", sentences=3, format="bullets")
    assert res_tr_bullets.ok
    lines = [line.strip() for line in res_tr_bullets.data.splitlines() if line.strip()]
    assert len(lines) == 3
    assert all(l.startswith("- ") for l in lines)
    # Primera frase central identificada
    assert "Guido van Rossum" in res_tr_bullets.data

    # TextRank en formato párrafo
    res_tr_para = runtime.summarize_text(None, [inp], engine="textrank", sentences=2, format="paragraph")
    assert res_tr_para.ok
    assert "Python" in res_tr_para.data




