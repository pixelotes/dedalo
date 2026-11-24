# Agent Harness and Tool Nodes

`transform.coder` is an agent with a fixed set of file and shell tools. `transform.harness` uses the same ReAct loop but has **no tools of its own**: it gets them from the `tool.*` nodes connected to it. An agent in Dédalo is just that:

```text
[schedule]            ┌──────────────┐
 tool.mail   ───────▶ │              │
 tool.memory ───────▶ │  transform.  │ ──▶ output.telegram
 tool.mcp    ───────▶ │  harness     │
 input.* (context) ─▶ │              │
                      └──────────────┘
```

The harness calls the model, runs the tools it asks for, sends the results back and repeats until the model gives a final answer, the turn limit (`max_turns`, up to 50) is reached or the time budget (`max_seconds`) runs out. It keeps the coder's context compaction and loop detection.

## How tool nodes work

A tool node produces no data. It emits a **descriptor** (the tool kind and its configuration, with secret *names* only) through an ordinary `ok` edge. The harness turns every descriptor into callable tools when it starts. As a result:

- Tool nodes can only connect to a `transform.harness`, and they take no inputs.
- Tool edges do not count towards `fan_in` and never block the harness: it runs when its data inputs (if any) are ok.
- Secret values are read when a tool runs. They never reach the model, the run history or exported flows.
- Inputs that are not tools (an `input.rss`, an `input.webhook` payload...) are appended to the prompt as context.

| Node | Tools it adds | Risk |
| --- | --- | --- |
| `tool.mail` | `mail_list`, `mail_read`, `mail_flag`, `mail_move`, `mail_send` (pick them in `actions`; default `list,read`) | list/read: read · flag/move: write · send: dangerous |
| `tool.http` | One tool that calls an API endpoint. `{placeholders}` in the URL become required arguments | GET reads, other methods write (or set `risk`) |
| `tool.mcp` | Every tool of an MCP server over Streamable HTTP (`allowed_tools` filters them) | from the server's annotations: `readOnlyHint` reads, `destructiveHint` is dangerous, otherwise write |
| `tool.flow` | Runs a saved flow and returns its result | read with `skip_outputs` (default), write without it |
| `tool.memory` | `memory_get`, `memory_set`: key/value memory that survives between runs | — (never asks by default) |

Two mailboxes or two MCP servers on one harness need different `prefix` values; duplicate tool names stop the run with an error.

The mail tools clean email bodies with the same anti prompt-injection sanitizer as `input.imap`, and email ids are IMAP UIDs, stable between calls.

## Human approval

Every tool node has an `approval` policy:

- `never`: calls run without asking.
- `write` (default): calls that change or send something (risk `write` or `dangerous`) need a human; reads run freely.
- `always`: every call needs a human.

The harness decides how to ask with `approval_channel`:

- `none` (default): calls that need approval are **denied**. The model is told so and must carry on without them.
- `telegram`: the harness sends a message with the tool, its arguments and **Approve / Deny** buttons to `approval_chat_id`, and waits up to `approval_timeout` seconds. No answer means denied. It long-polls `getUpdates`, so it needs no public URL. Use a bot that no other program polls or receives webhooks for, and only buttons pressed in the approval chat count.
- `auto_approve`: approves everything. For testing only.

While it waits for an answer the run is paused (the flow keeps its worker thread).

## State shared between runs

`tool.memory`, `input.state` and `output.state` use the same store: one JSON file per scope in `data/state/memory/<scope>.json` (the scope defaults to the flow id, and `data/` is covered by your backups). A flow can preload what the agent remembers with `input.state`, or save the agent's result with `output.state`. Writes are atomic and size-limited (`max_bytes`).

## Ending without output

Set `skip_if_response` to a marker such as `NO_INVOICES` and tell the agent to answer exactly that when there is nothing to report. The harness then emits `SKIPPED` and the nodes after it (the Telegram message) do not run.

## Example: invoice alert

The `avisar-facturas` template connects `tool.mail` (read only) and `tool.memory` to a harness that, every day:

1. reads the ids already notified from memory,
2. lists the emails of the last day and reads only the ones it cannot classify from sender, subject and attachment names,
3. answers `NO_INVOICES` when there is nothing new, or stores the new ids and writes a short notice that `output.telegram` sends.

Give the flow a schedule such as `0 9 * * *`, create the `imap-password` (an app password) and `telegram-token` secrets, and set `chat_id` on the Telegram node.

## Using an MCP server such as beanie

```text
tool.mcp  url=http://127.0.0.1:8092/mcp  secret=beanie-token  allowed_tools=get_datetime,calculate,web_search
```

`secret` holds the server's bearer token. If the server sits behind Cloudflare Access, put the service token headers in a secret as JSON (`{"CF-Access-Client-Id": "...", "CF-Access-Client-Secret": "..."}`) and choose it in `headers_secret`.

## Limits

- The model must support OpenAI-style tool calling.
- `tool.flow` is not available in the standalone export, like `input.subflows`.
- A pending approval holds a worker thread; for waits of hours, split the flow instead.
- `transform.harness` has no live preview in the editor, because it would run real tools on every change.
