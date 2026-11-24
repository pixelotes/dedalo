"""Tests for transform.harness, the tool.* nodes, Telegram approvals and the shared state store."""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from dedalo import compiler, runtime
from dedalo.runtime import (
    AgentTool,
    NodeResult,
    _mail_list,
    _mail_read,
    _react_loop,
    _telegram_approver,
    harness_agent,
    state_read,
    state_write,
    tool_http,
    tool_mail,
    tool_mcp,
    tool_memory,
)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """The harness writes state and usage under data/: keep it out of the repository."""
    monkeypatch.chdir(tmp_path)


class Ctx:
    flow_name = "invoices"


def _llm_reply(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> MagicMock:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"choices": [{"message": msg}]}
    return resp


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


HARNESS_KW = dict(url="http://llm.test/v1", model="m", max_context=8192)


# -- compiler ------------------------------------------------------------------


def _graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> compiler.Graph:
    return compiler.Graph.from_dict({"name": "t", "nodes": nodes, "edges": edges})


def test_tool_edges_compile_without_fan_in_and_do_not_guard():
    g = _graph(
        [
            {"id": "rss", "type": "input.rss", "config": {"url": "https://example.com/feed"}},
            {"id": "mail", "type": "tool.mail", "config": {"host": "imap.test", "username": "u"}},
            {"id": "mem", "type": "tool.memory", "config": {}},
            {"id": "agent", "type": "transform.harness", "config": {"prompt": "x"}},
            {"id": "tg", "type": "output.telegram", "config": {}},
        ],
        [
            {"source": "rss", "target": "agent"},
            {"source": "mail", "target": "agent"},
            {"source": "mem", "target": "agent"},
            {"source": "agent", "target": "tg"},
        ],
    )
    assert compiler.validate(g) == []
    code = compiler.compile(g)
    # The harness runs when its data input is ok; tool edges do not take part in the guard
    assert "r_agent = node_agent(ctx, [r_rss, r_mail, r_mem]) if r_rss.ok else SKIPPED" in code


def test_tool_nodes_only_connect_to_a_harness():
    g = _graph(
        [
            {"id": "mail", "type": "tool.mail", "config": {"host": "h", "username": "u"}},
            {"id": "tg", "type": "output.telegram", "config": {}},
            {"id": "rss", "type": "input.rss", "config": {"url": "https://example.com"}},
        ],
        [{"source": "mail", "target": "tg"}, {"source": "rss", "target": "mail"}],
    )
    errors = " | ".join(compiler.validate(g))
    assert "can only connect to an Agent Harness" in errors
    assert "tool nodes take no inputs" in errors


def test_invoice_template_compiles():
    from dedalo.templates import get_template

    tpl = get_template("avisar-facturas")
    assert tpl is not None
    g = compiler.Graph.from_dict(tpl)
    assert compiler.validate(g) == []
    compiler.compile(g)


# -- harness loop --------------------------------------------------------------


