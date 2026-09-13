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
    assert all(ln.startswith("- ") for ln in lines)
    # Primera frase central identificada
    assert "Guido van Rossum" in res_tr_bullets.data

    # TextRank en formato párrafo
    res_tr_para = runtime.summarize_text(None, [inp], engine="textrank", sentences=2, format="paragraph")
    assert res_tr_para.ok
    assert "Python" in res_tr_para.data


def test_web_search_duckduckgo_mocked(monkeypatch):
    sample_html = """
    <div class="result results_links results_links_deep web-result ">
        <h2 class="result__title">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F">Welcome to <b>Python.org</b></a>
        </h2>
        <div class="result__extras"></div>
        <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2F">Python &amp; &quot;batteries included&quot; language.</a>
    </div>
    <div class="result results_links results_links_deep web-result ">
        <h2 class="result__title">
            <a class="result__a" href="https://es.wikipedia.org/wiki/Python">Python en Wikipedia</a>
        </h2>
        <div class="result__extras"></div>
        <a class="result__snippet">Lenguaje de programaci&oacute;n interpretado.</a>
    </div>
    """

    class FakeResponse:
        status_code = 200
        text = sample_html

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, headers=None):
            return FakeResponse()

    monkeypatch.setattr(runtime, "_has_curl_cffi", lambda: False)
    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    # 1. Salida JSON
    res_json = runtime.web_search(None, [], query="python programming", engine="duckduckgo", limit=2, output_format="json")
    assert res_json.ok
    assert len(res_json.data) == 2
    assert res_json.data[0]["title"] == "Welcome to Python.org"
    assert res_json.data[0]["url"] == "https://www.python.org/"
    assert res_json.data[0]["snippet"] == 'Python & "batteries included" language.'
    assert res_json.data[1]["title"] == "Python en Wikipedia"
    assert res_json.data[1]["url"] == "https://es.wikipedia.org/wiki/Python"

    # 2. Salida Markdown
    res_md = runtime.web_search(None, [], query="python programming", engine="duckduckgo", limit=1, output_format="markdown")
    assert res_md.ok
    assert res_md.data == '- [Welcome to Python.org](https://www.python.org/): Python & "batteries included" language.'

    # 3. Interpolar {entrada}
    inp = runtime.NodeResult("ok", data="noticias hoy")
    res_interpolated = runtime.web_search(None, [inp], query="{entrada} tecnología", engine="duckduckgo")
    assert res_interpolated.ok


def test_web_search_validation():
    # Consulta vacía sin entradas
    r_err = runtime.web_search(None, [], query="")
    assert not r_err.ok
    assert "Se requiere un término de búsqueda" in r_err.error

    # Motor desconocido
    r_eng = runtime.web_search(None, [], query="algo", engine="altavista")
    assert not r_eng.ok
    assert "Motor de búsqueda desconocido" in r_eng.error


