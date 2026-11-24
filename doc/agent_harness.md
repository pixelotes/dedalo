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

## Fast classifier in front of the agent

An agent reasons well but is slow and costs a model call per turn. For repetitive sorting (mail, tickets, alerts) put a cheap classifier in front of it and let the agent handle only what the classifier is not sure about:

```text
input.imap ──▶ transform.classify ──▶ transform.harness ──▶ output.telegram
                                         ▲
                       tool.classifier ──┘  (learn_label)
```

- `transform.classify` turns each item into an embedding (any OpenAI-compatible `/embeddings`: Workers AI `@cf/baai/bge-m3`, Ollama, OpenAI...) and lets the `k` most similar labelled examples vote. It returns `sure` (with `_label` and `_confidence`) and `unsure`.
- The agent gets both lists as context, decides the `unsure` ones and calls `learn_label(_id, label)` from `tool.classifier`. Each label becomes an example, so the next run sends fewer items to the agent. It can also correct a `sure` item.
- Until the store has `min_examples` examples everything is `unsure` (cold start). Examples live in a SQLite file, `data/state/classifier/<store>.sqlite`, as float16 vectors (2 KB each for 1024 dimensions), capped by `max_examples`. The classifier streams them row by row and keeps only the best `k` per item, so memory does not grow with the store: with 2,000 examples a run peaks at about 4 MB.

Measured on a real mailbox with Gemma as teacher: after 50 labelled emails the classifier decided 20 of the next 30 on its own in 1.9 s, 18 of them right; both mistakes had a confidence below 0.85, so `min_agreement: 0.85` sends them to the agent instead.

## Reasoning models

Models such as Gemma, Qwen or DeepSeek reason before answering. In a tool loop that can mean thousands of hidden tokens per turn: the mail triage agent went from about 220 s to 60 s on Workers AI with `thinking` set to `off`, with the same results. `on` forces it and `default` sends nothing. Each family has its own switch: `chat_template_kwargs.enable_thinking` for Gemma, GLM and DeepSeek, that plus `/no_think` in the system prompt for Qwen, `reasoning_effort: low` for gpt-oss (it cannot stop reasoning), and nothing for Mistral, which rejects template parameters. Raise `timeout` (seconds per model request) if you keep reasoning on.

## Structured output

Set `output_schema` to a JSON Schema and the agent must finish by calling `submit_result` with data that matches it, instead of writing free text. The next nodes then get fields, not prose to parse:

```json
{"type": "object",
 "properties": {
   "important": {"type": "array", "items": {"type": "object", "properties": {"from": {"type": "string"}, "subject": {"type": "string"}, "why": {"type": "string"}}, "required": ["from", "subject", "why"]}},
   "subscriptions": {"type": "integer"},
   "spam": {"type": "integer"}},
 "required": ["important", "subscriptions", "spam"]}
```

- It works with any model that can call tools: the schema becomes the parameters of `submit_result`, so it does not depend on the server supporting `response_format`.
- The harness validates the result itself (types, `required`, `properties`, `additionalProperties`, `items`, `enum`, `const`, lengths, ranges, `anyOf`/`oneOf`), with no extra library. An invalid result goes back to the model with the errors so it can fix it.
- A plain JSON answer (even inside a ``` fence) is accepted when it matches. Otherwise the model is reminded twice, and then the node fails, so the failure branch can tell you.
- With `output_format: text` the node outputs the result object; with `full`, it is under `result`. A schema that is not an object (a list, for example) is wrapped as `{"result": ...}` for the tool call and unwrapped again.

## Retries and cost control

- **Retries** (`retries`, default 2): model and embedding requests are repeated after a 429, a 5xx, a timeout or a dropped connection, waiting 1, 2, 4... seconds with jitter or what `Retry-After` asks for, and never past the run's time budget. Other 4xx errors are not retried. Tool calls are not retried automatically: a tool that sends or changes something must not run twice.
- **Usage**: the `full` output has `usage` (requests, prompt, completion and total tokens, and Workers AI neurons when reported). Every run adds it to `data/state/usage.sqlite`, one row per flow and day, also when the run fails half way.
- **Limits**:
  - `max_tokens` caps each reply (it also stops runaway reasoning);
  - `token_budget` caps the run: when it is used up the agent summarizes and stops, with `stop_reason: token_budget`;
  - `daily_token_budget` caps the flow per day: once spent, the node fails with a clear error (the failure branch can notify you) until the next day.

## Limits

- The model must support OpenAI-style tool calling.
- `tool.flow` is not available in the standalone export, like `input.subflows`.
- A pending approval holds a worker thread; for waits of hours, split the flow instead.
- `transform.harness` has no live preview in the editor, because it would run real tools on every change.
