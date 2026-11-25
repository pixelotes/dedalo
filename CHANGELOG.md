# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Fixed

- Context compaction no longer separates parallel tool calls from their results, which strict servers rejected.

### Added

- Built-in MCP server at `/mcp` (stateless Streamable HTTP, token required in `DEDALO_MCP_TOKEN`): flows that start with the new `input.mcp` node are served as tools with a name, a description and typed arguments, and return the data of the new `output.return` node, optionally checked against a JSON Schema. `tool.flow` reads the same contract, so the harness gets typed arguments too. See `doc/mcp_server.md`.
- `transform.harness` node: generic agent harness built on the `transform.coder` ReAct loop, with tools from connected `tool.*` nodes, a turn and time budget, `skip_if_response` and human approval of risky calls over Telegram (Approve/Deny buttons). See `doc/agent_harness.md`.
- Tool nodes: `tool.mail` (IMAP/SMTP), `tool.http`, `tool.mcp` (Streamable HTTP client, no SDK), `tool.flow` and `tool.memory`, each with an approval policy (`never`, `write`, `always`).
- `input.state` and `output.state` nodes: key/value state between runs in `data/state/memory/`, shared with `tool.memory`.
- `avisar-facturas` template: daily invoice alert agent (mail + memory + Telegram).
- `transform.harness`: `output_schema` for structured results through a `submit_result` tool, validated with a built-in JSON Schema subset and sent back to the model when invalid.
- `transform.harness`: retries with backoff for rate limits, 5xx and dropped connections (`retries`), token usage in the output and in `data/state/usage.sqlite`, and `max_tokens`, `token_budget` and `daily_token_budget` limits.
- `input.imap`: `only_new` returns only the mail that arrived since the previous run (tracked by IMAP UID in the state store), and every email has a `bulk` flag from its mailing-list headers.
- `transform.classify` node and `tool.classifier`: a fast embeddings + nearest-examples classifier that decides the easy items and sends the rest to an agent, which teaches it back with `learn_label`.

- `input.subflows` node: runs other flows in parallel with a timeout and returns each result (status, data, error); sub-flows skip their `output.*` nodes by default, are limited to 3 levels with cycle detection, and are linked to the parent run in the history. Not available in the standalone export. See `doc/subflows.md`.
- `input.calendar` node: reads iCalendar (`.ics`) URLs, such as Google Calendar's secret address, with recurring event expansion; the URL is meant to be stored as a secret.
- External secret references (`op://`, `vault://`, `env://`, `file://`) with an in-memory TTL cache, `GET /api/secrets/info` and `POST /api/secrets/test`; `OP_*`, `VAULT_*`, `BAO_*` and `BWS_*` are hidden from shell commands (`DEDALO_ENV_ALLOW` re-exposes some).

- Internationalization (English/Spanish) of the UI, node catalog, templates and messages.
- Optional webhook token (`DEDALO_WEBHOOK_TOKEN`), body size limit (`DEDALO_MAX_BODY_MB`) and security headers with CSP.
- Encryption of secrets at rest, environment filtering and optional command isolation with bubblewrap.
- `Host` (`DEDALO_ALLOWED_HOSTS`) and `Origin` validation; CORS is removed.
- Elastic License 2.0, `NOTICE`, security policy and contribution guide.

### Changed

- `transform.coder` now runs on the shared `_react_loop`; its behaviour and output are unchanged.
- BREAKING: the default language is now English (UI, messages, docs). Set DEDALO_LANG=es to keep Spanish messages in scheduled runs and webhooks; the UI language is chosen in Settings.
- Webhooks run in a thread and no longer block the server.
- `/api/history/purge` requires `days` between 1 and 3650.
- `list_flows` uses a cache keyed by file date and size, 500 errors on save, import and export no longer expose internal details, and startup is ~100 ms faster (lazy imports).
- The scheduler runs flows in parallel (`DEDALO_MAX_CONCURRENT_FLOWS`, 2 by default).
- The history SQLite database is initialized only once per process, and that initialization is now thread-safe (concurrent first runs no longer fail with `database is locked`).
- The CI TruffleHog and Trivy actions are pinned by SHA.