def test_harness_runs_memory_tool_and_returns_response(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mem = tool_memory(Ctx(), [])
    responses = [
        _llm_reply(tool_calls=[_call("memory_set", {"key": "last", "value": {"n": 2}})]),
        _llm_reply("Done: 2 invoices"),
    ]
    with patch("httpx.Client.post", side_effect=responses):
        res = harness_agent(Ctx(), [mem, NodeResult("ok", data="some context")], prompt="go", **HARNESS_KW)
    assert res.ok, res.error
    assert res.data["response"] == "Done: 2 invoices"
    assert res.data["tools"] == ["memory_get", "memory_set"]
    assert res.data["tool_history"][0]["tool"] == "memory_set"
    stored = json.loads((tmp_path / "data/state/memory/invoices.json").read_text())
    assert stored == {"last": {"n": 2}}
    assert state_read(Ctx(), [], key="last").data == {"n": 2}


def test_harness_skip_if_response_emits_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("httpx.Client.post", return_value=_llm_reply("NO_INVOICES.")):
        res = harness_agent(Ctx(), [], prompt="go", skip_if_response="NO_INVOICES", **HARNESS_KW)
    assert res.skipped


def test_harness_sends_tools_and_current_date_to_the_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("httpx.Client.post", return_value=_llm_reply("ok")) as post:
        harness_agent(Ctx(), [tool_memory(Ctx(), [])], prompt="go", output_format="text", **HARNESS_KW)
    payload = post.call_args.kwargs["json"]
    assert {t["function"]["name"] for t in payload["tools"]} == {"memory_get", "memory_set"}
    assert "Current date and time" in payload["messages"][0]["content"]


def test_harness_rejects_duplicate_tool_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = harness_agent(Ctx(), [tool_memory(Ctx(), []), tool_memory(Ctx(), [])], prompt="go", **HARNESS_KW)
    assert res.failed and "memory_get" in res.error


def _loop(tools: list[AgentTool], approver=None):
    return _react_loop(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=tools, base_url="http://llm.test/v1", target_model="m", api_key="", temperature=0.0,
        context_window=8192, compaction_engine="blackhole", turn_limit=5, max_tool_chars=1000,
        timeout=5.0, approver=approver,
    )


def test_calls_that_need_approval_are_denied_without_a_channel():
    ran: list[dict[str, Any]] = []
    tool = AgentTool("send", "d", {"type": "object"}, lambda a: ran.append(a) or "sent", risk="dangerous", approval="write")
    responses = [_llm_reply(tool_calls=[_call("send", {"to": "x"})]), _llm_reply("gave up")]
    with patch("httpx.Client.post", side_effect=responses) as post:
        out = _loop([tool])
    assert ran == []
    tool_msg = post.call_args_list[1].kwargs["json"]["messages"][-2]  # the list is shared; [-1] is the final reply
    assert tool_msg["role"] == "tool" and "did not approve" in tool_msg["content"]
    assert out["tool_history"][0]["approval"] == "no approval channel configured"


def test_read_tools_skip_approval_in_write_mode_and_approver_is_used_for_writes():
    calls: list[str] = []
    read_tool = AgentTool("look", "d", {}, lambda a: calls.append("look") or "seen", risk="read", approval="write")
    write_tool = AgentTool("change", "d", {}, lambda a: calls.append("change") or "changed", risk="write", approval="write")
    asked: list[str] = []

    def approver(tool, args):
        asked.append(tool.name)
        return True, "approved by test"

    responses = [
        _llm_reply(tool_calls=[_call("look", {}, "a"), _call("change", {}, "b")]),
        _llm_reply("ok"),
    ]
    with patch("httpx.Client.post", side_effect=responses):
        out = _loop([read_tool, write_tool], approver)
    assert calls == ["look", "change"]
    assert asked == ["change"]
    assert out["completed"] is True


# -- Telegram approval ---------------------------------------------------------


def test_telegram_approver_waits_for_the_button(monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_TG_TEST", "123:abc")
    runtime._TG_OFFSET.clear()
    runtime._TG_ANSWERS.clear()
    sent: dict[str, Any] = {}

    def post(self, url, json=None, **kw):
        resp = MagicMock(status_code=200)
        if url.endswith("/sendMessage"):
            sent.update(json)
            resp.json.return_value = {"result": {"message_id": 7}}
        else:
            resp.json.return_value = {"ok": True}
        return resp

    def get(self, url, params=None, **kw):
        data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]  # the Approve button
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"result": [
            {"update_id": 5, "callback_query": {"id": "q", "data": data.replace(":y", ":n").replace("dedalo:", "dedalo:other"), "message": {"chat": {"id": 42}}, "from": {"username": "eve"}}},
            {"update_id": 6, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": 999}}, "from": {"username": "mallory"}}},
            {"update_id": 7, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": 42}}, "from": {"username": "raul"}}},
        ]}
        return resp

    approve = _telegram_approver(token_secret="tg-test", chat_id="42", timeout_s=30, flow_name="invoices")
    tool = AgentTool("mail_send", "d", {}, lambda a: "", risk="dangerous", approval="write")
    with patch("httpx.Client.post", post), patch("httpx.Client.get", get):
        ok, note = approve(tool, {"to": "a@b.c"})
    assert ok is True and note == "approved by raul"  # the button from chat 999 did not count
    assert "mail_send" in sent["text"] and sent["chat_id"] == "42"
    assert runtime._TG_OFFSET["123:abc"] == 8


# -- MCP -----------------------------------------------------------------------


def test_mcp_client_handles_json_and_sse(monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_BEANIE", "tok")
    seen_headers: list[dict[str, str]] = []

    def post(self, url, json=None, headers=None, **kw):
        seen_headers.append(dict(headers))
        resp = MagicMock()
        resp.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "s1"}
        resp.status_code = 200
        method = json.get("method")
        if method == "initialize":
            resp.json.return_value = {"jsonrpc": "2.0", "id": json["id"], "result": {"protocolVersion": "2025-03-26"}}
        elif method == "notifications/initialized":
            resp.status_code = 202
        elif method == "tools/list":
            resp.json.return_value = {"jsonrpc": "2.0", "id": json["id"], "result": {"tools": [
                {"name": "datetime", "description": "now", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
                {"name": "web_fetch", "description": "fetch", "inputSchema": {"type": "object"}},
            ]}}
        elif method == "tools/call":
            resp.headers["Content-Type"] = "text/event-stream"
            body = {"jsonrpc": "2.0", "id": json["id"], "result": {"content": [{"type": "text", "text": "2026-11-24"}]}}
            resp.text = "event: message\ndata: " + __import__("json").dumps(body) + "\n\n"
        return resp

    desc = tool_mcp(Ctx(), [], url="http://127.0.0.1:8092/mcp", secret="beanie", prefix="b_")
    with patch("httpx.Client.post", post):
        tools = runtime._build_mcp_tools(desc.data["config"], Ctx())
        assert [(t.name, t.risk) for t in tools] == [("b_datetime", "read"), ("b_web_fetch", "write")]
        assert tools[0].handler({}) == "2026-11-24"
    assert seen_headers[0]["Authorization"] == "Bearer tok"
    assert seen_headers[-1]["Mcp-Session-Id"] == "s1"
    assert seen_headers[-1]["Mcp-Protocol-Version"] == "2025-03-26"


# -- HTTP tool -----------------------------------------------------------------


def test_http_tool_fills_placeholders_and_sends_query():
    desc = tool_http(Ctx(), [], name="weather", url="https://api.test/city/{city}", parameters='{"type":"object","properties":{"units":{"type":"string"}}}')
    (tool,) = runtime._build_http_tools(desc.data["config"], Ctx())
    assert tool.risk == "read" and set(tool.parameters["properties"]) == {"units", "city"}
    with patch("httpx.Client.request", return_value=MagicMock(status_code=200, text="sunny")) as req:
        assert tool.handler({"city": "A Coruña/x", "units": "c"}) == "sunny"
    args = req.call_args
    assert args.args == ("GET", "https://api.test/city/A%20Coru%C3%B1a%2Fx")
    assert args.kwargs["params"] == {"units": "c"}


def test_tool_descriptors_never_carry_secret_values(monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_MAILPW", "hunter2")
    desc = tool_mail(Ctx(), [], host="imap.test", username="me", secret="mailpw")
    assert "hunter2" not in json.dumps(desc.data)
    assert tool_mail(Ctx(), [], host="h", username="u", actions="list,delete").failed
    assert tool_mail(Ctx(), [], host="h", username="u", approval="sometimes").failed


# -- Mail tool -----------------------------------------------------------------


class FakeIMAP:
    def __init__(self, *a, **kw):
        self.capabilities = ("IMAP4REV1", "MOVE")

    def login(self, u, p):
        assert p == "pw"

    def select(self, folder):
        return "OK", [b"3"]

    def logout(self):
        pass

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            assert "SINCE" in args
            return "OK", [b"10 11"]
        if cmd == "FETCH" and "HEADER.FIELDS" in args[1]:
            return "OK", [
                (b'1 (UID 11 FLAGS (\\Seen) RFC822.SIZE 20480 BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {60}',
                 b"From: Iberdrola <f@iberdrola.es>\r\nSubject: =?utf-8?q?Factura_n=C2=BA_7?=\r\n\r\n"),
                b' BODYSTRUCTURE (("TEXT" "PLAIN" NIL NIL NIL "7BIT" 10 1)("APPLICATION" "PDF" ("NAME" "factura-7.pdf") NIL NIL "BASE64" 100 NIL ("ATTACHMENT" ("FILENAME" "factura-7.pdf"))) "MIXED"))',
                (b"2 (UID 10 FLAGS () RFC822.SIZE 1024 BODYSTRUCTURE (\"TEXT\" \"PLAIN\" NIL NIL NIL \"7BIT\" 10 1) BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {40}",
                 b"From: Ana <ana@x.es>\r\nSubject: Hola\r\n\r\n"),
                b")",
            ]
        if cmd == "FETCH":
            raw = (b"From: Iberdrola <f@iberdrola.es>\r\nSubject: Factura\r\nMessage-ID: <m1@x>\r\n"
                   b"Content-Type: text/plain; charset=utf-8\r\n\r\nImporte: 42,10 EUR\r\n")
            return "OK", [(b"1 (UID 11 BODY[] {100}", raw), b")"]
        raise AssertionError(cmd)


def test_mail_list_and_read(monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_MAILPW", "pw")
    cfg = tool_mail(Ctx(), [], host="imap.test", username="me", secret="mailpw").data["config"]
    with patch("imaplib.IMAP4_SSL", FakeIMAP):
        listed = json.loads(_mail_list(cfg, {"since_days": 1}))
        filtered = json.loads(_mail_list(cfg, {"subject_contains": "factura"}))
        read = json.loads(_mail_read(cfg, {"id": "11"}))
    assert listed["count"] == 2
    first = listed["emails"][0]
    assert first["id"] == "11" and first["subject"] == "Factura nº 7" and first["attachments"] == ["factura-7.pdf"]
    assert first["seen"] is True and first["size_kb"] == 20
    assert [e["id"] for e in filtered["emails"]] == ["11"]
    assert "42,10 EUR" in read["body"] and read["message_id"] == "<m1@x>"
    with pytest.raises(ValueError):
        _mail_read(cfg, {"id": "11 OR 1"})


def test_mail_tools_by_action_and_risk():
    cfg = tool_mail(Ctx(), [], host="h", username="u", actions="list,read,flag,send").data["config"]
    tools = {t.name: t for t in runtime._build_mail_tools(cfg, Ctx())}
    assert set(tools) == {"mail_list", "mail_read", "mail_flag", "mail_send"}
    assert not tools["mail_read"].needs_approval()
    assert tools["mail_flag"].needs_approval() and tools["mail_send"].risk == "dangerous"
    assert "not configured" in tools["mail_send"].handler({"to": "a@b.c", "subject": "s", "body": "b"})


# -- State nodes ---------------------------------------------------------------


def test_state_write_set_and_append(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert state_write(Ctx(), [NodeResult("ok", data={"a": 1})], key="cfg").ok
    state_write(Ctx(), [NodeResult("ok", data="x")], key="log", mode="append")
    state_write(Ctx(), [NodeResult("ok", data="y")], key="log", mode="append")
    assert state_read(Ctx(), [], key="log").data == ["x", "y"]
    assert state_read(Ctx(), []).data == {"cfg": {"a": 1}, "log": ["x", "y"]}
    assert state_read(Ctx(), [], key="missing", default="none").data == "none"
    assert state_write(Ctx(), [NodeResult("ok", data=1)], key="../evil").failed
    assert state_write(Ctx(), [NodeResult("ok", data="z" * 300)], key="big", max_bytes=200).failed


def test_workers_ai_null_fields_are_normalized_before_the_next_turn():
    # Workers AI returns "role": null, "content": null and tool calls with "id": null
    raw = MagicMock(status_code=200)
    raw.json.return_value = {"choices": [{"message": {
        "role": None, "content": None, "reasoning_content": None,
        "tool_calls": [{"id": None, "type": "function", "function": {"name": "look", "arguments": {"q": 1}}}],
    }}]}
    seen: list[dict[str, Any]] = []
    tool = AgentTool("look", "d", {}, lambda a: seen.append(a) or "result", risk="read")
    with patch("httpx.Client.post", side_effect=[raw, _llm_reply("done")]) as post:
        out = _loop([tool])
    assert seen == [{"q": 1}] and out["response"] == "done"
    sent = post.call_args_list[1].kwargs["json"]["messages"]
    assistant, tool_msg = sent[2], sent[3]
    assert assistant["role"] == "assistant" and assistant["content"] == ""
    assert "reasoning_content" not in assistant
    call = assistant["tool_calls"][0]
    assert call["id"] and call["id"] == tool_msg["tool_call_id"]
    assert call["function"]["arguments"] == '{"q": 1}'


def test_workers_ai_merged_tool_calls_are_split():
    # Workers AI merges parallel calls to one tool into one call with the JSON objects back to back
    raw = MagicMock(status_code=200)
    raw.json.return_value = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "look",
         "arguments": '{"key": "a"}{"key": "b"} {"key": "c"}'}},
        {"id": "c2", "type": "function", "function": {"name": "look", "arguments": "not json"}},
    ]}}]}
    seen: list[dict[str, Any]] = []
    tool = AgentTool("look", "d", {}, lambda a: seen.append(a) or "result", risk="read")
    with patch("httpx.Client.post", side_effect=[raw, _llm_reply("done")]) as post:
        out = _loop([tool])
    assert seen == [{"key": "a"}, {"key": "b"}, {"key": "c"}] and out["response"] == "done"
    sent = post.call_args_list[1].kwargs["json"]["messages"]
    calls = sent[2]["tool_calls"]
    assert [c["id"] for c in calls] == ["c1", "c1_1", "c1_2", "c2"]
    for c in calls:
        json.loads(c["function"]["arguments"])  # every call sent back is valid JSON
    assert "_dedalo_invalid_calls" not in sent[2]
    results = {m["tool_call_id"]: m["content"] for m in sent if m.get("role") == "tool"}
    assert set(results) == {"c1", "c1_1", "c1_2", "c2"}
    assert "not valid JSON" in results["c2"]


