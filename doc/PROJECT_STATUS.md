# Dédalo Standalone — Project Status, Architecture and Relationship with Tomos

This document summarizes the current technical state of **Dédalo Standalone**, its origin and synchronization with the **Tomos** project, the architecture decisions adopted, and the complete record of implemented improvements. It serves as the baseline reference for continuing the development of Dédalo as a 100% self-contained project.

---

## 1. Vision and Current State

**Dédalo** is an ultralight visual workflow orchestrator designed specifically for resource-constrained environments (such as a Raspberry Pi or small home servers), without the overhead of tools like n8n, Node-RED or Airflow.

### Main Features in Standalone:
- **Minimal RAM usage**: ~14-16 MB in pure daemon mode (`--daemon`), ~28-34 MB in web server mode without frontend (`--headless`), and ~35-42 MB with the full web server on Linux/Raspberry Pi (compared to the 400+ MB typical of Node.js or Electron-based applications).
- **Backend**: Pure Python 3.10+ with FastAPI, Uvicorn and SQLite in WAL mode.
- **Frontend**: Vanilla JavaScript (ES6+) and native SVG. No bundlers (Webpack/Vite), no heavy frameworks (React/Vue).
- **Compilation to plain Python**: Visual graphs are not interpreted step by step in a reactive loop; they are compiled into a deterministic Python script (`<slug>.py`) with ternary operators and the `SKIPPED` sentinel.
- **Native Cron Evaluator**: Cron and interval parser (`@every 30m`, `@hourly`, etc.) 100% in pure Python with no external dependencies (`croniter` and `python-dateutil` removed).
- **Infinite Canvas**: Matrix transformation system with drag panning, zoom via mouse wheel or on-screen controls, and decoupled world coordinates.
- **Edge connections**: Input ports on the left edge and success/failure branches (`ok` / `error`) on the right edge.
- **Execution history**: Built-in SQLite database with automatic rotation every 24 hours (deletes runs older than 30 days).
- **Change tracking**: Detection of unsaved changes with a confirmation modal before switching flows.
- **Active Memory Optimization**: Periodic heap compaction with forced garbage collection and returning pages to the OS via `malloc_trim(0)`.
- **Anti-bot Evasion Without a Heavy Browser**: Dual HTTP engine with `curl_cffi` (Chrome/Safari/Firefox TLS/HTTP/2 impersonation and User-Agents) and transparent fallback to `httpx`.
- **Test suite**: 104 automated tests passing 100% in `pytest`.

---

## 2. Relationship with Tomos

