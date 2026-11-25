"""Tests for flows served as tools: input.mcp, output.return, the /mcp endpoint and typed tool.flow."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from dedalo import compiler, history, mcp_server, runtime, scheduler, store
from dedalo.app import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer s3cret"}

HAIKU = "Pasan los tests hoy,\nen producción algo arde:\nera la caché."


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEDALO_MCP_TOKEN", "s3cret")
    history.init_db()
    yield tmp_path
    for t in threading.enumerate():  # a timed-out call is still running: let it end inside the test
        if t.name.startswith("dedalo-mcp"):
            t.join(10)


def _flow(fid: str, nodes: list[dict], edges: list[tuple[str, str]]) -> None:
    graph = {"version": 1, "name": fid, "nodes": nodes, "edges": [{"source": a, "target": b, "port": "ok"} for a, b in edges]}
    store.save_flow(fid, graph, force=True)


def _haiku_flow() -> None:
    """Fully deterministic tool: no model, a script that returns one haiku."""
    _flow(
        "haiku",
        [
            {"id": "start", "type": "input.mcp", "config": {"tool_name": "haiku", "description": "Returns a haiku about software."}},
            {"id": "poem", "type": "transform.script", "config": {"code": f"return {HAIKU!r}"}},
            {"id": "out", "type": "output.return", "config": {}},
        ],
        [("start", "poem"), ("poem", "out")],
    )


GREET_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "times": {"type": "integer"}}, "required": ["name"]}
GREET_RESULT = {"type": "object", "properties": {"greeting": {"type": "string"}}, "required": ["greeting"]}


def _greet_flow(run_outputs: bool = False) -> None:
    _flow(
        "greet",
        [
            {"id": "start", "type": "input.mcp", "config": {"description": "Greets someone.", "parameters": json.dumps(GREET_SCHEMA), "run_outputs": run_outputs}},
            {"id": "make", "type": "transform.script", "config": {"code": "a = entrada['start']\nreturn {'greeting': ' '.join(['hola ' + a['name']] * a.get('times', 1))}"}},
            {"id": "out", "type": "output.return", "config": {"schema": json.dumps(GREET_RESULT)}},
            {"id": "side", "type": "output.state", "config": {"key": "last", "scope": "greet-side"}},
        ],
        [("start", "make"), ("make", "out"), ("make", "side")],
    )


def _rpc(method: str, params: dict | None = None, msg_id: int | None = 1, headers: dict | None = None):
    body: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=AUTH if headers is None else headers)


# -- Endpoint ------------------------------------------------------------------


def test_endpoint_is_disabled_without_a_token_and_rejects_bad_tokens(monkeypatch):
    assert _rpc("ping", headers={}).status_code == 401
    assert _rpc("ping", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/mcp?token=s3cret", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert _rpc("ping", headers={"X-Dedalo-Token": "s3cret"}).json()["result"] == {}
    monkeypatch.delenv("DEDALO_MCP_TOKEN")
    assert _rpc("ping").status_code == 404


def test_handshake_notifications_and_protocol_errors():
    init = _rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}).json()["result"]
    assert init["protocolVersion"] == "2025-03-26" and "tools" in init["capabilities"]
    assert _rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]
    assert _rpc("notifications/initialized", msg_id=None).status_code == 202
    assert client.get("/mcp", headers=AUTH).status_code == 405
    assert _rpc("resources/list").json()["error"]["code"] == -32601
    assert client.post("/mcp", content=b"{nope", headers=AUTH).json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[1], headers=AUTH).json()["error"]["code"] == -32600
    assert _rpc("tools/call", {"name": "missing", "arguments": {}}).json()["error"]["code"] == -32602


def test_haiku_tool_is_listed_and_returns_the_haiku():
    _haiku_flow()
    _flow("plain", [{"id": "s", "type": "transform.script", "config": {"code": "return 1"}}], [])  # no input.mcp: not served

    tools = _rpc("tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["haiku"]
    assert tools[0]["description"] == "Returns a haiku about software."
    assert tools[0]["inputSchema"] == {"type": "object", "properties": {}}
    assert tools[0]["annotations"] == {"readOnlyHint": True}

    result = _rpc("tools/call", {"name": "haiku", "arguments": {}}).json()["result"]
    assert result == {"content": [{"type": "text", "text": HAIKU}], "isError": False}
    assert history.list_runs(flow_id="haiku")[0]["trigger_type"] == "mcp"


def test_typed_arguments_structured_result_and_outputs_skipped_by_default():
    _greet_flow()
    tool = _rpc("tools/list").json()["result"]["tools"][0]
    assert tool["name"] == "greet" and tool["inputSchema"]["required"] == ["name"]
    assert tool["outputSchema"] == GREET_RESULT

    result = _rpc("tools/call", {"name": "greet", "arguments": {"name": "Ada", "times": 2}}).json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == {"greeting": "hola Ada hola Ada"}
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert runtime._state_load("greet-side") == {}  # output.state did not run

    bad = _rpc("tools/call", {"name": "greet", "arguments": {"times": "x"}}).json()["result"]
    assert bad["isError"] is True and "name" in bad["content"][0]["text"]
    assert len(history.list_runs(flow_id="greet")) == 1  # the invalid call never started the flow


def test_run_outputs_runs_the_output_nodes_and_drops_the_read_only_hint():
    _greet_flow(run_outputs=True)
    assert _rpc("tools/list").json()["result"]["tools"][0]["annotations"] == {"readOnlyHint": False}
    result = _rpc("tools/call", {"name": "greet", "arguments": {"name": "Ada"}}).json()["result"]
    assert result["structuredContent"] == {"greeting": "hola Ada"}  # still the output.return node, not every sink
    assert runtime._state_load("greet-side") == {"last": {"greeting": "hola Ada"}}


def test_flow_errors_disabled_flows_and_duplicate_names():
    _flow(
        "broken",
        [
            {"id": "start", "type": "input.mcp", "config": {}},
            {"id": "boom", "type": "transform.script", "config": {"code": "raise ValueError('kaput')"}},
            {"id": "out", "type": "output.return", "config": {}},
        ],
        [("start", "boom"), ("boom", "out")],
    )
    result = _rpc("tools/call", {"name": "broken", "arguments": {}}).json()["result"]
    assert result["isError"] is True and "kaput" in result["content"][0]["text"]

    _haiku_flow()
    _flow("haiku2", [{"id": "start", "type": "input.mcp", "config": {"tool_name": "haiku"}}], [])
    assert sorted(t["name"] for t in mcp_server.list_tools()) == ["broken", "haiku"]
    assert mcp_server.contracts()["haiku"]["flow"] == "haiku"

    store.update_meta("haiku", {"enabled": False})
    store.update_meta("haiku2", {"enabled": False})
    assert [t["name"] for t in mcp_server.list_tools()] == ["broken"]


def test_call_times_out_and_reports_it(monkeypatch):
    _flow(
        "slow",
        [
            {"id": "start", "type": "input.mcp", "config": {"timeout": 1}},
            {"id": "wait", "type": "transform.script", "config": {"code": "import time\ntime.sleep(3)\nreturn 'late'"}},
        ],
        [("start", "wait")],
    )
    result = mcp_server.call_tool("slow", {})
    assert result["isError"] is True and "background" in result["content"][0]["text"]


# -- Nodes ---------------------------------------------------------------------


def test_input_mcp_validates_the_payload_and_tolerates_manual_runs():
    ctx = scheduler.FlowContext(flow_name="f")
    assert runtime.mcp_trigger(ctx, []).data == {}
    ctx.payload = {"name": "Ada"}
    assert runtime.mcp_trigger(ctx, [], parameters=json.dumps(GREET_SCHEMA)).data == {"name": "Ada"}
    ctx.payload = '{"name": "Ada"}'  # payload typed as text in input.subflows
    assert runtime.mcp_trigger(ctx, [], parameters=json.dumps(GREET_SCHEMA)).data == {"name": "Ada"}
    ctx.payload = {}
    assert "name" in runtime.mcp_trigger(ctx, [], parameters=json.dumps(GREET_SCHEMA)).error
    assert runtime.mcp_trigger(ctx, [], parameters="[1]").failed
    assert runtime.mcp_trigger(ctx, [], parameters='{"type": "array"}').failed


def test_output_return_passes_data_through_and_enforces_its_schema():
    ok = runtime.NodeResult("ok", data={"greeting": "hi"})
    assert runtime.return_result(None, [ok]).data == {"greeting": "hi"}
    assert runtime.return_result(None, [ok], schema=json.dumps(GREET_RESULT)).data == {"greeting": "hi"}
    fenced = runtime.NodeResult("ok", data='```json\n{"greeting": "hi"}\n```')  # a model's JSON answer
    assert runtime.return_result(None, [fenced], schema=json.dumps(GREET_RESULT)).data == {"greeting": "hi"}
    assert "greeting" in runtime.return_result(None, [runtime.NodeResult("ok", data={})], schema=json.dumps(GREET_RESULT)).error
    assert runtime.return_result(None, [runtime.NodeResult("ok", data="prose")], schema=json.dumps(GREET_RESULT)).failed
    assert runtime.return_result(None, [ok], schema="{nope").failed
    text = runtime.NodeResult("ok", data="plain")
    assert runtime.return_result(None, [text], schema='{"type": "string"}').data == "plain"


def test_a_flow_takes_one_start_and_one_return_node():
    graph = compiler.Graph.from_dict({"name": "f", "nodes": [
        {"id": "a", "type": "input.mcp"}, {"id": "b", "type": "input.mcp"},
        {"id": "c", "type": "output.return"}, {"id": "d", "type": "output.return"},
    ], "edges": [{"source": "a", "target": "c"}, {"source": "b", "target": "d"}]})
    errors = compiler.validate(graph)
    assert any("input.mcp" in e for e in errors) and any("output.return" in e for e in errors)


# -- tool.flow -----------------------------------------------------------------


def test_tool_flow_takes_name_description_and_arguments_from_the_flow():
    _greet_flow()
    ctx = scheduler.FlowContext(flow_name="agent")
    (tool,) = runtime._build_flow_tools({"flow": "greet"}, ctx)
    assert tool.name == "greet" and tool.description == "Greets someone."
    assert tool.parameters["required"] == ["name"] and tool.risk == "read"
    assert json.loads(tool.handler({"name": "Ada"})) == {"greeting": "hola Ada"}
    assert tool.handler({}).startswith("Error:")

    (renamed,) = runtime._build_flow_tools({"flow": "greet", "name": "say hi", "description": "d", "skip_outputs": False}, ctx)
    assert (renamed.name, renamed.description, renamed.risk) == ("say_hi", "d", "write")


def test_tool_flow_without_input_mcp_keeps_the_single_text_input():
    _flow("echo", [
        {"id": "in", "type": "input.webhook", "config": {}},
        {"id": "s", "type": "transform.script", "config": {"code": "return 'got ' + str(entrada['in'])"}},
    ], [("in", "s")])
    (tool,) = runtime._build_flow_tools({"flow": "echo"}, scheduler.FlowContext(flow_name="agent"))
    assert tool.name == "flow_echo" and list(tool.parameters["properties"]) == ["input"]
    assert tool.handler({"input": "x"}) == "got x"


def test_the_harness_mcp_client_can_use_the_server(monkeypatch):
    """Dédalo's own tool.mcp client against Dédalo's server: the loop closes."""
    _haiku_flow()

    class Bridge:  # httpx.Client stand-in that routes to the test app
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return client.post("/mcp", json=json, headers=headers)

    monkeypatch.setattr(runtime.httpx, "Client", Bridge)
    monkeypatch.setattr(runtime.secrets, "get", lambda name: "s3cret")
    tools = runtime._build_mcp_tools({"url": "http://testserver/mcp", "secret": "mcp-token"}, None)
    assert [(t.name, t.risk) for t in tools] == [("haiku", "read")]
    assert tools[0].handler({}) == HAIKU
