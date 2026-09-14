"""Tests para el compilador de grafos de Dédalo Standalone."""

import pytest
from dedalo import compiler


def _node(nid: str, ntype: str, config: dict = None) -> compiler.Node:
    return compiler.Node(id=nid, type=ntype, config=config or {})


def _edge(src: str, tgt: str, port: str = "ok") -> compiler.Edge:
    return compiler.Edge(source=src, target=tgt, port=port)


def test_linear_chain():
    g = compiler.Graph(
        name="cadena",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "transform.threshold", {"comparison": "menor_que", "limit": 20}),
            _node("n3", "output.alert", {"text": "alerta"}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n3")],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "r_n1 = node_n1(ctx, [])" in src
    assert "r_n2 = node_n2(ctx, [r_n1]) if r_n1.ok else SKIPPED" in src
    assert "r_n3 = node_n3(ctx, [r_n2]) if r_n2.ok else SKIPPED" in src


def test_fan_in_policies():
    g_all = compiler.Graph(
        name="fan_all",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "input.cpu_load"),
            _node("n3", "transform.merge", {"fan_in": "all"}),
        ],
        edges=[_edge("n1", "n3"), _edge("n2", "n3")],
    )
    src_all = compiler.compile(g_all)
    assert "(r_n1.ok and r_n2.ok)" in src_all

    g_any = compiler.Graph(
        name="fan_any",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "input.cpu_load"),
            _node("n3", "transform.merge", {"fan_in": "any"}),
        ],
        edges=[_edge("n1", "n3"), _edge("n2", "n3")],
    )
    src_any = compiler.compile(g_any)
    assert "(r_n1.ok or r_n2.ok)" in src_any


def test_cycle_rejected():
    g = compiler.Graph(
        name="ciclo",
        nodes=[
            _node("n1", "input.disk_free"),
            _node("n2", "transform.threshold", {"comparison": "menor_que", "limit": 20}),
        ],
        edges=[_edge("n1", "n2"), _edge("n2", "n1")],
    )
    with pytest.raises(compiler.GraphError) as exc:
        compiler.compile(g)
    assert any("ciclo" in e for e in exc.value.errors)


def test_script_node_validation():
    g = compiler.Graph(
        name="script_val",
        nodes=[
            _node("fuente", "input.disk_free"),
            _node("proc", "transform.script", {"code": "return entrada['no_existe']"}),
        ],
        edges=[_edge("fuente", "proc")],
    )
    with pytest.raises(compiler.GraphError) as exc:
        compiler.compile(g)
    assert any("no está conectado" in e for e in exc.value.errors)


