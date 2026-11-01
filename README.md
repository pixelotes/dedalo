# Dédalo

Visual workflow orchestrator designed to run continuously on resource-constrained hardware, aiming to run on a Raspberry Pi 3B using between 25 and 35 MB of RAM.

Unlike alternatives such as n8n or Node-RED, Dédalo does not require Node.js, does not use frontend bundlers and does not keep a reactive evaluation engine with heavy in-memory queues. Each visual graph is compiled directly to a plain Python script (`def run(ctx):`) based on ternary operators and skip sentinels (`SKIPPED`).

The system includes a broad set of deterministic transformation nodes, aiming to offer maximum flexibility in data processing and to avoid depending on large language models (LLMs) unless strictly necessary.

---

## Technical features

- **Low footprint**: 14 to 18 MB of RAM in pure daemon mode (cron scheduler without a web server), 25 to 35 MB with the API server and visual editor.
- **Direct compilation to Python**: Visual flows are translated into deterministic Python code in `data/flows/<name>.py`.
- **Lightweight, dependency-free frontend**: Web interface in vanilla JavaScript and native SVG, with an infinite canvas and no heavy component libraries.
- **Built-in cron evaluator**: Native parser for cron expressions and intervals (`@every 30m`, `@hourly`) with no external dependencies.
- **Web compatibility without a headless browser**: Dual HTTP client with `curl_cffi` (real browser TLS fingerprint, for sites that reject generic HTTP clients) and a fallback to `httpx`.
- **Step-by-step debugging and live preview**: Node-by-node execution control on the canvas, a modal viewer for complete untruncated inputs and outputs, and real-time recalculation of transformations (regex, CSS selectors, templates) as parameters are edited.
- **Local secret management**: Isolated credential storage in files with restricted permissions (`0600`) or through environment variables.
- **Internationalization**: The interface and messages are in English by default; Spanish is selectable in Settings (UI) and via `DEDALO_LANG=es` for scheduled runs and webhooks.

---

## Main nodes

The catalog includes more than 70 modular nodes (72 in total). Among the most commonly used are:

### Input nodes
- `input.git`: Reads local or remote Git repositories (commit history, diff of changes between revisions with path filtering, lightweight shallow clone or working tree status).
- `input.rss`: RSS and Atom feed reader with persistent memory of already processed items.
- `input.http_call` / `input.web_read`: HTTP requests with browser fingerprint impersonation and web content extraction.
- `input.imap`: Email reader over IMAP with TLS support and automatic sanitization of injections and signatures.
- `input.web_search`: Internet search through DuckDuckGo (no API key), Tavily or Exa.
- `input.webhook`: Exposes the flow as an HTTP endpoint to receive external events (no built-in authentication: see [Security](#security)).
- `input.mqtt`: Receives messages from local MQTT brokers (Mosquitto, Home Assistant), implemented on native sockets.
- `input.weather`: Real-time weather queries to Open-Meteo with automatic geocoding.
- `input.pi_health`: Measures temperature, memory, thermal throttling and load on Raspberry Pi and Linux systems.
- `input.ssl_cert` / `input.dns_check` / `input.ping`: Checks TLS certificate expiry, DNS resolution and network latency.

### Transform nodes
- `transform.coder`: Iterative programming micro-harness with a multi-turn ReAct loop and structured tools (`read`, `bash`, `edit`, `write`, `grep`, `find`, `ls`). Auto-detects the inference server's context window and compacts earlier history with Luhn+ when it reaches 90% saturation.
- `transform.html_extract`: Extracts fields and records from HTML documents using CSS selectors.
- `transform.regex`: Regular expression evaluation with match modes, group extraction and substitution.
- `transform.filter_items` / `transform.array_ops`: Conditional filtering, sorting, limits and projection over data lists.
- `transform.diff`: Novelty detection using persistent storage of hash signatures across successive runs.
- `transform.fuzzy_dedup`: Deduplication of texts and records based on string similarity, without resorting to AI.
- `transform.clean_email`: Email sanitization that neutralizes text hidden by CSS (`display:none`, `font-size:0`, `color:transparent`), HTML comments and invisible Unicode characters (zero-width), and trims common signatures and legal disclaimers.
- `transform.datetime`: Date and time calculations, relative offsets, time zone conversion and natural-language formatting.
- `transform.summarize`: Algorithmic extractive summarization using Luhn, Luhn+ and TextRank.
- `transform.script`: Execution of custom Python logic with variable isolation.
- `transform.agent`: Optional calls to OpenAI-compatible API endpoints (local models such as Ollama or remote services), with JSON mode support and field enrichment (`as_field`) over lists.

### Output nodes
- `output.git`: Creates commits and optionally pushes to remote repositories, with configurable author and message.
- `output.telegram` / `output.discord`: Sends formatted messages and alerts to channels and private chats.
- `output.sqlite`: Direct insertion and update of records in local SQLite databases.
- `output.file`: Writes and appends files on disk (Markdown, JSON, plain text).
- `output.mqtt`: Publishes telemetry or commands to MQTT brokers.
- `output.healthcheck`: Sends monitoring pings to services such as Healthchecks.io or Uptime Kuma.

---

## Representative use cases

1. **Selective monitoring of news sources or official bulletins**:
   Download RSS feeds or web articles, discard duplicates with `transform.fuzzy_dedup` or `transform.diff`, filter relevant topics and send a summary to a Telegram channel.

2. **Daily email briefing without injection risk**:
   Read unread emails with `input.imap`, strip hidden text and signatures with `transform.clean_email`, compile a prioritized executive summary with `transform.agent` and send it to your phone first thing in the morning.

3. **Infrastructure and hardware monitoring on Raspberry Pi**:
   Continuous monitoring of CPU temperature, free disk percentage and TLS certificate validity. A `transform.threshold` node stops the flow if the values are normal and fires critical alerts only when the limits are exceeded.

4. **Structured processing and archiving of invoices or notifications**:
   Receive events via webhook or IMAP, extract key data with regular expressions or an agent in JSON mode, and automatically insert them into a local SQLite database for later querying.

5. **Continuous commit auditing and Git security**:
   Automatically extract the latest change patch with `input.git` (diff mode with a maximum character limit to protect the context), analyze vulnerabilities and credentials with `transform.agent` and send preventive alerts to Telegram on critical findings.

6. **Assisted closed-loop code fixing and refactoring**:
   Trigger `transform.coder` to resolve an issue or adjust a source file on local disk. The agent inspects files with `read`/`grep`, tests changes with `bash` and edits atomically with `edit`, compacting memory via Luhn+ if the context saturates, before recording the result.

---

## Requirements and installation

Python 3.11 or later is required on a Unix-like operating system (Linux, Raspberry Pi OS, macOS).

```bash
git clone https://github.com/pixelotes/dedalo.git
cd dedalo

# Install the environment and dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start the application with the web interface
./run.sh
```

The application will be available at `http://localhost:8080`.

> Dédalo has no authentication. Before exposing it beyond `localhost`, read the [Security](#security) section.

### Run modes

- **Full (Web + API + Scheduler)**:
  ```bash
  ./run.sh
  ```

- **Headless (REST API and Webhooks, without serving static UI files)**:
  ```bash
  ./run.sh --headless
  ```

- **Pure daemon (cron scheduler only, minimum RAM usage)**:
  ```bash
  ./run.sh --daemon
  ```

- **Stopping**:
  ```bash
  ./stop.sh
  ```

---

## Docker deployment

The project includes a multi-stage configuration with permission restrictions (non-root user, all Linux capabilities dropped and `no-new-privileges`):

```bash
docker compose up -d
docker compose logs -f
```

---

## systemd service for Raspberry Pi

To ensure the service starts automatically after a reboot and to limit memory usage:

1. Create the file `/etc/systemd/system/dedalo.service`:

```ini
[Unit]
Description=Dedalo Automation Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/dedalo
Environment="MALLOC_TRIM_THRESHOLD_=65536"
Environment="PYTHONOPTIMIZE=1"
EnvironmentFile=-/home/pi/dedalo/.env
ExecStart=/home/pi/dedalo/.venv/bin/uvicorn dedalo.app:app --host 127.0.0.1 --port 8080 --no-access-log --limit-concurrency 15 --timeout-keep-alive 5
Restart=always
RestartSec=5

# Memory limits via cgroups
MemoryHigh=65M
MemoryMax=85M

[Install]
WantedBy=multi-user.target
```

2. Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dedalo
```

---

## Security

Dédalo runs code: flows are compiled to Python, and nodes such as `transform.script`, `transform.coder` (with `bash`) and `output.git` act on the system with the permissions of the user running it. Treat it as an administration console.

- **No authentication**: the web interface and the REST API (including `/api/run`, `/api/flow` and `/api/secrets`) do not ask for credentials. Anyone who can reach the port can run code and read or write secrets.
- **Local listening**: use `DEDALO_HOST=127.0.0.1` and access it through an SSH tunnel, Tailscale or a reverse proxy with authentication (for example Caddy or Nginx with OIDC or basic auth). Do not publish it directly on the internet or open it to the whole local network.
- **No CORS and Host checking**: the API does not send CORS headers (the interface is served from the same origin). Requests whose `Host` is not in `DEDALO_ALLOWED_HOSTS` (by default `localhost,127.0.0.1,[::1]`; supports `*.domain`) are rejected, as are state-changing requests with an `Origin` different from the `Host`. If you access it through a domain, a Tailscale IP or a reverse proxy, add it to that variable; the proxy must preserve the `Host` header. Calls without `Origin` (curl, server webhooks) are not affected, but they must still use an allowed `Host`.
- **Docker**: publish the port on localhost only (`"127.0.0.1:8080:8080"`). Inside the container it must keep listening on `0.0.0.0`.
- **Webhooks**: `/api/webhook/<flow>` does not validate the origin by default. Set `DEDALO_WEBHOOK_TOKEN` to require `Authorization: Bearer <token>`, the `X-Dedalo-Token` header or `?token=` (the header is preferable: the URL ends up in proxy logs); the token does not reach the flow or the history. If you need to receive events from the internet, expose only that route through the proxy. Flows run in a thread, so a slow one does not block the interface or `/health`.
- **Scheduler**: cron flows run in parallel up to `DEDALO_MAX_CONCURRENT_FLOWS` (2 by default; `1` restores sequential behavior) and a flow never overlaps with itself.
- **Limits and headers**: bodies larger than `DEDALO_MAX_BODY_MB` (25 by default) are rejected with 413. All responses carry `Content-Security-Policy`, `X-Frame-Options: DENY`, `nosniff` and `Referrer-Policy`, so the interface cannot be embedded in another website.
- **Shell commands** (`input.command`, `output.command` and the `bash` of `transform.coder`): subprocesses do not inherit `DEDALO_SECRET_*`, `DEDALO_MASTER_KEY*`, `DEDALO_LLM_KEY` or `DEDALO_TELEGRAM_*` (add more patterns with `DEDALO_ENV_DENY`). They can also be isolated with [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`, no root): they only see `/usr`, `/bin`, `/lib*` and `/etc` read-only, an empty `/tmp` and their working directory (`cwd`); `data/` is hidden. `DEDALO_SANDBOX=off|auto|on`: the default is `off` in command nodes (it does not change the behavior of your flows) and `auto` in `transform.coder`; `auto` uses `bwrap` if it works and otherwise runs without isolation, `on` fails if it is not available. Each node has a `sandbox` option (it can request `on`, but cannot disable a global `on`). Extras: `DEDALO_SANDBOX_RO` and `DEDALO_SANDBOX_RW` (paths separated by `:`) and `DEDALO_SANDBOX_NET=deny` to cut off the network. With the sandbox active, a command without `cwd` starts in an empty `/tmp`, and `systemctl` and similar tools cannot reach the host. The Docker image includes `bwrap` (`--build-arg WITH_BWRAP=0` to omit it), but Docker's default profile blocks user namespaces: to use it inside the container, see `docker-compose.yml`. Outside Docker (Raspberry Pi OS, Debian): `apt install bubblewrap`.
- **`transform.script`**: the isolation is variable-level, not a sandbox. Do not run third-party imported flows without reviewing them.
- **Secrets**: they are stored encrypted (Fernet) in `data/secrets/` with `0600` permissions; so is the `llm_key` in `data/config.json`. The master key comes from `DEDALO_MASTER_KEY` (or `DEDALO_MASTER_KEY_FILE`); if you define neither, `data/master.key` is generated, which protects against partial copies but not against someone who reads all of `data/`. For real protection, define the key outside `data/` (environment variable, Docker secret). Generate it with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and store it separately: without it the secrets cannot be recovered. Plaintext secrets from earlier versions are still read and are encrypted automatically on startup. `DEDALO_SECRET_<NAME>` variables keep working and are not encrypted (they live in the environment).

To report a vulnerability, see [SECURITY.md](SECURITY.md).

---

## Documentation

- **External secret stores**: a secret can hold a reference instead of a value — `op://Vault/Item/field` (1Password via the `op` CLI and a service account), `vault://mount/path#field` (HashiCorp Vault / OpenBao, KV v2), `env://VAR` or `file:///run/secrets/name` — so nothing sensitive is stored in `data/`. Flows keep using the secret by name. Values are cached in memory only (`DEDALO_SECRETS_CACHE_TTL`, 300 s by default) and the bootstrap credentials (`OP_*`, `VAULT_*`, `BAO_*`) are hidden from shell commands (re-expose some with `DEDALO_ENV_ALLOW`). Check a reference with `POST /api/secrets/test?name=<secret>`. See [doc/external_secrets_evaluation.md](doc/external_secrets_evaluation.md).
- [Documentation index](doc/README.md)
- [Project status](doc/PROJECT_STATUS.md)
- [Philosophy and architecture](doc/philosophy_and_architecture.md)
- [Node catalog](doc/node_catalog.md)
- [Agents and LLMs](doc/agents_and_llms.md)
- [Standalone export](doc/standalone_export.md)
- [Use cases and workflows](doc/use_cases_and_workflows.md)
- [Dependencies and performance](doc/dependencies_and_performance.md)
- [External secrets evaluation](doc/external_secrets_evaluation.md): Evaluation of external secret stores such as 1Password

---

## Automated tests and quality

To run the full unit test suite and the linter:

```bash
pytest -v
ruff check .
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [CHANGELOG](CHANGELOG.md).

---

## License

[Elastic License 2.0](LICENSE). See also [NOTICE](NOTICE).
