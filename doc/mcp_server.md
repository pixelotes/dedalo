# Flows as MCP Tools

A flow that starts with an `input.mcp` node is a **tool**: it has a name, a description and typed arguments, it runs like any other flow (with or without a model) and it returns the data of its `output.return` node.

```text
input.mcp ──▶ (any nodes: http, script, agent...) ──▶ output.return
 name, description,                                    schema (optional)
 parameters (JSON Schema)
```

The same flow is used in two ways, with one contract:

- **External agents** call it through Dédalo's MCP server at `/mcp`.
- **Dédalo's own harness** gets it from a `tool.flow` node, which reads the name, description and arguments from the flow's `input.mcp` node. No HTTP hop, and cycles between flows are still detected.

## The two nodes

`input.mcp` outputs the arguments of the call as a dict.

| Field | Meaning |
| --- | --- |
| `tool_name` | Name agents see (empty: the flow id). |
| `description` | What the tool does and when to use it. The model reads it. |
| `parameters` | JSON Schema (`type: object`) of the arguments. Calls that do not match are rejected before the flow starts. |
| `run_outputs` | Off (default): MCP calls skip the flow's other `output.*` nodes and the tool is announced as read-only. On: they run, and the tool is announced as one that changes things. |
| `timeout` | Seconds an MCP call waits (120). After that the caller gets an error; the flow cannot be killed and finishes in the background. |

`output.return` marks the result. With a `schema` the input must match it (JSON text written by a model, even inside a ``` fence, is parsed first); otherwise the node fails and the caller gets the error. Without the node, the result is the data of the flow's last nodes, as in sub-flows.

A flow can have one `input.mcp` and one `output.return`. Run by hand or on a schedule, `input.mcp` outputs `{}`.

## Enabling the server

The server is **off until you set a token**:

```bash
export DEDALO_MCP_TOKEN="$(openssl rand -hex 32)"
```

Clients send it as `Authorization: Bearer <token>` (or `X-Dedalo-Token`); it is never accepted in the URL. Every enabled flow with an `input.mcp` node is listed; disable a flow to stop serving it. Two flows with the same tool name: only the first (by flow id) is served and the other is logged.

The endpoint speaks Streamable HTTP without sessions or SSE: `initialize`, `ping`, `tools/list` and `tools/call`, answered as plain JSON. The `Host` must be in `DEDALO_ALLOWED_HOSTS`, as for the rest of the API.

```json
{"mcpServers": {"dedalo": {"type": "http", "url": "http://127.0.0.1:8080/mcp",
                           "headers": {"Authorization": "Bearer <token>"}}}}
```

From another Dédalo (or the same one), use `tool.mcp` with `url` and the token in `secret`.

## What a call returns

- The result as text (JSON when it is not a string), plus `structuredContent` when it is an object. When `output.return` has an object schema, the tool also announces it as `outputSchema`.
- `isError: true` with a readable message when the arguments do not match, the flow fails, the result does not match the schema or the timeout passes. The model can read it and correct the call.
- Every call is a run in the history with trigger `mcp`.

## Example: a deterministic tool

```text
input.mcp  tool_name=haiku  description="Returns a haiku about software."
   └▶ transform.script  code: return "Pasan los tests hoy,\nen producción algo arde:\nera la caché."
        └▶ output.return
```

`tools/call` with `{"name": "haiku", "arguments": {}}` returns the haiku. No model takes part.

## Limits and security

- A call holds a worker thread while the flow runs. Long flows belong on a schedule, not behind a tool.
- "Read-only" means only that the flow's `output.*` nodes are skipped. A flow whose transform nodes change things (a `transform.coder`, a harness with write tools, a script) still changes them: set `run_outputs` on so callers treat it as a write.
- The token gives access to every served flow. Expose `/mcp` only where you would expose the webhooks, and never the rest of the API.
- The standalone export runs the flow itself (arguments with `--input`, checked against `parameters`), but it does not include the MCP server.