@pytest.mark.parametrize("model,mode,body,suffix", [
    ("@cf/google/gemma-4-26b-a4b-it", "off", {"chat_template_kwargs": {"enable_thinking": False, "thinking": False}}, None),
    ("@cf/google/gemma-4-26b-a4b-it", "on", {"chat_template_kwargs": {"enable_thinking": True, "thinking": True}}, None),
    ("@cf/qwen/qwen3-30b-a3b-fp8", "off", {"chat_template_kwargs": {"enable_thinking": False, "thinking": False}}, "/no_think"),
    ("@cf/openai/gpt-oss-120b", "off", {"reasoning_effort": "low"}, None),
    ("@cf/openai/gpt-oss-120b", "on", {}, None),
    ("@cf/mistralai/mistral-small-3.1-24b-instruct", "off", {}, None),
    ("@cf/google/gemma-4-26b-a4b-it", "default", {}, None),
])
def test_thinking_option_uses_each_model_switch(model, mode, body, suffix):
    with patch("httpx.Client.post", return_value=_llm_reply("done")) as post:
        res = harness_agent(Ctx(), [], prompt="hi", thinking=mode, **{**HARNESS_KW, "model": model})
    assert res.ok
    sent = post.call_args_list[-1].kwargs["json"]
    for key in ("chat_template_kwargs", "reasoning_effort"):
        assert sent.get(key) == body.get(key)
    system = sent["messages"][0]["content"]
    if suffix:
        assert system.endswith(suffix)
    else:
        assert "/no_think" not in system and "/think" not in system


