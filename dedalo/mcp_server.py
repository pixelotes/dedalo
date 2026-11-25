"""MCP server: saved flows that start with `input.mcp` are served as tools.

Stateless Streamable HTTP with plain JSON responses (no SSE, no sessions): `initialize`,
`ping`, `tools/list` and `tools/call`. The same contract (name, description, arguments schema)
is what `tool.flow` gives to an internal harness, so a flow is written once and used by both.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
from typing import Any

from dedalo import compiler, runtime, scheduler, store
from dedalo.i18n import tr

log = logging.getLogger("dedalo.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "dedalo", "version": "1"}


def contract(flow_id: str, flow_data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Tool contract of a flow, or None when it has no `input.mcp` node or its schema is invalid."""
    nodes = ((flow_data or {}).get("graph") or {}).get("nodes") or []
    start = next((n for n in nodes if n.get("type") == compiler.MCP_START_NODE), None)
    if start is None:
        return None
    cfg = start.get("config") or {}
    try:
        parameters = runtime._tool_schema(cfg.get("parameters"))
    except ValueError as exc:
        log.warning("Flow %s is not served as a tool: invalid parameters schema (%s)", flow_id, exc)
        return None
    output_schema = None
    ret = next((n for n in nodes if n.get("type") == compiler.RETURN_NODE), None)
    if ret is not None:
        try:
            candidate = json.loads(str((ret.get("config") or {}).get("schema") or "") or "null")
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and candidate.get("type") == "object":
            output_schema = candidate
    try:
        timeout = max(1.0, float(cfg.get("timeout") or 120.0))
    except (TypeError, ValueError):
        timeout = 120.0
    return {
        "flow": flow_id,
        "name": runtime._safe_tool_name(str(cfg.get("tool_name") or flow_id)),
        "description": str(cfg.get("description") or "").strip() or tr("Runs the Dédalo flow '{flow}' and returns its result.", flow=flow_id),
        "parameters": parameters,
        "output_schema": output_schema,
        "run_outputs": bool(cfg.get("run_outputs", False)),
        "timeout": timeout,
    }


def contracts() -> dict[str, dict[str, Any]]:
    """Contracts of every enabled flow that is served as a tool, by tool name."""
    found: dict[str, dict[str, Any]] = {}
    for entry in store.list_flows():
        if not entry.get("enabled", True):
            continue
        c = contract(entry["id"], store.load_flow(entry["id"]))
        if c is None:
            continue
        if c["name"] in found:
            log.warning("Flow %s is not served: tool name '%s' is already used by flow %s", c["flow"], c["name"], found[c["name"]]["flow"])
            continue
        found[c["name"]] = c
    return found


def list_tools() -> list[dict[str, Any]]:
    tools = []
    for c in contracts().values():
        tool: dict[str, Any] = {
            "name": c["name"],
            "description": c["description"],
            "inputSchema": c["parameters"],
            "annotations": {"readOnlyHint": not c["run_outputs"]},
        }
        if c["output_schema"]:
            tool["outputSchema"] = c["output_schema"]
        tools.append(tool)
    return tools


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, arguments: Any) -> dict[str, Any] | None:
    """Runs the flow behind a tool and returns an MCP tool result. None: there is no such tool."""
    c = contracts().get(name)
    if c is None:
        return None
    args = arguments if arguments is not None else {}
    problems = runtime._schema_errors(args, c["parameters"])
    if problems:
        return _text_result(tr("Invalid tool arguments: {problems}", problems="; ".join(problems)), is_error=True)

    box: dict[str, Any] = {}

    def work() -> None:
        box["out"] = scheduler.run_subflow_sync(
            c["flow"], payload=args, skip_outputs=not c["run_outputs"], trigger_type="mcp",
        )

    # The flow cannot be killed: past the timeout the caller gets an error and the thread finishes alone.
    thread = threading.Thread(target=contextvars.copy_context().run, args=(work,), name=f"dedalo-mcp-{c['flow']}", daemon=True)
    thread.start()
    thread.join(c["timeout"])
    if thread.is_alive():
        return _text_result(tr("The flow did not finish in {seconds} s; it keeps running in the background.", seconds=int(c["timeout"])), is_error=True)

    out = box.get("out") or {}
    if out.get("status") != "ok":
        reason = out.get("error") or out.get("errors") or tr("the flow produced no result")
        return _text_result(tr("Flow '{flow}' failed: {error}", flow=c["flow"], error=reason), is_error=True)
    data = out.get("data")
    result = _text_result(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str))
    if isinstance(data, dict):
        result["structuredContent"] = json.loads(json.dumps(data, ensure_ascii=False, default=str))
    return result


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message: Any) -> dict[str, Any] | None:
    """Answers one JSON-RPC message. Returns None for notifications (the HTTP layer replies 202)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    if "id" not in message:
        return None
    msg_id, method = message["id"], message["method"]
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if method == "initialize":
        wanted = str(params.get("protocolVersion") or "")
        result: Any = {
            "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": list_tools()}
    elif method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _error(msg_id, -32602, "Missing tool name")
        result = call_tool(name, params.get("arguments"))
        if result is None:
            return _error(msg_id, -32602, f"Unknown tool: {name}")
    else:
        return _error(msg_id, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}