### Origin
Dédalo was originally born as the automation and visual skills module within the **Tomos** project (`src/tomos/dedalo*.py` and `src/tomos/static/dedalo.*`). In Tomos, its purpose was to let the user create flows that consumed the search indexes, BOE (Spain's official gazette) summaries and language agents.

### Becoming Independent
It was decided to extract Dédalo into an independent repository (`dedalo`) in order to:
1. Use it as a general-purpose automation service on any machine or Raspberry Pi without installing Tomos's dependencies (PDF processing, local embeddings, mass scrapers, etc.).
2. Give it its own lifecycle, configuration, secrets and history.
3. Allow anyone to deploy it with a simple `git clone` and `./run.sh`.

### Compatibility and Synchronization (Backporting)
Despite the separation, Dédalo maintains data-level and conceptual-model compatibility with Tomos:
- **JSON Schema**: The graph serialization format (`nodes`, `edges`, configuration properties) is identical. Any flow exported from Dédalo can be copied to `data/skills/` in Tomos.
- **Compilation Pattern**: The compiler generates the same Python code based on `SKIPPED` sentinels and `entrada["nodo"]`.
- **Node Visual Design**: The connector layout (input on the left, `ok` output at 33% on the right, `error` output at 67% on the right) and the horizontal bézier curves are identical to the visual standard proven in Tomos.

### Key Differences of the Standalone Version Compared to Tomos
| Aspect | Tomos (Internal module) | Dédalo Standalone |
| :--- | :--- | :--- |
| **Server** | Integrated into the Tomos web app | Standalone FastAPI server on a configurable port (8080) |
| **Secret Management** | Inherits environment variables from Tomos | Own encrypted/protected store in `data/secrets.json` with `0600` permissions |
| **History** | Not persisted or in ephemeral memory | SQLite with WAL (`data/history.db`) and periodic 30-day purge |
| **Canvas** | Static position with container scroll | Fluid panning by dragging the background and free zoom with controls |
| **Node Palette** | Basic selection by click | Click or native drag (`HTML5 Drag & Drop`) to the exact spot |
| **Side Inspector** | Fixed in the right panel | Collapsible with a button and shortcut, persisted in `localStorage` |
| **Code Editor** | Simple textarea in the sidebar | Full-screen modal CodeMirror editor with variable viewer |
| **Node Catalog** | Focused on documentary tasks and Tomos | Expanded with 14 system nodes (SQLite, Webhooks, Raspberry Pi, Discord, Telegram, etc.) |

---

## 3. Technical and Architecture Decisions

1. **Zero Build Dependencies (No-Build)**:
   The entire frontend is delivered as direct static files (`index.html`, `dedalo.js`, `dedalo.css`). No Node.js, npm or build steps are required for the UI. This guarantees instant deployments and maximum code longevity without obsolete-dependency problems.

2. **World Coordinate Space on the Canvas**:
   The internal flow state stores each node's coordinates (`node.x`, `node.y`) in a continuous world coordinate system. The visual layer applies a single CSS transformation matrix (`transform: translate(...) scale(...)`) to the `#canvasContent` container. All mouse interactions (wires, node dragging, dropping from the palette) are translated with the `screenToWorld(clientX, clientY)` function, so pan and zoom never misalign elements or wires.

3. **Deterministic SVG Curve Calculation (`portCentre` & `curve`)**:
   Wires are neither straight lines nor arbitrary SVG curves. They follow a horizontal cubic bézier formula (`M x1 y1 C x1+dx y1, x2-dx y2, x2 y2`) with adaptive tension (`Math.max(38, dx * 0.45)`). Anchor points are computed relative to the edges of the source and target nodes, ensuring the visual flow always reads naturally from left to right.

4. **Lightweight Persistence in SQLite with WAL**:
   To record every execution without degrading SD card performance on a Raspberry Pi:
   - Write-Ahead Logging mode is enabled (`PRAGMA journal_mode=WAL`).
   - `PRAGMA synchronous=NORMAL` is used.
   - Direct parameterized queries are run with Python's native `sqlite3` module.
   - A background thread checks every 24 hours and purges records older than 30 days.

5. **Reactive Triggers via Webhooks**:
   Any flow that starts with an `input.webhook` node is immediately exposed at the `/api/webhook/{flow_id}` endpoint. Upon receiving an HTTP request (POST, GET or PUT), the server compiles and runs the flow in the background, passing the body, headers and query parameters as input data.

---

## 4. Record of Implemented Improvements

The improvements made on top of the standalone base are detailed below:

### A. Interface and User Experience (UI/UX)
- **Tomos-Style Side Connectors**:
  - Input port (`.port.in`) centered on the left edge (`left: -7px; top: 50%;`).
  - Output ports on the right edge: success (`.port.ok`, green) at 33% and error (`.port.error`, red) at 67%.
  - Descriptive `ok` and `error` labels (`.port-tag`) to the right of the connectors.
  - Removed the bottom inner bar (`.node-ports`), leaving the node body roomier and cleaner.
- **Friendly Drag-to-Connect**:
  - While dragging a wire, the available input ports emit a subtle pulse animation (`is-wiring`).
  - Ability to drop the wire on the connector or on **any part of the target node** (`document.elementFromPoint`).
- **Drag & Drop from the Left Palette**:
  - The sidebar pills (`.node-pill`) are draggable (`draggable="true"`).
  - When dropped on the canvas, the new node is created exactly at the cursor position translated into world space. Clicking the pill places it at the current visible center.
- **Color Coding by Category**:
  - Sidebar pills with accents and backgrounds by type: **Input** (sky blue `#38bdf8`), **Transform** (violet `#c084fc`) and **Output** (emerald green `#34d399`).
  - Node header on the canvas (`.node-header`) tinted with its category color.
- **Collapsible Side Inspector**:
  - Close button in the panel header (`#btnCollapseInspector`), a button in the top bar (`#btnToggleInspector`) and a floating tab on the canvas to restore it.
  - Automatic expansion when double-clicking any node.
  - Preference persisted in `localStorage`.
- **Full-Screen Modal Code Editor**:
  - For Python script nodes (`transform.script`), the inspector offers a button to open a modal with a vendored CodeMirror editor, syntax highlighting, keyboard shortcuts and an input-variable viewer.
- **Navigation and Pan/Zoom**:
  - Fluid panning by dragging the canvas background (with left or middle button).
  - Zoom via mouse wheel centered on the cursor.
  - Toolbar with Zoom In, Zoom Out, 100% and Fit Flow to screen buttons.
- **Unsaved Changes Management (Dirty State)**:
  - Modified-state indicator (`isDirty`) with a pulsing visual badge (`● Unsaved`) in the top bar next to the flow name.
  - Reactive update of the tab title (`● flow-name — Dédalo`) and a contextual glow on the save button (`#btnSave.is-dirty`).
  - Confirmation modal dialog before switching to another flow or creating a new one when there are pending changes.
- **Keyboard Shortcuts and Native Ergonomics**:
  - `Cmd+S` / `Ctrl+S`: Save the flow from any context (even while editing text or code).
  - `Delete` / `Backspace`: Delete the selected node directly (respecting inputs and modals).
  - `Cmd+D` / `Ctrl+D`: Duplicate the selected node with automatic offset and cloned configuration.
  - `Escape`: Deselect node, cancel wiring in progress or close any open modal.
  - `Spacebar (Space + Drag)`: Free, fluid canvas panning with a hand cursor (`grab` / `grabbing`).
- **Top Bar (Header) Hierarchy and Reorganization**:
  - Functional grouping into semantic blocks with subtle separators: *Flow* (`[Clear 🗑 | Templates... ▾]`), *Tools* (`[Code] [Schedule]`), *Execution* (`[Test ▶ | Debug 🪲]`), *Primary Action* (`[Save]`) and *Environment* (`[Inspector] [Theme]`).
  - Responsive flow name field with adaptive width to avoid truncating descriptive names.
- **Optimized Node Palette (Chips + Accordion)**:
  - Quick filter chips at the top (`All`, `Network/Web`, `Data`, `AI`, `Git`, `System`) to instantly filter the 56 nodes.
  - Collapsible category blocks (Input, Transform, Output) with a dynamic node counter and interactive chevron.
  - Automatic expansion of sections when searching in real time.
- **Clean Nodes Without Visual Noise**:
  - Removed the floating `ok` and `error` text labels from the right edge of nodes to avoid overlapping neighboring wires; the semantics are conveyed intuitively by the port color (emerald for success, red for failure) and native tooltips.
- **Fix for Visibility and Toggle Behavior in the Debug Bar**:
  - Resolved a CSS specificity conflict with the `[hidden]` attribute, ensuring that the step-by-step debug bar and the data viewer modal remain hidden by default until the user deliberately invokes them.
  - The top **Debug** button (`#btnDebugFlow`) now works as a *toggle*: a first click activates debug mode and opens the bar, while a second click closes the bar and ends the session.
  - The button takes on a persistent active state in primary blue (`.active`) while debug mode is active on the canvas, returning to its usual secondary style when closed or when the canvas is reset.
  - Compact, icon-only debug bar (`icon-btn` with native SVGs for Restart, Next step, Continue and Stop) and an adaptive node name with ellipsis truncation.
  - When advancing with **Next step**, the side inspector automatically shows the data of the node that was just executed (inputs, outputs and execution time). At the same time, the user is free to click any other node in the flow and examine its data without the debugger forcing the view.

### B. Execution History
- Dedicated tab in the sidebar with a historical list of executions, date/time, duration and status (`OK`, `ERR`, `RUN`).
- Detail modal sheet to inspect the inputs and outputs of each individual node.
- Visual highlighting on the canvas itself of the result of the last test (`node-run-ok`, `node-run-err`, `node-run-skip`).
- Automatic purge every 24h to discard executions older than 30 days.

### C. Expanded Node Catalog (14 New Ultralight Types)
Without adding heavy dependencies and taking advantage of Python's standard library:
- **Inputs**: `input.webhook` (HTTP trigger), `input.sqlite_query` (local SQL queries), `input.command` (controlled shell execution), `input.pi_health` (Raspberry Pi CPU/RAM telemetry), `input.directory_scan` (file scanning), `input.http_health` (web latency/status check).
- **Transforms**: `transform.html_extract` (CSS selector and HTML data extraction with no dependencies), `transform.template` (template substitution with variables), `transform.array_ops` (sort, filter, trim lists), `transform.regex` (extractions and replacements with regular expressions), `transform.router` (conditional splitter to the `ok` or `error` port).
- **Outputs**: `output.sqlite_exec` (write to a local database), `output.email_smtp` (send email via SMTP), `output.ntfy` (push notifications to phones via ntfy.sh), `output.discord` (webhooks with messages and embeds), `output.slack` (team notifications), `output.command` (output system command execution).

### D. Extreme Memory and Resource Optimization (3 Levels Implemented)
Designed to operate indefinitely on small servers and Raspberry Pis with guaranteed low consumption:
1. **Level 1: Immediate SQLite and Concurrency Tuning**:
   - Strict limit on SQLite cache in `dedalo/history.py`: `PRAGMA cache_size = -2000` (maximum 2 MB instead of the default of up to tens of MB), `PRAGMA mmap_size = 0` (avoids aggressive mapping into virtual memory), `PRAGMA temp_store = FILE`.
   - Uvicorn flags in `run.sh`: `--limit-concurrency 15`, `--timeout-keep-alive 5`, `--no-access-log`.
2. **Level 2: Dependency Removal, Headless Modes and Active Compaction**:
   - **Native Cron Evaluator (`dedalo/cron.py`)**: Complete replacement of the external `croniter` library (and its heavy dependency `python-dateutil`). Direct savings: **~5.4 MB of RAM** at load time. Supports `@every 30m` intervals, `@hourly`, `@daily` shortcuts and classic 5-field cron syntax.
   - **Heap Compactor (`dedalo/runtime.py:compact_memory`)**: Runs a forced `gc.collect()` and calls `libc.malloc_trim(0)` on Linux systems to immediately return unfragmented pages to the operating system after every flow execution and on every scheduler cycle.
   - **Headless Web Mode (`./run.sh --headless`)**: Skips mounting static files and the web UI. Keeps the REST API and webhooks active. Idle consumption: **~28-34 MB** on Linux.
   - **Pure Daemon Mode (`./run.sh --daemon` / `python -m dedalo.headless`)**: Runs only the flow scheduler (`Scheduler`) without Uvicorn, FastAPI or Starlette. Idle consumption: **~14-16 MB** on Linux.
3. **Level 3: Operating System and Container Isolation**:
   - glibc environment flags in `run.sh`: `MALLOC_TRIM_THRESHOLD_=65536` and `PYTHONOPTIMIZE=1`.
   - `systemd` service profile documented in `README.md` with `MemoryHigh=65M` and `MemoryMax=85M` limits.
   - **Hardened Docker Packaging (`Dockerfile` + `docker-compose.yml`)**: Multi-stage build, unprivileged user (`UID/GID 1000`), `cap_drop: ALL`, `no-new-privileges: true`, native Python healthcheck (no curl), `/tmp` mounted as `tmpfs` and strict memory control (128 MB limit).

### E. Web Retrieval with Anti-bot and TLS Evasion (`curl_cffi`)
To avoid the hundreds of megabytes of memory and CPU spikes of a headless browser such as Obscura, native support for **`curl_cffi`** (based on `libcurl-impersonate`) has been integrated:
- **Cryptographic TLS and HTTP/2 impersonation**: Replicates the exact fingerprint of real browsers (Chrome, Safari, Firefox), bypassing typical Cloudflare, Akamai and Datadome blocks with only ~2-4 MB of memory.
- **Nodes that benefit**:
  - `input.http_call` (REST requests and generic scraping).
  - `input.web_read` (downloading and cleaning web pages).
  - `input.rss` (downloading feeds protected by Cloudflare or Substack).
- **Engine Selector**: `auto` (uses `curl_cffi` with automatic fallback to `httpx`), `curl_cffi` or `httpx`.
- **Configurable User-Agent**: Choice of presets (`chrome`, `safari`, `firefox`, `mobile`, `bot`) or a free-form custom User-Agent.
- **Resilient fallback**: If a request in `auto` mode hits a failure in `curl_cffi`, it is automatically retried through `httpx` before reporting an error.

### F. Advanced Statistical Summarization (Luhn, Luhn+ and TextRank)
The deterministic, LLM-free summarization node (`transform.summarize`) has been enhanced:
- **Four Available Engines**:
  - **`luhn+`** (default): Filter of 120+ stopwords (Spanish and English), weighting by information density normalized by length ($\sqrt{N}$), position bias (*Lead Bias*, +50% for the opening defining sentence) and diversity-based selection (lightweight MMR that penalizes thematic redundancy).
  - **`textrank`**: Native pure-Python implementation of the classic algorithm by Mihalcea & Tarau (2004). Builds a lexical similarity graph between sentences and computes their centrality through damped PageRank iterations ($d=0.85$).
  - **`luhn`**: Classic simple-frequency algorithm for backward compatibility.
  - **`llm`**: Generative summarization through an OpenAI-compatible API.
- **Flexible Output Format (`format`)**:
  - `paragraph`: Classic continuous text in a single paragraph.
  - `bullets`: Markdown bullet list (`- Sentence`), ideal for digesting key points without syntactic jarring.
- **Configurable Limits**: Support for an exact number of sentences (`sentences: int`) or a percentage ratio of the document (`ratio: float`), complementing the classic presets (`short`, `medium`, `long`, `full`).
- **Robust Sentence Segmentation**:
  - Protection of common Spanish/English abbreviations and titles (`Dr.`, `Dra.`, `Sr.`, `p. ej.`, `art.`, `a. C.`, `c.`, `ca.`, `EE. UU.`, etc.) to avoid improper splits on internal periods.
  - Automatic cleanup of wiki section headings (`== Title ==`) and markdown headings (`###`).
  - Filtering of residual lines or footnote callouts without losing syntactic coherence.
- **Access to Complex Nested Structures**:
  - Both `transform.summarize` and `transform.template` now support dot-notation navigation with list indexes (e.g. `query.pages.0.extract`).
  - Automatic recursive search as a fallback mechanism if a key is not found at the root level.

### G. Multi-purpose Web Search (`input.web_search`)
A native web search node has been added that lets you feed any flow with live information from the internet without heavy dependencies:
- **Three Built-in Engines**:
  - **`duckduckgo`** (default): Zero configuration, free and no API keys required. Performs clean scraping with TLS evasion (`curl_cffi`/`httpx`), extracting titles and descriptive snippets and decoding the real destination URLs (unwrapping the `uddg` parameter). Automatically unescapes HTML entities.
  - **`tavily`**: Designed specifically for agents and AI (`https://api.tavily.com/search`). Requires attaching a secure secret with the API Key. Returns enriched contextual snippets and relevance scores (`score`).
  - **`exa`** (Metaphor): Neural and semantic search (`https://api.exa.ai/search`). Requires attaching a secure secret with the API Key (`x-api-key`). Returns snippets, textual highlights (`highlights`) and publication date.
- **Dynamic Query Interpolation**:
  - Supports templates with `{entrada}` to search for terms dynamically generated by previous nodes (RSS, transformations, LLM, etc.). If `query` is empty, it automatically uses the text of the connected node.
- **Flexible Output Formats (`output_format`)**:
  - `json`: Structured list of Python/JSON objects with `title`, `url`, `snippet` (and metadata such as `score` or `published_date`).
  - `markdown`: List formatted with bullets and Markdown links (`- [Title](URL): Snippet`), ready to chain into `transform.summarize`, `output.file` or messaging nodes (Telegram, Discord, Slack, Email).
- **Integration with the UI and Dédalo Secrets**:
  - The `secret` field is dynamically bound to the secrets store (`choices_from={"secret": "secrets"}`) and is displayed conditionally only when a commercial engine that requires it is selected (`show_when={"secret": {"engine": ["tavily", "exa"]}}`).

### H. Deterministic Non-LLM Nodes and Palette Search
To guarantee maximum autonomy, predictability and performance on a Raspberry Pi (~20 MB RAM) without incurring API costs or language-model latencies, a full battery of deterministic nodes based 100% on Python's standard library has been integrated:
- **Change and Novelty Detection (`transform.diff`)**:
  - Keeps a state snapshot in `data/state/{flow}_{key}.json` between successive executions.
  - Can emit only new (`added`), modified (`modified`), removed (`removed`) items or the global summary (`all`).
  - Prevents periodic flows from re-notifying items already seen on previous days.
- **Smart Fuzzy Deduplication (`transform.fuzzy_dedup`)**:
  - Combines `difflib.SequenceMatcher` with lexical set similarity (Jaccard and subset containment) to unify titles with slight variations between stores or newspapers.
  - Modes `keep_first`, `merge_list` (attaches the `duplicates` list) and `group` (clusters).
- **Statistical Aggregator (`transform.aggregate`)**:
  - Computes `count`, `sum`, `avg`, `min`, `max`, `median` and a full `summary` over numeric fields, with support for grouping by key (`group_by`).
- **Table Formatter (`transform.table`)**:
  - Renders lists of dictionaries as aligned Markdown tables or HTML with inline styles, ready for emails or visual notifications.
- **RAKE Keyword Extraction (`transform.keywords_rake`)**:
  - *Rapid Automatic Keyword Extraction* algorithm implemented in pure Python to extract key concepts and topics from texts without heavy NLP models.
- **Infrastructure and Network Monitoring (`input.ssl_cert`, `input.dns_check`, `input.ping`)**:
  - `ssl_cert`: Remaining days of validity, issuer, subject and status of TLS certificates (`ssl` and `socket`).
  - `dns_check`: IP address resolution and DNS response time.
  - `ping_host`: Connection latency in ms via TCP handshake (without requiring root privileges or raw sockets in Docker/Linux) or native ICMP ping.
- **Home Automation and IoT Connectivity (`output.mqtt` / `input.mqtt`)**:
  - Native client for the MQTT 3.1.1 binary protocol implemented over direct Python sockets (no external libraries needed). Publishes and reads messages on local brokers such as Mosquitto or Home Assistant.
- **IMAP Mail Reader (`input.imap`)**:
  - Secure IMAP over TLS (`imaplib` + `email`) with filter-based search (`UNSEEN`, `ALL`, etc.), MIME header decoding and extraction of body and attachments.
- **Live Search in the Node Palette**:
  - `#paletteSearch` field in the sidebar's nodes tab that instantly filters by name, type, category or description as you type, hiding empty categories.

### I. 5 High-Utility Nodes Inspired by n8n (52 Nodes in Total)
- **Pause and Wait with Jitter (`transform.delay`)**:
  - Halts the flow for `N` seconds (with an optional random `jitter` variation to evade mechanical patterns when scraping websites) and forwards the data unaltered.
- **Date and Time Operations (`transform.datetime`)**:
  - Calculation, Spanish-language formatting (`Domingo, 13 de Septiembre...`), adding/subtracting days or hours (`offset_days`, `offset_hours`), IANA time zone with native `zoneinfo` and text or structured dictionary output modes.
- **XML Sitemap Reader (`input.sitemap`)**:
  - Downloads and processes `sitemap.xml` files (including nested `<sitemapindex>` indexes), filtering by age in hours (`filter_hours`) or keywords in the URL.
- **Heartbeat Monitoring Ping (`output.healthcheck`)**:
  - Sends confirmation GET or POST requests to Healthchecks.io (automatic `/fail` if there was an error) or Uptime Kuma Push (`?status=up&msg=OK`), making it possible to monitor the health of crons.
- **Open Weather (`input.weather`)**:
  - Real-time query to Open-Meteo (100% free, no registration or API keys) with automatic geocoding by city name, multi-day forecast and WMO codes translated into Spanish with emojis.

### J. Flow Export and Import as ZIP and Full Backup
To ease portability, deployment between different machines (e.g. development workstation to Raspberry Pi) and backups without build dependencies:
- **Individual ZIP Export**:
  - Generates an in-memory ZIP file on the fly containing the canonical visual graph (`<flow_id>.json`), its cron metadata (`<flow_id>.meta.json`) and a manifest (`manifest.json`).
  - **Required Secrets Detection**: Inspects the flow's nodes and adds to the manifest the names of the secrets required for it to work (e.g. `TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`), never including their confidential values.
- **Full Backup (Backup All)**:
  - Endpoint `GET /api/flows/export-all` and a button in the UI header that packages all the system's flows into `dedalo_flows_backup_<timestamp>.zip`.
- **Smart and Secure Import**:
  - Endpoint `POST /api/flows/import` and a file picker in the sidebar with support for `.zip` and `.json` files.
  - **Zero Dependencies**: Implemented with Python standard modules (`zipfile`, `io`) and a native multipart parser to avoid the `python-multipart` dependency.
  - **Zip-Slip Security**: Thorough sanitization of relative paths with strict rejection of directory-escape sequences (`..`).
  - **DoS Protection**: 25 MB decompression limit.
  - **Automatic Recompilation**: Saves the graph and invokes `save_flow()`, immediately compiling it into executable Python (`<flow_id>.py`).

### K. Step-by-Step Debugging, Full Inspection and Real-Time Transformations
To diagnose complex data flows and experiment with transformations interactively without overloading remote services:
- **Step-by-Step Debugging on the Canvas**:
  - Floating toolbar (`#debugToolbar`) with state control (current node, next step, continue, restart and stop).
  - Visual highlighting on the canvas with a cyan pulse animation (`.node-debug-active`) on the node being debugged.
  - Targeted execution: "▶ Run to this node" button in the properties inspector to evaluate the DAG subgraph needed up to an arbitrary node.
  - Dedicated endpoints: `POST /api/flow/step` (with `step` and `run_to` modes) and backend function `runtime.execute_single_node()`.
- **Untruncated Data Inspection**:
  - In debug and inspection mode, data truncation is disabled (`truncate=False`), preserving inputs and outputs at 100% fidelity.
  - The node inspector includes `[Output]` and `[Input]` tabs, a toggled Formatted / Raw view and a quick copy-to-clipboard button.
  - Full-screen `#dataViewerModal` modal for deep inspection of JSON/HTML/Text data with line search, row counter and size calculation in KB/MB.
- **Real-Time Transformations (Live Preview Playground)**:
  - Interactive panel in the inspector for transform nodes (`transform.*`).
  - When modifying parameters (regular expressions, CSS selectors, date formats, Python scripts, etc.), the result is recalculated in real time (~2-4 ms) with a 250 ms debounce.
  - Automatically takes as input the real data produced by the parent node in the last execution/step, or lets you switch to a custom test sample.

### L. Secure Email Processing, Anti-Injection Sanitization and Daily Digest (53 Nodes in Total)
To automate reading and analyzing emails robustly against modern attack vectors without heavy dependencies:
- **Deep Anti Prompt-Injection Sanitization**:
  - Native `EmailSanitizerParser` in standard Python (`html.parser`): suppresses scripts, styles, metadata, HTML comments and elements with CSS hiding tricks (`display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, `color:transparent`, `color:#fff`, `text-indent:-9999px`, `aria-hidden="true"`, `hidden`).
  - Invisible Unicode character filter (`_INVISIBLE_CHARS_RE`): removes zero-width spaces/joiners (`​`, `‌`, `‍`, `⁠`, `﻿`), soft hyphens (`­`), Unicode tag planes (`\U000e0000` - `\U000e007f`) and bidirectional overrides.
  - Automatic trimming of mechanical signatures (`-- \n`, `Enviado desde mi iPhone`, `Get Outlook for iOS`, etc.) and removal of legal clauses and privacy disclaimers (GDPR) to avoid useless token spending and hallucinations in language models.
- **Standalone `transform.clean_email` Node and Improvements to `input.imap`**:
  - `input.imap` now supports configurable `clean_body`, `strip_signatures` and `max_body_chars`, cleaning both plain text and HTML and sanitizing sender and subject.
  - New catalog node `transform.clean_email` to clean correspondence coming from webhooks, files or external sources.
- **Per-Item Enrichment (`as_field`) and Manual Context Limit (`max_context`) in `transform.agent`**:
  - When running an LLM over lists (`per_item: true`), the `as_field` parameter lets you inject the model's response into a given field (e.g. `resumen` or `categoria`) while keeping the email's original metadata (`id`, `subject`, `from`, `date`, `attachments`) intact.
  - Configurable `max_context` parameter: lets you manually set the context window size. If the input plus the prompts exceed 90% of the available budget after reserving output tokens, it is automatically compacted with the extractive Luhn+ algorithm before issuing the HTTP request, preventing overflow errors on the server.
- **Official "Daily Email Digest" Template (`resumen-correos-diario`)**:
  - Preconfigured flow IMAP $\rightarrow$ Executive Assistant LLM (with a defensive system prompt that treats emails as untrusted external data and formats priorities as Urgent / Informational / Newsletters) $\rightarrow$ Telegram.

### M. Native Git Integration and Continuous Code Auditing (55 Nodes in Total)
To turn Dédalo into a lightweight CI/CD, GitOps and security-review orchestrator on low-power servers:
- **Zero Python Dependencies**: Implemented directly on top of the operating system's `git` executable via `subprocess.run()`, without using high-consumption or memory-leaking libraries (`GitPython`, `pygit2`).
- **Input Node `input.git`**:
  - `log`: Structured history with author, email, date, hash and affected files, with support for time windows (`since="24 hours ago"`) and ASCII delimiters that prevent format collisions.
  - `diff`: Extraction of the unified patch between revisions or branches with an extension filter (`path_filter`) and a safety limit (`max_chars`, 8000 characters by default) to prevent context overflows in LLMs.
  - `clone_or_pull`: Optimized initial download with shallow clones (`--depth 1 --no-tags`) that protect MicroSD card storage, and updating via `pull --ff-only`.
  - `status`: Inspection of the working tree to detect modified or untracked files.
- **Output Node `output.git`**:
  - Selective staging, commit generation with a configurable message and author (`author_name`, `author_email`) and optional remote push to use Git as a database or versioned storage (Obsidian, notes, reports).
- **Official "AI Commit Audit" Template (`auditoria-commits-git`)**:
  - Connects `input.git` (diff mode) $\rightarrow$ `transform.agent` (defensive audit looking for exposed credentials, security regressions and a summary of changes) $\rightarrow$ `output.telegram`.

### N. ReAct Coding Micro-Harness, Structured Tools and Luhn+ Compaction (56 Nodes in Total)
To enable autonomous resolution of coding tasks, regression testing and guided refactoring:
- **Native Tool Calling Protocol (OpenAI Compatible)**:
  - Implemented directly through standard HTTP requests with `httpx` (zero additional external dependencies), connecting to local inference servers (Ollama, vLLM) or remote ones (OpenAI, OpenRouter, DeepSeek).
- **Canonical Palette of 7 Tools and Presets**:
  - `ALL_TOOL_NAMES`: `("read", "bash", "edit", "write", "grep", "find", "ls")`.
  - `CODING_TOOL_NAMES`: `("read", "bash", "edit", "write")`.
  - `READ_ONLY_TOOL_NAMES`: `("read", "grep", "find", "ls")`.
  - `read` tool: reading with line numbering and bounded ranges.
  - `edit` tool: atomic replacement verifying strict uniqueness of the `old_str` block.
  - `write` tool: writing to disk, creating subdirectories automatically.
  - `bash` tool: execution in the workspace with timeout and exit code capture.
  - `grep`, `find` and `ls` tools: selective exploration ignoring dependency folders and binaries (`.git`, `.venv`, etc.).
- **Path Sandboxing**:
  - Strict confinement to `workspace_dir` via `Path.resolve().is_relative_to(base_dir)`. Any attempt to read or modify outside the workspace is blocked with a security error.
- **Server Context Window Autodetection**:
  - `_detect_context_window` function: queries `/api/show` on Ollama (extracting `num_ctx` or `model_info`), `/models` on OpenAI/OpenRouter endpoints and a heuristics table for known families (`gpt-4o`, `claude`, `qwen2.5-coder`, `deepseek`, `llama-3.x`).
- **Automatic Luhn+ Compaction at 90% Saturation**:
  - Continuous evaluation of the token budget (`~3.5` characters per token).
  - Upon reaching 90% of the context, the initial prompts and recent calls are preserved, and the dense intermediate history is synthesized using the extractive `Luhn+` algorithm (stopword filtering, length normalization, position bias and MMR diversity).
  - Additional defensive truncation of disproportionate individual tool outputs.
- **Official Templates with Coder**:
  - **Exception triage and patch proposal (`triaje-errores-coder`)**: Receives exception traces via webhook, inspects the surrounding code in read-only mode with `read`/`grep`, analyzes the root cause of the failure and sends the diagnosis with the proposed patch to Telegram without altering files on disk.
  - **Automatic test generation (`generar-tests-coder`)**: Extracts the diff of the latest code changes with `input.git`, writes new unit tests in `tests/` with `write`/`edit` imitating the existing suite, runs `pytest` through `bash` until it validates that they pass, and generates an atomic commit with `output.git`.
  - **Self-repair of web scrapers (`auto-reparar-scraper`)**: Downloads the updated HTML with `input.http_call` (curl_cffi with browser impersonation), inspects selectors in `scrapers/extractor.py`, applies patches with `edit` and validates the local execution through `bash` before alerting via Telegram.
  - **Documentation synchronization with Git (`sincronizar-docs-codigo`)**: Extracts the diff of the latest code changes with `input.git`, compares modified signatures and arguments against `README.md`, adjusts the documentation with `edit` and generates an atomic commit with `output.git`.

---

## 5. Repository Structure

```text
dedalo/
├── data/
│   ├── flows/              # JSON definitions and compiled Python scripts
│   ├── history.db          # Execution history in SQLite (WAL mode, 2MB cache)
│   ├── secrets.json        # Tokens and credentials (0600 permissions)
│   └── state/              # State persisted between executions (e.g. RSS)
├── dedalo/
│   ├── __init__.py
│   ├── app.py              # REST API with FastAPI (supports the DEDALO_HEADLESS flag)
│   ├── compiler.py         # Compiler from visual graphs to sequential Python scripts
│   ├── cron.py             # Ultralight native cron evaluator (croniter replacement)
│   ├── headless.py         # Pure daemon runner without a web server (~14-16 MB RAM)
│   ├── history.py          # SQLite database manager for history and purging
│   ├── llms.py             # Multi-endpoint LLM management and secret resolution
│   ├── runtime.py          # Node executor, sandbox and compact_memory()
│   ├── scheduler.py        # Task scheduler with native cron and compaction
│   ├── secrets.py          # Secure loading and storage of secrets
│   ├── static/             # Standalone frontend (unbundled HTML, CSS and JS)
│   │   ├── dedalo.css
│   │   ├── dedalo.js
│   │   └── index.html
│   ├── store.py            # Flow persistence as JSON/PY and ZIP packaging
│   └── templates.py        # Quick-start templates
├── tests/                  # Automated tests (pytest - 119 tests)
│   ├── test_api.py
│   ├── test_coder.py       # Tests for transform.coder, tools, sandboxing and Luhn+
│   ├── test_compiler.py
│   ├── test_cron.py        # Tests for the native cron and interval evaluator
│   ├── test_debug.py       # Tests for isolated execution, step-by-step and live preview
│   ├── test_email_digest.py # Tests for anti prompt-injection sanitization and digest
│   ├── test_git.py         # Tests for native Git integration (log, diff, commit, push)
│   ├── test_history.py
│   ├── test_llms.py        # Tests for multi-endpoint LLM management and fallback
│   ├── test_runtime.py
│   ├── test_secrets.py
│   └── test_store.py       # Tests for ZIP export/import and Zip-Slip
├── .dockerignore           # Strict exclusion of secrets, caches and venv
├── Dockerfile              # Secure multi-stage build with non-root user
├── docker-compose.yml      # Deployment with memory limits and hardening
├── pyproject.toml          # Minimal dependencies (no croniter or dateutil)
├── run.sh                  # Startup with --headless and --daemon options
├── stop.sh                 # Script to stop the service
└── README.md               # General documentation, Docker and systemd
```

---

## 6. Guide to Continue Development

### Environment and Execution
To work on this project in isolation:
```bash
cd dedalo

# Start the application (at http://localhost:8080)
./run.sh

# Run the automated test suite
.venv/bin/pytest

# Stop the server
./stop.sh
```

### Ideas and Possible Next Steps (Roadmap)
1. **Component Library / Subflows**: Ability to reuse a flow inside another one as a single composite node.
2. **Live Metrics on the Canvas**: Numeric indicator of items processed or elapsed time overlaid on each output connector.
3. **Local Git Version History**: Automatic local revision log for every saved flow modification.
4. **Conditional Breakpoints**: Pause execution on the canvas only when the data meets a condition (e.g. empty list or error).