# -- Retries and cost control ---------------------------------------------------


def _with_usage(resp: MagicMock, prompt: int, completion: int, neurons: float | None = None) -> MagicMock:
    body = resp.json.return_value
    body["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if neurons is not None:
        body["usage"]["neurons"] = neurons
    return resp


def _status(code: int, retry_after: str = "") -> MagicMock:
    resp = MagicMock(status_code=code, text=f"error {code}")
    resp.headers = {"retry-after": retry_after} if retry_after else {}
    return resp


def test_post_with_retry_retries_transient_errors_only():
    waits: list[float] = []
    client = MagicMock()
    client.post.side_effect = [_status(429, "3"), httpx.ConnectError("reset"), _status(200)]
    resp = runtime._post_with_retry(client, "u", headers={}, json={}, retries=2, sleep=waits.append)
    assert resp.status_code == 200 and client.post.call_count == 3
    assert waits[0] == 3.0  # Retry-After is honoured
    assert 1.2 <= waits[1] <= 2.0  # then exponential backoff with jitter

    client.post.side_effect = [_status(400)]
    assert runtime._post_with_retry(client, "u", headers={}, json={}, retries=2, sleep=waits.append).status_code == 400

    client.post.side_effect = [_status(503), _status(503), _status(503)]
    assert runtime._post_with_retry(client, "u", headers={}, json={}, retries=2, sleep=lambda s: None).status_code == 503


def test_post_with_retry_never_waits_past_the_deadline():
    client = MagicMock()
    client.post.side_effect = [_status(429, "50"), _status(200)]
    resp = runtime._post_with_retry(client, "u", headers={}, json={}, retries=2, deadline=time.monotonic() + 5,
                                    sleep=lambda s: pytest.fail("must not sleep"))
    assert resp.status_code == 429 and client.post.call_count == 1


def test_harness_retries_a_rate_limited_model_and_reports_usage(monkeypatch):
    monkeypatch.setattr(runtime.time, "sleep", lambda s: None)
    replies = [_status(429), _with_usage(_llm_reply("done"), 100, 20, neurons=1.5)]
    with patch("httpx.Client.post", side_effect=replies) as post:
        res = harness_agent(Ctx(), [], prompt="go", **HARNESS_KW)
    assert res.ok and post.call_count == 2
    assert res.data["usage"] == {"requests": 1, "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "neurons": 1.5}
    assert runtime._usage_today("invoices")["total_tokens"] == 120  # recorded for the flow


def test_max_tokens_is_sent_and_token_budget_stops_the_loop():
    calls = [_with_usage(_llm_reply(tool_calls=[_call("memory_get", {"key": "x"}, f"c{i}")]), 900, 200) for i in range(2)]
    calls.append(_llm_reply("summary"))
    with patch("httpx.Client.post", side_effect=calls) as post:
        res = harness_agent(Ctx(), [tool_memory(Ctx(), [])], prompt="go", max_turns=10, max_tokens=256, token_budget=2000, **HARNESS_KW)
    assert res.ok
    assert res.data["stop_reason"] == "token_budget"
    assert res.data["turns_used"] == 2  # 1100 + 1100 tokens >= 2000 after the second reply
    assert res.data["response"] == "summary"
    assert all(c.kwargs["json"].get("max_tokens") == 256 for c in post.call_args_list)


def test_daily_token_budget_blocks_the_run_once_spent():
    runtime._usage_record("invoices", {"requests": 1, "total_tokens": 5000})
    with patch("httpx.Client.post") as post:
        res = harness_agent(Ctx(), [], prompt="go", daily_token_budget=5000, **HARNESS_KW)
    assert not res.ok and "Daily token budget" in res.error and post.call_count == 0


def test_usage_is_recorded_even_when_the_model_fails_later():
    replies = [_with_usage(_llm_reply(tool_calls=[_call("memory_get", {"key": "x"})]), 50, 10), _status(400)]
    with patch("httpx.Client.post", side_effect=replies):
        res = harness_agent(Ctx(), [tool_memory(Ctx(), [])], prompt="go", **HARNESS_KW)
    assert not res.ok and "400" in res.error
    assert runtime._usage_today("invoices")["total_tokens"] == 60


# -- Structured output -----------------------------------------------------------

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "important": {"type": "array", "items": {"type": "string"}},
        "count": {"type": "integer", "minimum": 0},
        "mood": {"enum": ["calm", "busy"]},
    },
    "required": ["important", "count"],
    "additionalProperties": False,
})


