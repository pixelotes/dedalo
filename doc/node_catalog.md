# Complete Dédalo Node Catalog

Dédalo has **72 modular nodes** grouped into three families:
- **Inputs (`input.*`)**: 27 nodes to ingest data from APIs, services, queues, hardware, repositories or files.
- **Transforms (`transform.*`)**: 28 nodes to clean, filter, aggregate, compute, deduplicate, audit vulnerabilities (CVE/SBOM), scan for secrets (DLP), map data, add human approval gates, or enrich data with local algorithms or LLM agents.
- **Outputs (`output.*`)**: 17 nodes to persist evidence, open incidents, update wikis, send alerts to messaging platforms (including Microsoft Teams) or run actions.

---

## 1. Input Nodes (`input.*`)

### `input.telemetry`
- **Label**: Incident Telemetry.
- **Description**: Ingests raw events, traces or logs from incidents for SecOps and SRE processes. Accepts inline JSON or reading local log files.
- **Parameters**: `incident_id` (optional, auto-generated if empty), `resource` (cluster, pod, tenant), `telemetry_json`, `file_path`.
- **Output**: Dictionary with `incident_id`, `resource`, `timestamp` and the parsed telemetry data.

### `input.jira_issues`
- **Label**: Jira Issues (JQL).
- **Description**: Runs advanced searches against the Jira Cloud REST API v3 using the JQL query language.
- **Parameters**: `instance_url` (*required*), `email`, `secret` (API Token), `jql`, `fields` (comma-separated list of fields), `max_results`, `timeout`.
- **Output**: Dictionary with the list of enriched `issues` (key, summary, status, priority, ADF or text description, assignee), the total and the executed query.

### `input.confluence_pages`
- **Label**: Confluence Pages (CQL).
- **Description**: Retrieves pages and technical documentation from Confluence Cloud through space/title searches or advanced CQL queries.
- **Parameters**: `instance_url` (*required*), `email`, `secret`, `space_key`, `title`, `cql`, `limit`, `timeout`.
- **Output**: Dictionary with `pages` (list of pages with `id`, `title`, `space`, `version`, `content_storage_xhtml`, `content_text` and web links).

### `input.git`
- **Label**: Git Reader.
- **Description**: Reads information from local or remote Git repositories without installing heavy libraries (uses the `git` binary).
- **Actions**: `log` (commit history), `diff` (change patch with a character limit), `clone_or_pull` (shallow clone or update), `status` (working tree).
- **Parameters**: `action`, `repo_path`, `url`, `revision` (e.g. `HEAD~1..HEAD`), `limit`, `path_filter`, `max_chars`.

### `input.webhook`
- **Label**: Incoming Webhook.
- **Description**: Automatically exposes an HTTP endpoint on the Dédalo server (`/api/webhook/{flow_name}`) to receive external payloads (Grafana alerts, GitHub Actions, Defender, etc.).
- **Output**: Dictionary with the received JSON or the HTTP body text.

### `input.http_call`
- **Label**: Fetch from an API / Web.
- **Description**: Makes HTTP requests (GET, POST, PUT, DELETE) with flexible authentication and a dual engine: standard `httpx` or `curl_cffi` for TLS fingerprint impersonation and anti-bot evasion (Cloudflare).
- **Parameters**: `url` (*required*), `method`, `headers`, `body`, `secret`, `auth_header`, `engine`, `impersonate` (chrome, safari, firefox).

### `input.web_read`
- **Label**: Extract Web Page.
- **Description**: Downloads the full content of a web URL and extracts the readable text or sanitized HTML.

### `input.web_search`
- **Label**: Web Search.
- **Description**: Internet search through DuckDuckGo (free, no credentials), Tavily or Exa AI, with snippet and link filtering.

### `input.imap`
- **Label**: Mail Reader (IMAP).
- **Description**: Connects to mailboxes via IMAP/TLS, searches for unread messages or messages filtered by subject/sender, and extracts body, sender and attachments with injection sanitization.