def test_new_nodes_compile_and_execute(tmp_path):
    db_file = str(tmp_path / "comp_test.db")
    g = compiler.Graph(
        name="test_new_nodes",
        nodes=[
            _node("hook", "input.webhook"),
            _node("tmpl", "transform.template", {"template": "Evento: {evento}"}),
            _node("router", "transform.router", {"condition": "contains", "value": "alerta"}),
            _node("store", "output.sqlite_exec", {"path": db_file, "query": "CREATE TABLE IF NOT EXISTS t (m TEXT)"}),
        ],
        edges=[
            _edge("hook", "tmpl", "ok"),
            _edge("tmpl", "router", "ok"),
            _edge("router", "store", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.webhook_trigger" in src
    assert "runtime.template" in src
    assert "runtime.router" in src
    assert "runtime.sqlite_exec" in src

    # Ejecutar el código compilado
    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_web_search_node_compiles():
    g = compiler.Graph(
        name="test_search",
        nodes=[
            _node("buscar", "input.web_search", {"query": "noticias python", "engine": "duckduckgo", "limit": 3}),
            _node("guardar", "output.file", {"filename": "noticias.txt", "mode": "sobrescribir"}),
        ],
        edges=[
            _edge("buscar", "guardar", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.web_search" in src
    assert "query='noticias python'" in src
    assert "engine='duckduckgo'" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_deterministic_nodes_compile():
    g = compiler.Graph(
        name="test_det_nodes",
        nodes=[
            _node("ssl", "input.ssl_cert", {"host": "example.com"}),
            _node("dns", "input.dns_check", {"host": "example.com"}),
            _node("ping", "input.ping", {"host": "example.com"}),
            _node("imap", "input.imap", {"host": "imap.example.com", "username": "me@example.com"}),
            _node("diff", "transform.diff", {"key_field": "url", "mode": "added"}),
            _node("dedup", "transform.fuzzy_dedup", {"field": "title", "threshold": 0.8}),
            _node("agg", "transform.aggregate", {"field": "price", "operation": "avg"}),
            _node("table", "transform.table", {"format": "markdown", "columns": "a,b"}),
            _node("rake", "transform.keywords_rake", {"max_keywords": 3}),
            _node("pub_mqtt", "output.mqtt", {"topic": "home/alert"}),
        ],
        edges=[
            _edge("ssl", "diff", "ok"),
            _edge("diff", "dedup", "ok"),
            _edge("dedup", "agg", "ok"),
            _edge("agg", "table", "ok"),
            _edge("table", "rake", "ok"),
            _edge("rake", "pub_mqtt", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.ssl_cert" in src
    assert "runtime.dns_check" in src
    assert "runtime.ping_host" in src
    assert "runtime.imap_read" in src
    assert "runtime.diff_data" in src
    assert "runtime.fuzzy_dedup" in src
    assert "runtime.aggregate" in src
    assert "runtime.format_table" in src
    assert "runtime.keywords_rake" in src
    assert "runtime.mqtt_publish" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_n8n_nodes_compile():
    g = compiler.Graph(
        name="test_n8n_nodes",
        nodes=[
            _node("clima", "input.weather", {"city": "Valencia", "format": "summary"}),
            _node("mapa", "input.sitemap", {"url": "https://example.com/sitemap.xml", "max_urls": 20}),
            _node("tiempo", "transform.datetime", {"preset": "es_long", "timezone": "Europe/Madrid"}),
            _node("pausa", "transform.delay", {"seconds": 1.5, "jitter": 0.5}),
            _node("ping", "output.healthcheck", {"url": "https://hc-ping.com/123", "method": "GET"}),
        ],
        edges=[
            _edge("clima", "tiempo", "ok"),
            _edge("tiempo", "pausa", "ok"),
            _edge("pausa", "ping", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.weather_forecast" in src
    assert "runtime.sitemap_read" in src
    assert "runtime.datetime_ops" in src
    assert "runtime.delay_execution" in src
    assert "runtime.healthcheck_ping" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_secops_pipeline_compilation():
    g = compiler.Graph(
        name="secops_pipeline",
        nodes=[
            _node("telemetry", "input.telemetry", {"incident_id": "inc-101", "resource": "k8s/cluster-a"}),
            _node("vault", "output.evidence_vault", {"compress": True}),
            _node("analyzer", "transform.analyzer", {"sanitize_secrets": True}),
            _node("planner", "transform.planner", {"output_format": "markdown"}),
        ],
        edges=[
            _edge("telemetry", "vault", "ok"),
            _edge("vault", "analyzer", "ok"),
            _edge("analyzer", "planner", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.telemetry_input" in src
    assert "runtime.evidence_vault" in src
    assert "runtime.analyzer_agent" in src
    assert "runtime.planner_agent" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_atlassian_nodes_compilation():
    g = compiler.Graph(
        name="atlassian_pipeline",
        nodes=[
            _node("jira_in", "input.jira_issues", {"instance_url": "https://test.atlassian.net", "jql": "project=SEC"}),
            _node("conf_in", "input.confluence_pages", {"instance_url": "https://test.atlassian.net/wiki", "space_key": "SEC"}),
            _node("jira_out", "output.jira_issue", {"instance_url": "https://test.atlassian.net", "project_key": "SEC"}),
            _node("conf_out", "output.confluence_page", {"instance_url": "https://test.atlassian.net/wiki", "space_key": "SEC"}),
        ],
        edges=[
            _edge("jira_in", "jira_out", "ok"),
            _edge("conf_in", "conf_out", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.jira_issues_input" in src
    assert "runtime.confluence_pages_input" in src
    assert "runtime.jira_issue_output" in src
    assert "runtime.confluence_page_output" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])


def test_cve_audit_pipeline_compilation():
    g = compiler.Graph(
        name="cve_audit_pipeline",
        nodes=[
            _node("sbom_file", "input.json_file", {"path": "bom.json"}),
            _node("audit", "transform.cve_audit", {"severity_threshold": "HIGH"}),
            _node("report", "output.file", {"filename": "cve_report.md"}),
        ],
        edges=[
            _edge("sbom_file", "audit", "ok"),
            _edge("audit", "report", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.cve_audit" in src

    # Probar compilación standalone con tree-shaking
    standalone = compiler.compile_standalone(g)
    assert "import httpx" in standalone
    assert "def cve_audit(" in standalone
    assert "def run(ctx):" in standalone


def test_secops_and_network_nodes_compilation():
    g = compiler.Graph(
        name="secops_extra_pipeline",
        nodes=[
            _node("rdap", "input.whois_rdap", {"domain": "miempresa.com"}),
            _node("dns", "input.dns_record", {"name": "miempresa.com", "type": "TXT"}),
            _node("scanner", "transform.secret_scanner", {"mode": "detect_and_redact"}),
            _node("jpath", "transform.json_path", {"path": "registrar"}),
            _node("mapper", "transform.data_mapper", {"mapping": '{"dom": "domain"}'}),
            _node("teams", "output.teams", {"webhook_url": "https://outlook.office.com/webhook/test"}),
        ],
        edges=[
            _edge("rdap", "jpath", "ok"),
            _edge("jpath", "mapper", "ok"),
            _edge("dns", "scanner", "ok"),
            _edge("scanner", "teams", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.whois_rdap" in src
    assert "runtime.dns_record" in src
    assert "runtime.secret_scanner" in src
    assert "runtime.json_path_extract" in src
    assert "runtime.data_mapper" in src
    assert "runtime.teams_webhook" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])

    standalone = compiler.compile_standalone(g)
    assert "import httpx" in standalone
    assert "def whois_rdap(" in standalone
    assert "def dns_record(" in standalone
    assert "def secret_scanner(" in standalone
    assert "def json_path_extract(" in standalone
    assert "def data_mapper(" in standalone
    assert "def teams_webhook(" in standalone
    assert "def run(ctx):" in standalone


def test_approval_node_compilation():
    g = compiler.Graph(
        name="pipeline_with_approval",
        nodes=[
            _node("hook", "input.webhook"),
            _node("gate", "transform.approval", {"prompt_message": "¿Continuar con despliegue?", "on_reject": "skip"}),
            _node("alert", "output.alert", {"text": "Desplegado"}),
        ],
        edges=[
            _edge("hook", "gate", "ok"),
            _edge("gate", "alert", "ok"),
        ],
    )
    src = compiler.compile(g)
    assert "def run(ctx):" in src
    assert "runtime.human_approval" in src

    ns = {}
    exec(src, ns)
    assert callable(ns["run"])

    standalone = compiler.compile_standalone(g)
    assert "def human_approval(" in standalone
    assert "--yes" in standalone
    assert "--non-interactive" in standalone
    assert "def run(ctx):" in standalone