def test_schema_errors_cover_the_common_keywords():
    schema = json.loads(SCHEMA)
    assert runtime._schema_errors({"important": ["a"], "count": 2, "mood": "calm"}, schema) == []
    errs = runtime._schema_errors({"important": [1], "count": -1, "mood": "x", "extra": 1}, schema)
    text = " ".join(errs)
    for part in ("$.important[0]", "$.count", "$.mood", "extra"):
        assert part in text
    assert runtime._schema_errors(True, {"type": "integer"})  # bool is not an integer
    assert runtime._schema_errors({"a": 1}, {"type": "object"}) == []
    assert runtime._schema_errors("x", {"anyOf": [{"type": "integer"}, {"type": "string"}]}) == []
    assert runtime._schema_errors(None, {"anyOf": [{"type": "integer"}, {"type": "string"}]})


def test_submit_result_is_validated_and_ends_the_run():
    bad = _llm_reply(tool_calls=[_call("submit_result", {"important": "no", "count": 1}, "c1")])
    good = _llm_reply(tool_calls=[_call("submit_result", {"important": ["bank"], "count": 1}, "c2")])
    with patch("httpx.Client.post", side_effect=[bad, good]) as post:
        res = harness_agent(Ctx(), [], prompt="go", output_schema=SCHEMA, output_format="text", **HARNESS_KW)
    assert res.ok and res.data == {"important": ["bank"], "count": 1}
    sent = post.call_args_list[1].kwargs["json"]
    # the model saw why (the message list is shared, so look through it)
    assert any("does not match the schema" in str(m.get("content")) for m in sent["messages"] if m.get("role") == "tool")
    spec = next(t for t in sent["tools"] if t["function"]["name"] == "submit_result")
    assert spec["function"]["parameters"]["required"] == ["important", "count"]
    assert "submit_result" in sent["messages"][0]["content"]