### `input.rss`
- **Label**: RSS / Atom Feed.
- **Description**: Reads news and syndication feeds with persistent memory of already processed items (`only_new: True`).

### `input.command`
- **Label**: System Command (Shell).
- **Description**: Runs a command in the shell of the server or local container and captures the combined standard output and error.

### `input.csv_file` and `input.json_file`
- **Labels**: CSV File / JSON File.
- **Description**: Structured reading and parsing of tabular files or JSON documents from the local file system.

### `input.sqlite_query`
- **Label**: SQLite Query (SELECT).
- **Description**: Runs read queries against local SQLite databases and returns rows as dictionaries.

### `input.pi_health`, `input.cpu_load`, `input.disk_free`
- **Labels**: System Health Metrics.
- **Description**: Reads low-level hardware metrics directly from `/proc` and `/sys` on Linux and Raspberry Pi (CPU temperature in °C, thermal throttling, free memory and disk percentage) with no additional dependencies.

### `input.ssl_cert`, `input.dns_check`, `input.ping`
- **Labels**: Network and Security Monitors.
- **Description**: Checks the remaining days of TLS certificates, DNS resolution and latency, and ping connectivity (TCP or ICMP).

### `input.mqtt`
- **Label**: MQTT Subscribe.
- **Description**: Topic subscription on local or remote MQTT brokers, implemented directly on native Python sockets.

### `input.whois_rdap`
- **Label**: WHOIS / RDAP (Domain).
- **Description**: Queries registration data, registrar, creation/update dates, expiry, remaining days and nameservers of a domain through the standard RDAP REST JSON protocol.

### `input.dns_record`
- **Label**: DNS Records (DoH).
- **Description**: Resolves specific DNS records (`A`, `AAAA`, `MX`, `TXT` for SPF/DMARC, `CNAME`, `NS`, `SOA`, `CAA`) through DNS-over-HTTPS (Cloudflare or Google DoH) with structured JSON output.

---

## 2. Transform Nodes (`transform.*`)

### `transform.analyzer`
- **Label**: Analyzer Agent (SecOps / SRE).
- **Description**: Diagnostic agent specialized in cybersecurity, incident response and infrastructure. Diagnoses root causes from telemetry and evidence.
- **Key features**:
  - `sanitize_secrets: True`: Masks tokens, private keys, cloud credentials and passwords before calling the model.
  - `per_item: True`: Processes batches of items (e.g. a list of Jira tickets or Confluence pages) sequentially, enriching each record in `as_field`.
  - Supports structured JSON mode.

### `transform.planner`
- **Label**: Planner Agent (Remediation / Mitigation).
- **Description**: Takes the diagnosis issued by the analyzer and builds a structured remediation plan with immediate mitigation steps, a permanent fix and verification. Produces professional Markdown or strict JSON output.

### `transform.coder`
- **Label**: Coder Agent.
- **Description**: Iterative coding micro-harness with a multi-turn ReAct loop, sandboxing control and granular selection of tools and permissions in the inspector.
- **Available tools**:
  - `read`, `grep`, `find`, `ls`: Safe exploration and reading of the workspace.
  - `edit`, `write`: Atomic modification and creation of files on disk.
  - `bash`: Running terminal commands, tests and linters in the working environment.
  - `web_search`: Internet search (DuckDuckGo, no API key) to look up documentation, libraries and technical errors.
- **Interactive Inspector Panel**: Lets you enable or revoke individual tools with checkboxes grouped by permission category (Read, Write, Shell and Network), or pick quick profiles (`Coding`, `Read-Only`, `All`, `Custom`).
- **Compaction**: When the context reaches 90% of the model's window, the history is compacted automatically using the **Blackhole** or **Luhn+** algorithms.

### `transform.agent`
- **Label**: LLM Agent.
- **Description**: Flexible invocation of any OpenAI API-compatible endpoint (local Ollama, vLLM, OpenAI, Groq, etc.) with `{entrada}` variable injection, temperature control and JSON mode.