def test_web_search_tavily(monkeypatch):
    # Sin secreto
    r_no_sec = runtime.web_search(None, [], query="ia news", engine="tavily", secret="")
    assert not r_no_sec.ok
    assert "requiere configurar un secreto" in r_no_sec.error

    # Con secreto mockeado
    from dedalo import secrets
    monkeypatch.setattr(secrets, "get", lambda name: "fake-tavily-key" if name == "mi_tavily" else "")

    recorded = {}
    class FakeTavilyResponse:
        status_code = 200
        def json(self):
            return {
                "results": [
                    {"title": "Tavily Title", "url": "https://tavily.com/1", "content": "Tavily content", "score": 0.95}
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None, headers=None):
            recorded["url"] = url
            recorded["json"] = json
            return FakeTavilyResponse()

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    res = runtime.web_search(None, [], query="ia news", engine="tavily", secret="mi_tavily", limit=3)
    assert res.ok
    assert recorded["url"] == "https://api.tavily.com/search"
    assert recorded["json"]["api_key"] == "fake-tavily-key"
    assert recorded["json"]["query"] == "ia news"
    assert len(res.data) == 1
    assert res.data[0]["title"] == "Tavily Title"
    assert res.data[0]["snippet"] == "Tavily content"
    assert res.data[0]["score"] == 0.95


def test_web_search_exa(monkeypatch):
    # Sin secreto
    r_no_sec = runtime.web_search(None, [], query="papers", engine="exa", secret="")
    assert not r_no_sec.ok
    assert "requiere configurar un secreto" in r_no_sec.error

    # Con secreto mockeado
    from dedalo import secrets
    monkeypatch.setattr(secrets, "get", lambda name: "fake-exa-key" if name == "mi_exa" else "")

    recorded = {}
    class FakeExaResponse:
        status_code = 200
        def json(self):
            return {
                "results": [
                    {"title": "Exa Paper", "url": "https://arxiv.org/abs/123", "highlights": ["Highlight summary"], "score": 0.88, "publishedDate": "2026-01-01"}
                ]
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def post(self, url, json=None, headers=None):
            recorded["url"] = url
            recorded["headers"] = headers
            recorded["json"] = json
            return FakeExaResponse()

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    res = runtime.web_search(None, [], query="papers", engine="exa", secret="mi_exa", limit=1)
    assert res.ok
    assert recorded["url"] == "https://api.exa.ai/search"
    assert recorded["headers"]["x-api-key"] == "fake-exa-key"
    assert len(res.data) == 1
    assert res.data[0]["title"] == "Exa Paper"
    assert res.data[0]["snippet"] == "Highlight summary"
    assert res.data[0]["published_date"] == "2026-01-01"


def test_diff_data_stateful(tmp_path, monkeypatch):
    class FakeCtx:
        flow_name = "test_diff_flow"

    # Usar tmp_path para Path("data/state")
    fake_state_dir = tmp_path / "state"
    fake_state_dir.mkdir(parents=True)
    monkeypatch.setattr(runtime, "Path", lambda *args: (fake_state_dir / args[1]) if len(args) == 2 and args[0] == "data/state" else tmp_path / "dummy")

    ctx = FakeCtx()

    # Ejecución 1: dos elementos nuevos
    items_1 = [
        {"url": "https://example.com/1", "title": "Noticia 1"},
        {"url": "https://example.com/2", "title": "Noticia 2"},
    ]
    inp1 = runtime.NodeResult("ok", data=items_1)
    res1 = runtime.diff_data(ctx, [inp1], key_field="url", mode="added", state_key="diff_test")
    assert res1.ok
    assert len(res1.data) == 2

    # Ejecución 2: Noticia 1 sigue igual, Noticia 2 desaparece, Noticia 3 es nueva
    items_2 = [
        {"url": "https://example.com/1", "title": "Noticia 1"},
        {"url": "https://example.com/3", "title": "Noticia 3 (Nueva)"},
    ]
    inp2 = runtime.NodeResult("ok", data=items_2)
    # Solo nuevos
    res2_added = runtime.diff_data(ctx, [inp2], key_field="url", mode="added", state_key="diff_test")
    assert res2_added.ok
    assert len(res2_added.data) == 1
    assert res2_added.data[0]["title"] == "Noticia 3 (Nueva)"

    # Ejecución 3: Modo "all"
    items_3 = [
        {"url": "https://example.com/1", "title": "Noticia 1 Modificada"},
    ]
    inp3 = runtime.NodeResult("ok", data=items_3)
    res3_all = runtime.diff_data(ctx, [inp3], key_field="url", mode="all", state_key="diff_test")
    assert res3_all.ok
    assert res3_all.data["count_added"] == 0
    assert res3_all.data["count_removed"] == 1
    assert res3_all.data["count_modified"] == 1


def test_fuzzy_dedup():
    items = [
        {"title": "Catan (Edición 2024)", "price": 35.0, "store": "Mathom"},
        {"title": "Catan Ed. 2024", "price": 32.0, "store": "Outlet-PC"},
        {"title": "Dixit Odyssey", "price": 28.0, "store": "Mathom"},
        {"title": "Dixit: Odyssey juego familiar", "price": 27.5, "store": "Zacatrus"},
        {"title": "Carcassonne", "price": 25.0, "store": "Mathom"},
    ]
    inp = runtime.NodeResult("ok", data=items)

    # 1. Estrategia keep_first
    res_keep = runtime.fuzzy_dedup(None, [inp], field="title", threshold=0.75, merge_strategy="keep_first")
    assert res_keep.ok
    titles = [x["title"] for x in res_keep.data]
    assert len(titles) == 3
    assert "Catan (Edición 2024)" in titles
    assert "Dixit Odyssey" in titles
    assert "Carcassonne" in titles

    # 2. Estrategia merge_list
    res_merge = runtime.fuzzy_dedup(None, [inp], field="title", threshold=0.75, merge_strategy="merge_list")
    assert res_merge.ok
    catan_item = next(x for x in res_merge.data if "Catan" in x["title"])
    assert "duplicates" in catan_item
    assert len(catan_item["duplicates"]) == 1
    assert catan_item["duplicates"][0]["store"] == "Outlet-PC"

    # 3. Estrategia group
    res_grp = runtime.fuzzy_dedup(None, [inp], field="title", threshold=0.75, merge_strategy="group")
    assert res_grp.ok
    assert len(res_grp.data) == 3
    assert len(res_grp.data[0]) == 2  # Catan y su duplicado


def test_aggregate():
    items = [
        {"title": "Juego A", "price": "10,50 €", "category": "familiar"},
        {"title": "Juego B", "price": "20.50", "category": "estrategia"},
        {"title": "Juego C", "price": 30.0, "category": "familiar"},
        {"title": "Juego D", "price": 40, "category": "estrategia"},
    ]
    inp = runtime.NodeResult("ok", data=items)

    # Media global
    res_avg = runtime.aggregate(None, [inp], field="price", operation="avg")
    assert res_avg.ok
    assert res_avg.data == round((10.5 + 20.5 + 30.0 + 40.0) / 4, 4)

    # Suma
    res_sum = runtime.aggregate(None, [inp], field="price", operation="sum")
    assert res_sum.ok
    assert res_sum.data == 101.0

    # Summary
    res_sumry = runtime.aggregate(None, [inp], field="price", operation="summary")
    assert res_sumry.ok
    assert res_sumry.data["count"] == 4
    assert res_sumry.data["min"] == 10.5
    assert res_sumry.data["max"] == 40.0

    # Group by
    res_grp = runtime.aggregate(None, [inp], field="price", operation="sum", group_by="category")
    assert res_grp.ok
    assert res_grp.data["familiar"] == 40.5
    assert res_grp.data["estrategia"] == 60.5


def test_format_table():
    items = [
        {"juego": "Catan", "precio": "35€", "tienda": "Mathom"},
        {"juego": "Dixit", "precio": "28€", "tienda": "Outlet-PC"},
    ]
    inp = runtime.NodeResult("ok", data=items)

    # Markdown
    res_md = runtime.format_table(None, [inp], format="markdown", columns="juego,precio,tienda", headers="Juego,Precio,Tienda")
    assert res_md.ok
    assert "| Juego | Precio | Tienda |" in res_md.data
    assert "| Catan | 35€ | Mathom |" in res_md.data

    # HTML
    res_html = runtime.format_table(None, [inp], format="html", columns="juego,precio", headers="Nombre,Precio")
    assert res_html.ok
    assert "<table" in res_html.data
    assert ">Nombre</th>" in res_html.data
    assert ">Catan</td>" in res_html.data


def test_keywords_rake():
    text = (
        "El telescopio espacial James Webb descubrió galaxias lejanas en el universo primitivo. "
        "Las observaciones infrarrojas del James Webb proporcionan imágenes astronómicas detalladas. "
        "Los astrónomos estudian la formación estelar mediante este observatorio espacial."
    )
    inp = runtime.NodeResult("ok", data=text)

    res_list = runtime.keywords_rake(None, [inp], max_keywords=3, max_words=3, output_format="list")
    assert res_list.ok
    assert isinstance(res_list.data, list)
    assert len(res_list.data) > 0
    # Comprobar que detecta james webb o observatorio espacial
    all_kws = " ".join(res_list.data).lower()
    assert "james webb" in all_kws or "espacial" in all_kws or "universo" in all_kws

    res_str = runtime.keywords_rake(None, [inp], max_keywords=2, output_format="text")
    assert res_str.ok
    assert isinstance(res_str.data, str)
    assert ", " in res_str.data


def test_ssl_cert_mocked(monkeypatch):
    class FakeSSLSocket:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def getpeercert(self):
            return {
                "notAfter": "May 20 12:00:00 2030 GMT",
                "issuer": ((("organizationName", "Let's Encrypt"),),),
                "subject": ((("commonName", "example.com"),),),
                "serialNumber": "1234567890",
            }

    class FakeSSLContext:
        def wrap_socket(self, sock, server_hostname=None):
            return FakeSSLSocket()

    monkeypatch.setattr(runtime.ssl, "create_default_context", lambda: FakeSSLContext())
    monkeypatch.setattr(runtime.socket, "create_connection", lambda addr, timeout=None: FakeSSLSocket())

    res = runtime.ssl_cert(None, [], host="https://example.com:443")
    assert res.ok
    assert res.data["host"] == "example.com"
    assert res.data["days_left"] > 365
    assert res.data["is_valid"] is True
    assert res.data["issuer"] == "Let's Encrypt"
    assert res.data["subject"] == "example.com"


def test_dns_check_and_ping_mocked(monkeypatch):
    # DNS Check
    monkeypatch.setattr(runtime.socket, "gethostbyname_ex", lambda host: ("example.com", [], ["93.184.216.34"]))
    res_dns = runtime.dns_check(None, [], host="example.com")
    assert res_dns.ok
    assert res_dns.data["canonical_name"] == "example.com"
    assert "93.184.216.34" in res_dns.data["ips"]
    assert res_dns.data["response_time_ms"] >= 0

    # Ping Host TCP
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    monkeypatch.setattr(runtime.socket, "create_connection", lambda addr, timeout=None: FakeSocket())
    res_ping = runtime.ping_host(None, [], host="example.com", port=80, mode="tcp")
    assert res_ping.ok
    assert res_ping.data["alive"] is True
    assert res_ping.data["mode"] == "tcp"
    assert res_ping.data["latency_ms"] >= 0


def test_mqtt_publish_and_read_mocked(monkeypatch):
    sent_buffers = []

    class FakeMQTTSocket:
        def __init__(self, mode="pub"):
            self._mode = mode
            self.recv_calls = 0
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, t): pass
        def sendall(self, data): sent_buffers.append(data)
        def recv(self, n):
            self.recv_calls += 1
            if self._mode == "pub":
                return b"\x20\x02\x00\x00"
            else:
                if self.recv_calls == 1:
                    return b"\x20\x02\x00\x00"
                elif self.recv_calls == 2:
                    return b"\x90\x03\x00\x01\x00"
                else:
                    top_b = b"sensores/temp"
                    pay_b = b"23.5"
                    body = len(top_b).to_bytes(2, "big") + top_b + pay_b
                    return b"\x30" + bytes([len(body)]) + body

    current_mode = ["pub"]
    monkeypatch.setattr(runtime.socket, "create_connection", lambda addr, timeout=None: FakeMQTTSocket(current_mode[0]))

    # 1. Publish
    res_pub = runtime.mqtt_publish(None, [], broker="127.0.0.1", topic="sensores/temp", payload="24.1")
    assert res_pub.ok
    assert "Publicado en sensores/temp" in res_pub.data
    assert len(sent_buffers) == 3  # CONNECT, PUBLISH, DISCONNECT

    # 2. Read
    sent_buffers.clear()
    current_mode[0] = "sub"
    res_read = runtime.mqtt_read(None, [], broker="127.0.0.1", topic="sensores/temp")
    assert res_read.ok
    assert res_read.data == "23.5"


def test_imap_read_mocked(monkeypatch):
    class FakeIMAP:
        def __init__(self, host, port, timeout=None): pass
        def login(self, u, p): return ("OK", [b"Logged in"])
        def select(self, f): return ("OK", [b"1"])
        def search(self, charset, crit): return ("OK", [b"101"])
        def fetch(self, mid, cmd):
            raw = (
                b"Subject: =?utf-8?q?Prueba_de_asunto?=\r\n"
                b"From: remitente@example.com\r\n"
                b"Date: Sun, 13 Sep 2026 12:00:00 +0200\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                b"Cuerpo del mensaje de prueba."
            )
            return ("OK", [(b"101 (RFC822 {150}", raw)])
        def logout(self): return ("BYE", [b"Logged out"])

    from dedalo import secrets
    monkeypatch.setattr(secrets, "get", lambda name: "fake_pwd" if name == "mi_correo" else "")
    monkeypatch.setattr(runtime.imaplib, "IMAP4_SSL", FakeIMAP)

    res = runtime.imap_read(None, [], host="imap.example.com", username="user@example.com", secret="mi_correo")
    assert res.ok
    assert len(res.data) == 1
    assert res.data[0]["subject"] == "Prueba de asunto"
    assert res.data[0]["from"] == "remitente@example.com"
    assert "Cuerpo del mensaje" in res.data[0]["body"]


def test_delay_execution():
    import time
    inp = runtime.NodeResult("ok", data={"key": "val"})
    start = time.time()
    res = runtime.delay_execution(None, [inp], seconds=0.05, jitter=0.01)
    duration = time.time() - start
    assert res.ok
    assert res.data == {"key": "val"}
    assert duration >= 0.04


def test_datetime_ops():
    # 1. Preset español largo con salida dict
    inp = runtime.NodeResult("ok", data="2026-09-13T14:30:00Z")
    res = runtime.datetime_ops(None, [inp], preset="es_long", timezone="UTC", output_format="dict")
    assert res.ok
    assert "13 de Septiembre de 2026" in res.data["formatted"]
    assert res.data["year"] == 2026
    assert res.data["month"] == 9
    assert res.data["day"] == 13

    # 2. Offset (+2 días, -1 hora) con salida text
    res_off = runtime.datetime_ops(None, [inp], preset="date_only", offset_days=2, offset_hours=-1, timezone="UTC", output_format="text")
    assert res_off.ok
    assert res_off.data == "2026-09-15"

    # 3. Custom format con salida text
    res_cust = runtime.datetime_ops(None, [inp], preset="custom", format_str="%Y/%m/%d %Hh", timezone="UTC", output_format="text")
    assert res_cust.ok
    assert res_cust.data == "2026/09/13 14h"


def test_sitemap_read_mocked(monkeypatch):
    xml_sample = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url>
            <loc>https://example.com/noticia-tecnologia</loc>
            <lastmod>2026-09-13T10:00:00+00:00</lastmod>
        </url>
        <url>
            <loc>https://example.com/receta-cocina</loc>
            <lastmod>2026-09-10T10:00:00+00:00</lastmod>
        </url>
    </urlset>
    """

    class FakeResponse:
        status_code = 200
        text = xml_sample
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs): return FakeResponse()

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    # 1. Sin filtro
    res = runtime.sitemap_read(None, [], url="https://example.com/sitemap.xml", max_urls=10)
    assert res.ok
    assert len(res.data) == 2
    assert res.data[0]["url"] == "https://example.com/noticia-tecnologia"

    # 2. Con filtro de palabra clave
    res_kw = runtime.sitemap_read(None, [], url="https://example.com/sitemap.xml", filter_keyword="tecnologia")
    assert res_kw.ok
    assert len(res_kw.data) == 1
    assert "tecnologia" in res_kw.data[0]["url"]


def test_healthcheck_ping_mocked(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        is_success = True

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return FakeResponse()
        def post(self, url, content=None, **kwargs):
            calls.append(("POST", url, content))
            return FakeResponse()

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    # 1. Healthchecks.io con éxito
    res_ok = runtime.healthcheck_ping(None, [runtime.NodeResult("ok", data="todo bien")], url="https://hc-ping.com/abc-123")
    assert res_ok.ok
    assert calls[-1] == ("GET", "https://hc-ping.com/abc-123")

    # 2. Healthchecks.io con fallo previo -> debe añadir /fail
    res_fail = runtime.healthcheck_ping(None, [runtime.NodeResult("error", error="falló")], url="https://hc-ping.com/abc-123")
    assert res_fail.ok
    assert calls[-1] == ("GET", "https://hc-ping.com/abc-123/fail")

    # 3. Uptime Kuma push
    res_kuma = runtime.healthcheck_ping(None, [runtime.NodeResult("ok")], url="http://kuma:3001/api/push/xyz")
    assert res_kuma.ok
    assert "status=up&msg=OK" in calls[-1][1]


def test_weather_forecast_mocked(monkeypatch):
    class FakeGeoResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "results": [
                    {"name": "Valencia", "country": "España", "latitude": 39.47, "longitude": -0.38}
                ]
            }

    class FakeWeatherResponse:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {
                "current": {
                    "temperature_2m": 22.4,
                    "apparent_temperature": 23.1,
                    "relative_humidity_2m": 60,
                    "wind_speed_10m": 14.5,
                    "precipitation": 0.0,
                    "weather_code": 1,
                },
                "daily": {
                    "time": ["2026-09-13"],
                    "temperature_2m_max": [26.0],
                    "temperature_2m_min": [18.0],
                    "precipitation_probability_max": [10],
                    "weather_code": [1],
                }
            }

    class FakeClient:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            if "geocoding" in url:
                return FakeGeoResponse()
            return FakeWeatherResponse()

    monkeypatch.setattr(runtime.httpx, "Client", FakeClient)

    res = runtime.weather_forecast(None, [], city="Valencia", format="summary")
    assert res.ok
    assert res.data["city"] == "Valencia, España"
    assert res.data["temperature"] == 22.4
    assert "Mayormente despejado" in res.data["weather_desc"]
    assert "🌤️" in res.data["markdown"]
    assert "22.4°C" in res.data["markdown"]