def test_a_text_reply_with_valid_json_is_accepted_and_invalid_text_is_nudged():
    fenced = _llm_reply('Here it is:\n```json\n{"important": [], "count": 0}\n```')
    with patch("httpx.Client.post", side_effect=[fenced]):
        res = harness_agent(Ctx(), [], prompt="go", output_schema=SCHEMA, **HARNESS_KW)
    assert res.ok and res.data["result"] == {"important": [], "count": 0}

    prose = [_llm_reply("I could not find anything.") for _ in range(3)]
    with patch("httpx.Client.post", side_effect=prose) as post:
        res = harness_agent(Ctx(), [], prompt="go", output_schema=SCHEMA, **HARNESS_KW)
    assert not res.ok and "output_schema" in res.error and post.call_count == 3  # two nudges, then it gives up


def test_non_object_schemas_are_wrapped_and_unwrapped():
    reply = _llm_reply(tool_calls=[_call("submit_result", {"result": ["a", "b"]})])
    with patch("httpx.Client.post", side_effect=[reply]) as post:
        res = harness_agent(Ctx(), [], prompt="go", output_schema='{"type": "array", "items": {"type": "string"}}',
                            output_format="text", **HARNESS_KW)
    assert res.ok and res.data == ["a", "b"]
    spec = post.call_args_list[0].kwargs["json"]["tools"][0]["function"]["parameters"]
    assert spec["required"] == ["result"]


def test_invalid_output_schema_fails_early():
    with patch("httpx.Client.post") as post:
        res = harness_agent(Ctx(), [], prompt="go", output_schema="{not json", **HARNESS_KW)
    assert not res.ok and "output_schema" in res.error and post.call_count == 0