### `transform.script`
- **Label**: Python Code.
- **Description**: Execution of custom pure Python code. Receives the `inp` object with methods such as `.first_data()`, `.text()`, `.items()` and the `ctx` context. Allows any transformation with microsecond latency and zero token consumption.

### `transform.cve_audit`
- **Label**: CVE / SBOM Audit (OSV.dev).
- **Description**: Audits lists of components and dependencies by querying the **OSV.dev** open vulnerability database (Google / OpenSSF) through batch requests (up to 500 queries per call).
- **Supported formats**: CycloneDX JSON SBOM, SPDX JSON, lists of dictionaries with PURL, or `requirements.txt`.
- **Output**: Structured dictionary with metrics (`total_packages`, `vulnerable_packages`, `total_vulnerabilities`, `highest_severity`), a detailed list of CVEs/GHSAs with the patched version (*fixed version*) and a professional Markdown table report.
- **Parameters**: `ecosystem`, `severity_threshold` (ALL, LOW, MEDIUM, HIGH, CRITICAL), `max_results`, `timeout`.

### `transform.secret_scanner`
- **Label**: Secret Scanner (DLP).
- **Description**: Detects leaked secrets and exposed credentials in texts, logs or diffs by combining high-precision regular expressions (AWS, GitHub, Slack, JWTs, private keys) with **Shannon entropy** analysis for random cryptographic strings.
- **Modes**: `detect_and_redact` (replaces with `[REDACTED_SECRET]`), `detect_only` (lists the findings) and `mask` (partially masks `ab***12`).

### `transform.json_path`
- **Label**: Extract JSON Path.
- **Description**: Extracts specific values or filtered lists from complex JSON structures using an intuitive path syntax (e.g. `items[0].metadata.name`, `status.containerStatuses[*].state`).

### `transform.data_mapper`
- **Label**: Data Mapper.
- **Description**: Declaratively projects and restructures dictionaries or lists of objects from a mapping (e.g. `{"pod": "metadata.name", "ns": "metadata.namespace"}`).

### `transform.filter` and `transform.array_ops`
- **Labels**: Filter Items / List Operations.
- **Description**: Deterministic filtering without AI (`contains`, `not_contains`, `regex`, `>`, `<`), projections (field *pluck*), sorting, numeric limits and list flattening.

### `transform.diff`
- **Label**: Change Detection (Diff).
- **Description**: Keeps state between runs in `data/state/` using SHA-256 hashes. Emits only the truly new or modified items, discarding those already seen.

### `transform.fuzzy_dedup`
- **Label**: Fuzzy Deduplication.
- **Description**: String similarity algorithm (Levenshtein and Jaro-Winkler) to group and remove nearly identical news items or alerts without calling language models.

### `transform.summarize`
- **Label**: Summarize Text.
- **Description**: Deterministic extractive summarization using classic natural language processing algorithms: **Luhn**, **Luhn+** (with positional weights) and graph-based **TextRank**.

### `transform.clean_email` and `transform.clean_html`
- **Labels**: Text Cleaners.
- **Description**: `clean_html` removes tags and scripts; `clean_email` neutralizes prompt injection attacks hidden in emails via CSS (`font-size:0`, `display:none`), removes invisible Unicode characters (zero-width) and trims common signatures and legal disclaimers.

### `transform.threshold` and `transform.router`
- **Labels**: Conditional Flow Control.
- **Description**: `threshold` stops execution by emitting the `SKIPPED` sentinel if a value does not exceed the configured limits; `router` branches the output into different paths according to condition rules.

### `transform.approval`
- **Label**: Human Approval (Approval Gate).
- **Description**: Human validation gate for critical flows (production deployments, isolating compromised hosts, deletion or remediation). In interactive mode, it pauses execution and asks the operator for confirmation on the console (`[y/N]`), showing a preview of the incoming data.
- **Operating modes**:
  - **Approved**: Emits the input data intact to the downstream nodes.
  - **Rejected**: Depending on `on_reject`, emits the `SKIPPED` sentinel (cleanly skipping the rest of the branch) or stops the flow with an error (`error`).
  - **CI/CD Pipelines / Non-Interactive**: Lets you skip the interactive wait by using the `--yes`, `-y` or `--non-interactive` flags in exported scripts, the `DEDALO_AUTO_APPROVE=1` environment variable, or the `auto_approve` configuration parameter. If run in an environment without an interactive terminal (pipe/CI) and without auto-approval flags, it is safely rejected.
- **Parameters**: `prompt_message` (question or warning to display), `on_reject` (`"skip"` or `"error"`), `auto_approve` (`true`/`false`).
- **Output**: Received data intact on success, or the `SKIPPED` sentinel / `error`.

### `transform.template`, `transform.table`, `transform.datetime`, `transform.aggregate`, `transform.keywords_rake`, `transform.delay`, `transform.merge`
- **Description**: Auxiliary operations for template formatting, Markdown/HTML tables, time-zone-aware date calculations, statistical aggregates (sum, mean, percentiles), keyword extraction with RAKE, delays and merging multiple branches.

---

## 3. Output Nodes (`output.*`)

### `output.evidence_vault`
- **Label**: Evidence Vault.
- **Description**: Immutable forensic custody store for SecOps and SRE incidents.
- **How it works**: Saves the telemetry compressed as `.tar.gz` or `.json.gz`, computes the SHA-256 cryptographic hash of the content and generates an immutable `.meta.json` manifest for legal or technical audit.

### `output.jira_issue`
- **Label**: Jira Operations (Create / Comment / Update).
- **Description**: Runs actions against the Jira Cloud REST API v3:
  - `create`: Opens a new issue with issue type, priority and components.
  - `add_comment`: Adds comments in rich Atlassian Document Format (ADF).
  - `update`: Modifies fields such as priority, summary or labels.
  - `transition`: Changes the ticket status in the workflow (e.g. "In Progress", "Resolved", "Closed").
  - Supports batch processing (`batch`) when it receives a list of tickets.

### `output.confluence_page`
- **Label**: Publish or Update in Confluence.
- **Description**: Manages pages in Confluence Cloud using XHTML storage format.
- **Modes**:
  - `create_or_update`: If the page exists (by `page_id` or `title`), it fetches its previous version and does a `PUT` incrementing the version (`version + 1`). If it does not exist, it creates it.
  - `append`: Keeps all the current content of the page and adds the new paragraphs or sections at the end.

### `output.git`
- **Label**: Publish to Git.
- **Description**: Runs `git add`, `git commit` with configurable author and message, and optionally `git push` to the configured remote branch.

### `output.teams`
- **Label**: Microsoft Teams Webhook.
- **Description**: Sends rich cards (*MessageCard*) with a customizable accent color and interactive action buttons (e.g. a direct link to the Jira issue or to the vault) to Microsoft Teams channels via Incoming Webhook.

### `output.telegram`, `output.discord`, `output.slack`
- **Labels**: Messaging and Alerts.
- **Description**: Immediate notifications to chats, groups or channels with support for Markdown, embeds and alert formatting.

### `output.ntfy`
- **Label**: Push Notification (ntfy.sh).
- **Description**: Instant push notifications to mobile phones or browsers without registering accounts or paying for subscriptions, using the open ntfy protocol.

### `output.sqlite_exec`
- **Label**: Execute in SQLite (INSERT/UPDATE).
- **Description**: Persistently inserts or updates data records in local SQLite databases.

### `output.email_smtp`
- **Label**: Send Mail (SMTP).
- **Description**: Sends emails with STARTTLS or direct SSL authentication, HTML or plain-text body, and attachments.

### `output.file`
- **Label**: Save to File.
- **Description**: Writes files to disk in overwrite mode, append mode, or automatically numbered if the file already exists.

### `output.command`
- **Label**: Run Action (Shell).
- **Description**: Runs commands or scripts on the host system as the final step of the flow (restarting services, running ansible, etc.).

### `output.healthcheck`
- **Label**: Monitoring Ping (Heartbeat).
- **Description**: Sends periodic confirmation pings to monitoring services such as Healthchecks.io or Uptime Kuma to alert if a cron flow stops running.
