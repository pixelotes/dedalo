"""Compilador de grafos de Dédalo a código Python plano.

Un flujo se define como un grafo acíclico dirigido (DAG) de nodos de entrada,
transformación y salida. Al compilarse, no se ejecuta un intérprete de grafos:
se genera un script Python independiente con una función plana `run(ctx)`.
"""

from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from dedalo.runtime import DEFAULT_AGENT_TOKENS
from dedalo.i18n import tr

_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENTRADA_REF_RE = re.compile(r"""entrada(?:\s*\[\s*|\.get\(\s*)['"]([^'"]+)['"]""")
_compile_source = compile

VERSION = 1
PORTS = ("ok", "error")
FAN_IN_POLICIES = ("all", "any")


class GraphError(Exception):
    """Lanzada por `compile()` cuando el grafo no pasa la validación."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(errors))


@dataclass
class Node:
    id: str
    type: str  # ej. "input.rss", "transform.agent", "output.file"
    label: str = ""
    config: dict = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "config": dict(self.config),
            "x": self.x,
            "y": self.y,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        return cls(
            id=str(data["id"]),
            type=str(data["type"]),
            label=str(data.get("label", "")),
            config=dict(data.get("config") or {}),
            x=float(data.get("x", 0) or 0),
            y=float(data.get("y", 0) or 0),
        )


@dataclass
class Edge:
    source: str
    target: str
    port: str = "ok"  # "ok" o "error"

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target, "port": self.port}

    @classmethod
    def from_dict(cls, data: dict) -> "Edge":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            port=str(data.get("port", "ok")),
        )


@dataclass
class Graph:
    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    version: int = VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Graph":
        return cls(
            name=str(data.get("name", "")),
            nodes=[Node.from_dict(n) for n in data.get("nodes") or []],
            edges=[Edge.from_dict(e) for e in data.get("edges") or []],
            version=int(data.get("version", VERSION)),
        )

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target == node_id]

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source == node_id]


@dataclass
class NodeSpec:
    category: str  # "input" | "transform" | "output"
    runtime_fn: str  # Nombre de la función en dedalo.runtime
    required: frozenset = frozenset()
    optional: dict = field(default_factory=dict)
    label: str = ""
    output: str = "texto"
    code_field: str = ""
    show_when: dict = field(default_factory=dict)
    choices: dict = field(default_factory=dict)
    choices_from: dict = field(default_factory=dict)
    help: dict = field(default_factory=dict)


NODE_TYPES: dict[str, NodeSpec] = {
    # -- Entradas -----------------------------------------------------------
    "input.webhook": NodeSpec(
        category="input",
        runtime_fn="webhook_trigger",
        label='Incoming Webhook',
        output='received payload (dict or text)',
        help={
            "url_info": 'Receives HTTP requests at /api/webhook/{flow_name}.',
        },
    ),
    "input.http_call": NodeSpec(
        category="input",
        runtime_fn="http_call",
        required=frozenset({"url"}),
        optional={
            "method": "GET",
            "headers": "",
            "body": "",
            "secret": "",
            "auth_header": "Authorization",
            "timeout": 20.0,
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label='Fetch from an API / Web',
        output='dict if it returns JSON, text otherwise',
        choices={
            "method": ["GET", "POST", "PUT", "DELETE"],
            "engine": [
                {"value": "auto", "label": 'Auto (curl_cffi with httpx fallback)'},
                {"value": "curl_cffi", "label": 'curl_cffi (TLS/browser impersonation)'},
                {"value": "httpx", "label": 'httpx (standard HTTP client)'},
            ],
            "impersonate": [
                {"value": "chrome", "label": 'Google Chrome (recommended)'},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": 'No impersonation'},
            ],
        },
        choices_from={"secret": "secrets"},
        help={
            "url": 'URL of the REST API or web page to query.',
            "body": 'Body for POST/PUT (supports {entrada} or JSON).',
            "secret": 'Name of the secret holding the authentication token.',
            "auth_header": 'Header that carries the secret (Authorization by default).',
            "timeout": 'Timeout in seconds.',
            "engine": "HTTP engine: 'auto' uses curl_cffi to bypass anti-bot protections, or httpx as a fallback.",
            "impersonate": 'Browser to impersonate at the TLS and HTTP/2 level to evade anti-bot blocks (Cloudflare, etc.).',
            "user_agent": "Optional User-Agent. Presets: 'chrome', 'safari', 'firefox', 'mobile', 'bot', or free text.",
        },
    ),
    "input.rss": NodeSpec(
        category="input",
        runtime_fn="rss",
        required=frozenset({"url"}),
        optional={
            "only_new": False,
            "corte": "completo",
            "max_items": 20,
            "filter_keyword": "",
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label='RSS / Atom Feed',
        output='list of news items (title, link, guid, date, description)',
        choices={
            "corte": [
                {"value": "300", "label": '300 characters'},
                {"value": "600", "label": '600 characters'},
                {"value": "1000", "label": '1000 characters'},
                {"value": "completo", "label": 'Full'},
            ],
            "engine": [
                {"value": "auto", "label": 'Auto (curl_cffi if available)'},
                {"value": "curl_cffi", "label": 'curl_cffi (TLS impersonation)'},
                {"value": "httpx", "label": 'httpx (standard client)'},
            ],
            "impersonate": [
                {"value": "chrome", "label": "Google Chrome"},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": 'No impersonation'},
            ],
        },
        help={
            "only_new": 'Skips news items already seen in previous runs.',
            "max_items": 'Maximum number of news items to extract.',
            "filter_keyword": 'Filters news items containing this word.',
            "corte": 'Character limit per description.',
            "engine": 'HTTP engine used to download the feed (useful for feeds behind Cloudflare or Substack).',
            "impersonate": 'Browser to impersonate to prevent the feed from being blocked.',
            "user_agent": 'Optional User-Agent for the feed request.',
        },
    ),
    "input.web_read": NodeSpec(
        category="input",
        runtime_fn="web_read",
        required=frozenset({"url"}),
        optional={
            "length": "medium",
            "engine": "auto",
            "impersonate": "chrome",
            "user_agent": "",
        },
        label='Extract Web Page',
        output='clean, condensed text',
        choices={
            "length": ["short", "medium", "long", "full"],
            "engine": [
                {"value": "auto", "label": 'Auto (curl_cffi if available)'},
                {"value": "curl_cffi", "label": 'curl_cffi (TLS impersonation)'},
                {"value": "httpx", "label": 'httpx (standard client)'},
            ],
            "impersonate": [
                {"value": "chrome", "label": "Google Chrome"},
                {"value": "safari", "label": "Apple Safari"},
                {"value": "firefox", "label": "Mozilla Firefox"},
                {"value": "none", "label": 'No impersonation'},
            ],
        },
        help={
            "url": 'URL of the web page to read and clean.',
            "length": 'Desired format of the extracted text.',
            "engine": "HTTP engine: 'auto' uses curl_cffi to bypass anti-bot protections, or httpx as a fallback.",
            "impersonate": 'Browser profile to impersonate (Chrome, Safari, Firefox).',
            "user_agent": 'Optional User-Agent (leave empty to use the native one of the impersonated browser).',
        },
    ),
    "input.web_search": NodeSpec(
        category="input",
        runtime_fn="web_search",
        required=frozenset({"query"}),
        optional={
            "engine": "duckduckgo",
            "limit": 5,
            "secret": "",
            "output_format": "json",
            "timeout": 15.0,
        },
        label='Web Search',
        output='search results (JSON list or markdown text)',
        choices={
            "engine": [
                {"value": "duckduckgo", "label": 'DuckDuckGo (free, no key)'},
                {"value": "tavily", "label": 'Tavily Search API (requires API key)'},
                {"value": "exa", "label": 'Exa / Metaphor API (requires API key)'},
            ],
            "output_format": [
                {"value": "json", "label": 'Structured (JSON / list of dicts)'},
                {"value": "markdown", "label": 'Formatted Markdown list'},
            ],
        },
        choices_from={"secret": "secrets"},
        show_when={"secret": {"engine": ["tavily", "exa"]}},
        help={
            "query": 'Search term or query (supports {entrada}).',
            "engine": 'Search engine: DuckDuckGo (no API key), Tavily or Exa.',
            "limit": 'Maximum number of results to fetch.',
            "secret": 'Name of the secret with the API key (required for Tavily and Exa).',
            "output_format": "Output format: 'json' (structured list) or 'markdown' (bulleted links).",
            "timeout": 'Maximum time in seconds for the request.',
        },
    ),
    "input.csv_file": NodeSpec(
        category="input",
        runtime_fn="csv_file",
        required=frozenset({"path"}),
        optional={"delimiter": ",", "encoding": "utf-8"},
        label='CSV File',
        output='list of dicts (rows)',
        choices={
            "delimiter": [
                {"value": ",", "label": 'Comma (,)'},
                {"value": ";", "label": 'Semicolon (;)'},
                {"value": "\\t", "label": 'Tab (\\t)'},
                {"value": "|", "label": 'Pipe (|)'},
            ],
            "encoding": ["utf-8", "latin-1", "ascii"],
        },
        help={"path": 'Path to the CSV file on disk.'},
    ),
    "input.json_file": NodeSpec(
        category="input",
        runtime_fn="json_file",
        required=frozenset({"path"}),
        label='JSON File',
        output='dict or list depending on the file',
        help={"path": 'Path to the JSON file on disk.'},
    ),
    "input.sqlite_query": NodeSpec(
        category="input",
        runtime_fn="sqlite_query",
        required=frozenset({"query"}),
        optional={"path": "data/store.db"},
        label='SQLite Query (SELECT)',
        output='list of dicts (rows)',
        help={
            "query": 'SQL SELECT query to run.',
            "path": 'Path to the SQLite database file.',
        },
    ),
    "input.command": NodeSpec(
        category="input",
        runtime_fn="command_input",
        required=frozenset({"cmd"}),
        optional={"timeout": 15.0, "cwd": "", "sandbox": ""},
        label='System Command (Shell)',
        output='dict with stdout, stderr, exit_code',
        help={
            "cmd": 'Bash/shell command to run (supports {entrada}).',
            "timeout": 'Maximum execution time in seconds.',
            "cwd": 'Working directory, relative or absolute.',
            "sandbox": 'Isolation with bubblewrap: only sees /usr, /etc and this working directory; data/ hidden.',
        },
        choices={
            "sandbox": [
                {"value": "", "label": 'According to DEDALO_SANDBOX (default)'},
                {"value": "on", "label": 'Isolate with bubblewrap (fails if unavailable)'},
                {"value": "off", "label": 'Not isolated'},
            ],
        },
    ),
    "input.git": NodeSpec(
        category="input",
        runtime_fn="git_read",
        optional={
            "action": "log",
            "repo_path": ".",
            "url": "",
            "revision": "HEAD~1..HEAD",
            "since": "",
            "limit": 5,
            "path_filter": "",
            "max_chars": 8000,
            "timeout": 30.0,
        },
        label='Git Reader',
        output='commits (log), patch (diff), or repository status',
        choices={
            "action": [
                {"value": "log", "label": 'Commit history (log)'},
                {"value": "diff", "label": 'Change patch (diff)'},
                {"value": "clone_or_pull", "label": 'Clone or update (clone/pull)'},
                {"value": "status", "label": 'Tree status (status)'},
            ],
        },
        help={
            "action": 'Action to run: log (commits), diff (changes), clone_or_pull (clone/update), status.',
            "repo_path": 'Local path to the repository (defaults to . for the current repo).',
            "url": 'Remote Git URL (required when cloning a nonexistent repo).',
            "revision": 'Revision range or reference for diff (e.g. HEAD~1..HEAD or main..feature).',
            "since": "Time window for the log (e.g. '24 hours ago', 'yesterday').",
            "limit": 'Maximum number of commits to return in log mode.',
            "path_filter": "Comma-separated path or extension filter (e.g. '*.py, *.js').",
            "max_chars": 'Maximum character limit of the diff to avoid overflowing tokens.',
        },
    ),
    "input.pi_health": NodeSpec(
        category="input",
        runtime_fn="pi_health",
        label='System Health (Pi / Linux)',
        output='dict with cpu_temp_c, memory_percent_used, memory_free_mb, load_1m, disk_percent_used',
        help={
            "info": 'Reads CPU temperature, memory and load directly from /proc and /sys with no dependencies.',
        },
    ),
    "input.directory_scan": NodeSpec(
        category="input",
        runtime_fn="directory_scan",
        optional={"path": ".", "pattern": "*", "recursive": False, "max_items": 50},
        label='Scan Directory',
        output='list of files (name, path, size_bytes, modified_at)',
        help={
            "path": 'Folder to scan.',
            "pattern": 'Search pattern (e.g. *.log, *.pdf).',
            "recursive": 'Include subdirectories.',
        },
    ),
    "input.http_health": NodeSpec(
        category="input",
        runtime_fn="http_health",
        required=frozenset({"url"}),
        optional={"timeout": 5.0, "expected_status": 200},
        label='HTTP Health Check',
        output='dict with url, status_code, latency_ms, is_healthy',
        help={
            "url": 'URL to check.',
            "expected_status": 'Expected response code.',
        },
    ),
    "input.telemetry": NodeSpec(
        category="input",
        runtime_fn="telemetry_input",
        optional={
            "incident_id": "",
            "resource": "",
            "telemetry_json": "",
            "file_path": "",
        },
        label='Incident Telemetry',
        output='dict with incident_id, resource, timestamp and structured telemetry',
        help={
            "incident_id": 'Unique incident identifier (optional, auto-generated if empty).',
            "resource": 'Target resource (e.g. cluster/pod, Azure subscription, SaaS tenant).',
            "telemetry_json": 'JSON or text with direct logs/metrics (optional if it comes from another node).',
            "file_path": 'Path to a local file with the logs or event dump (optional).',
        },
    ),
    "input.jira_issues": NodeSpec(
        category="input",
        runtime_fn="jira_issues_input",
        required=frozenset({"instance_url"}),
        optional={
            "email": "",
            "secret": "",
            "jql": "",
            "fields": "summary,status,priority,assignee,created,updated,description",
            "max_results": 20,
            "timeout": 20.0,
        },
        label="Jira Issues (JQL)",
        output='dict with issues (list of tickets), total, count and jql',
        choices_from={"secret": "secrets"},
        help={
            "instance_url": 'Base URL of the Jira Cloud instance (e.g. https://your-company.atlassian.net).',
            "email": 'Atlassian user email.',
            "secret": 'Name of the secret with the Atlassian API token.',
            "jql": 'JQL query (e.g. project = SEC AND status != Closed ORDER BY created DESC).',
            "fields": 'Comma-separated fields to extract.',
            "max_results": 'Maximum number of tickets to retrieve.',
        },
    ),
    "input.confluence_pages": NodeSpec(
        category="input",
        runtime_fn="confluence_pages_input",
        required=frozenset({"instance_url"}),
        optional={
            "email": "",
            "secret": "",
            "space_key": "",
            "title": "",
            "cql": "",
            "limit": 10,
            "timeout": 20.0,
        },
        label='Confluence Pages (CQL)',
        output='dict with pages (list of pages with id, title, space, url, preview), total and cql',
        choices_from={"secret": "secrets"},
        help={
            "instance_url": 'Confluence Cloud URL (e.g. https://your-company.atlassian.net/wiki).',
            "email": 'Atlassian user email.',
            "secret": 'Name of the secret with the Atlassian API token.',
            "space_key": 'Space key (e.g. IT, SEC, DOCS).',
            "title": 'Search by title or text match.',
            "cql": 'Optional advanced CQL query (e.g. type=page and space=SEC).',
            "limit": 'Maximum number of pages to retrieve.',
        },
    ),
    "input.disk_free": NodeSpec(
        category="input",
        runtime_fn="disk_free",
        optional={"path": ""},
        label='Free Disk Space',
        output='number (% free)',
    ),
    "input.cpu_load": NodeSpec(
        category="input",
        runtime_fn="cpu_load",
        optional={"seconds": 1.0},
        label='CPU Usage',
        output='number (% load)',
    ),
    "input.ssl_cert": NodeSpec(
        category="input",
        runtime_fn="ssl_cert",
        required=frozenset({"host"}),
        optional={"port": 443, "timeout": 5.0},
        label='SSL/TLS Certificate',
        output='dict with days_left, expires_at, is_valid, issuer',
        help={
            "host": 'Domain or host to check (e.g. google.com or my-server.org).',
            "port": 'TLS port (default 443).',
            "timeout": 'Maximum time in seconds.',
        },
    ),
    "input.dns_check": NodeSpec(
        category="input",
        runtime_fn="dns_check",
        required=frozenset({"host"}),
        optional={"record_type": "A", "timeout": 5.0},
        label='DNS Check',
        output='dict with ips, canonical_name, response_time_ms',
        help={
            "host": 'Domain to resolve (e.g. example.com).',
            "timeout": 'Timeout in seconds.',
        },
    ),
    "input.whois_rdap": NodeSpec(
        category="input",
        runtime_fn="whois_rdap",
        required=frozenset({"domain"}),
        optional={"timeout": 15.0},
        label='WHOIS / RDAP (Domain)',
        output='dict with registrar, expiry, days remaining, nameservers and status',
        help={
            "domain": 'Domain name to audit (e.g. mycompany.com or google.es).',
            "timeout": 'Maximum time in seconds for the RDAP query.',
        },
    ),
    "input.dns_record": NodeSpec(
        category="input",
        runtime_fn="dns_record",
        required=frozenset({"name"}),
        optional={"type": "A", "provider": "cloudflare", "timeout": 8.0},
        label='DNS Records (DoH)',
        output='dict with name, type, status, answers and list of values',
        choices={
            "type": ["A", "AAAA", "MX", "TXT", "CNAME", "NS", "SOA", "CAA"],
            "provider": [
                {"value": "cloudflare", "label": "Cloudflare DNS (1.1.1.1)"},
                {"value": "google", "label": "Google DNS (8.8.8.8)"},
            ],
        },
        help={
            "name": 'Host or domain name to query (e.g. example.com).',
            "type": 'DNS record type (A, AAAA, MX, TXT for SPF/DMARC, CNAME, NS, SOA).',
            "provider": 'DNS-over-HTTPS (DoH) provider.',
        },
    ),
    "input.ping": NodeSpec(
        category="input",
        runtime_fn="ping_host",
        required=frozenset({"host"}),
        optional={"port": 80, "mode": "tcp", "timeout": 3.0},
        label='Ping and Latency',
        output='dict with alive, latency_ms, host',
        choices={
            "mode": [
                {"value": "tcp", "label": 'TCP Connect (no root)'},
                {"value": "icmp", "label": 'ICMP Ping (system)'},
            ],
        },
        help={
            "host": 'Host or IP to check.',
            "port": 'Port for TCP ping (default 80).',
            "mode": 'Check mode: TCP or ICMP.',
        },
    ),
    "input.mqtt": NodeSpec(
        category="input",
        runtime_fn="mqtt_read",
        required=frozenset({"topic"}),
        optional={"broker": "localhost", "port": 1883, "username": "", "secret": "", "timeout": 5.0},
        label='MQTT Subscribe',
        output='message received on the topic',
        choices_from={"secret": "secrets"},
        help={
            "topic": 'MQTT topic to listen on.',
            "broker": 'Broker address (e.g. localhost or 192.168.1.50).',
            "secret": 'Secret with the password if the broker requires auth.',
        },
    ),
    "input.imap": NodeSpec(
        category="input",
        runtime_fn="imap_read",
        required=frozenset({"host", "username"}),
        optional={
            "port": 993,
            "secret": "",
            "folder": "INBOX",
            "filter": "UNSEEN",
            "limit": 5,
            "mark_as_read": False,
            "timeout": 15.0,
            "clean_body": True,
            "strip_signatures": True,
            "max_body_chars": 3000,
        },
        label='Mail Reader (IMAP)',
        output='list of incoming emails with subject, sender and body',
        choices_from={"secret": "secrets"},
        choices={
            "filter": [
                {"value": "UNSEEN", "label": 'Unread only (UNSEEN)'},
                {"value": "ALL", "label": 'All recent (ALL)'},
                {"value": "FLAGGED", "label": 'Flagged (FLAGGED)'},
            ],
        },
        help={
            "host": 'IMAP server (e.g. imap.gmail.com).',
            "username": 'Login username or email.',
            "secret": 'Secret with the password or App Password.',
            "folder": 'Mailbox to query (INBOX by default).',
            "limit": 'Maximum number of emails to retrieve.',
            "mark_as_read": 'If true, marks the messages as read.',
            "clean_body": 'Cleans HTML, hidden styles and invisible characters (anti prompt injection).',
            "strip_signatures": 'Removes mechanical signatures and legal/GDPR clauses from the body.',
            "max_body_chars": 'Maximum character limit per email body.',
        },
    ),
    "input.sitemap": NodeSpec(
        category="input",
        runtime_fn="sitemap_read",
        required=frozenset({"url"}),
        optional={
            "max_urls": 50,
            "filter_hours": 0.0,
            "filter_keyword": "",
            "follow_sub_sitemaps": True,
        },
        label='Sitemap Reader (XML)',
        output='list of URLs found with lastmod date',
        help={
            "url": 'URL of the sitemap.xml file (e.g. https://example.com/sitemap.xml).',
            "max_urls": 'Maximum number of URLs to retrieve.',
            "filter_hours": 'Recent hours based on the lastmod date (0 = no limit, e.g. 24).',
            "filter_keyword": 'Filter URLs containing this word or segment.',
            "follow_sub_sitemaps": 'If it is a sitemapindex, automatically descends into sub-sitemaps.',
        },
    ),
    "input.weather": NodeSpec(
        category="input",
        runtime_fn="weather_forecast",
        optional={
            "city": "Valencia",
            "latitude": 0.0,
            "longitude": 0.0,
            "forecast_days": 1,
            "format": "summary",
        },
        label='Weather (Open-Meteo)',
        output='weather summary and forecast',
        choices={
            "format": [
                {"value": "summary", "label": 'Structured summary (dict)'},
                {"value": "markdown", "label": 'Formatted text (Markdown)'},
                {"value": "raw", "label": 'Raw Open-Meteo JSON'},
            ]
        },
        help={
            "city": 'City to query (automatic geocoding, no API key).',
            "latitude": 'Exact latitude (optional if a city is given).',
            "longitude": 'Exact longitude (optional if a city is given).',
            "forecast_days": 'Forecast days to include (1 to 7).',
        },
    ),
    # -- Transformaciones ---------------------------------------------------
    "transform.delay": NodeSpec(
        category="transform",
        runtime_fn="delay_execution",
        optional={
            "seconds": 2.0,
            "jitter": 0.0,
        },
        label='Pause / Wait (Delay)',
        output='same data received (unmodified)',
        help={
            "seconds": 'Seconds to pause before emitting the data to the next node.',
            "jitter": 'Maximum random variation in seconds (0 to jitter) to avoid detection when scraping.',
        },
    ),
    "transform.datetime": NodeSpec(
        category="transform",
        runtime_fn="datetime_ops",
        optional={
            "mode": "format",
            "preset": "es_long",
            "format_str": "%Y-%m-%d %H:%M:%S",
            "offset_days": 0,
            "offset_hours": 0,
            "offset_minutes": 0,
            "timezone": "Europe/Madrid",
        },
        label='Date and Time Operations',
        output='formatted date (text) and metadata (dict)',
        choices={
            "mode": [
                {"value": "format", "label": 'Format received / current date'},
                {"value": "now", "label": 'Current system time'},
                {"value": "offset", "label": 'Apply offset (add/subtract time)'},
            ],
            "preset": [
                {"value": "es_long", "label": 'Long Spanish (e.g. Domingo, 13 de Septiembre...)'},
                {"value": "es_short", "label": 'Short Spanish (e.g. 13 de Sep de 2026 14:30)'},
                {"value": "iso", "label": 'ISO 8601 standard'},
                {"value": "date_only", "label": 'Date only (YYYY-MM-DD)'},
                {"value": "time_only", "label": 'Time only (HH:MM:SS)'},
                {"value": "custom", "label": 'Custom with format_str'},
            ],
        },
        help={
            "mode": 'Operation mode.',
            "preset": 'Default output format.',
            "offset_days": 'Days to add (positive) or subtract (negative).',
            "offset_hours": 'Hours to add or subtract.',
            "timezone": 'IANA time zone (e.g. Europe/Madrid, UTC, America/Buenos_Aires).',
        },
    ),
    "transform.template": NodeSpec(
        category="transform",
        runtime_fn="template",
        required=frozenset({"template"}),
        label='Text Template',
        output='text with interpolated variables',
        help={
            "template": 'Template with placeholders such as {entrada}, {fecha}, {hora}, {campo}.',
        },
    ),
    "transform.array_ops": NodeSpec(
        category="transform",
        runtime_fn="array_ops",
        optional={"operation": "head", "count": 10, "field": "", "order": "asc", "join_sep": "\\n"},
        label='List Operations',
        output='filtered/sorted list or joined text',
        choices={
            "operation": [
                {"value": "head", "label": 'First N items'},
                {"value": "tail", "label": 'Last N items'},
                {"value": "sort", "label": 'Sort by field'},
                {"value": "dedup", "label": 'Deduplicate items'},
                {"value": "join", "label": 'Join into a single text (Join)'},
            ],
            "order": ["asc", "desc"],
        },
        help={
            "field": 'Field to sort or deduplicate by (if items are dictionaries).',
            "join_sep": 'Separator for the join operation (newline by default).',
        },
    ),
    "transform.diff": NodeSpec(
        category="transform",
        runtime_fn="diff_data",
        optional={"key_field": "", "mode": "added", "state_key": ""},
        label='Change Detection (Diff)',
        output='new or changed items compared to the previous run',
        choices={
            "mode": [
                {"value": "added", "label": 'Only new items (added)'},
                {"value": "modified", "label": 'Only modified (modified)'},
                {"value": "removed", "label": 'Only removed (removed)'},
                {"value": "all", "label": 'All changes (dict with added/modified/removed)'},
            ],
        },
        help={
            "key_field": 'Unique identifier field (e.g. url, id, title). If empty, it is detected automatically.',
            "mode": 'Which set to emit: new, modified, removed, or the full summary.',
            "state_key": 'Optional identifier of the state memory.',
        },
    ),
    "transform.fuzzy_dedup": NodeSpec(
        category="transform",
        runtime_fn="fuzzy_dedup",
        optional={"field": "title", "threshold": 0.80, "merge_strategy": "keep_first"},
        label='Fuzzy Deduplication',
        output='list of items deduplicated by lexical similarity',
        choices={
            "merge_strategy": [
                {"value": "keep_first", "label": 'Keep the first and discard duplicates'},
                {"value": "merge_list", "label": 'Keep the first and attach a list of duplicates'},
                {"value": "group", "label": 'Group into clusters of duplicates'},
            ],
        },
        help={
            "field": 'Field to compare when measuring similarity (e.g. title, nombre).',
            "threshold": 'Similarity threshold between 0.0 and 1.0 (0.80 is recommended).',
            "merge_strategy": 'Strategy when duplicates are found.',
        },
    ),
    "transform.aggregate": NodeSpec(
        category="transform",
        runtime_fn="aggregate",
        optional={"field": "", "operation": "avg", "group_by": ""},
        label='Statistical Aggregator',
        output='numeric calculation or statistical summary',
        choices={
            "operation": [
                {"value": "avg", "label": 'Arithmetic mean (average)'},
                {"value": "sum", "label": 'Total sum'},
                {"value": "min", "label": 'Minimum value'},
                {"value": "max", "label": 'Maximum value'},
                {"value": "count", "label": 'Item count'},
                {"value": "median", "label": 'Median'},
                {"value": "summary", "label": 'Full summary (all metrics)'},
            ],
        },
        help={
            "field": 'Numeric field to evaluate in each object (leave empty if the items are plain numbers).',
            "operation": 'Statistical metric to compute.',
            "group_by": 'Optional field to group the statistics by.',
        },
    ),
    "transform.table": NodeSpec(
        category="transform",
        runtime_fn="format_table",
        optional={"format": "markdown", "columns": "", "headers": "", "max_rows": 100},
        label='Table Formatter',
        output='table formatted as Markdown or HTML',
        choices={
            "format": [
                {"value": "markdown", "label": 'Markdown Table'},
                {"value": "html", "label": 'HTML Table (inline styles)'},
            ],
        },
        help={
            "columns": 'Fields to include in the table, comma-separated (e.g. title,price,store).',
            "headers": 'Custom headers, comma-separated (e.g. Title,Price,Store).',
            "max_rows": 'Maximum number of rows to render.',
        },
    ),
    "transform.keywords_rake": NodeSpec(
        category="transform",
        runtime_fn="keywords_rake",
        optional={"max_keywords": 5, "min_freq": 1, "max_words": 3, "language": "es", "output_format": "list"},
        label='Keywords (RAKE)',
        output='key phrases and concepts extracted from the text',
        choices={
            "language": [
                {"value": "es", "label": 'Spanish'},
                {"value": "en", "label": 'English'},
            ],
            "output_format": [
                {"value": "list", "label": 'Structured list'},
                {"value": "text", "label": 'Comma-separated text'},
            ],
        },
        help={
            "max_keywords": 'Maximum number of key phrases to return.',
            "max_words": 'Maximum length in words of each key phrase.',
            "min_freq": 'Minimum frequency of occurrence.',
        },
    ),
    "transform.regex": NodeSpec(
        category="transform",
        runtime_fn="regex",
        required=frozenset({"pattern"}),
        optional={"action": "find_all", "replace_with": "", "field": ""},
        label='Regular Expression (Regex)',
        output='list of matches or modified text',
        choices={
            "action": [
                {"value": "find_all", "label": 'Find all matches'},
                {"value": "search", "label": 'Find first match'},
                {"value": "sub", "label": 'Replace matches'},
            ]
        },
        help={
            "pattern": 'Regular expression (e.g. [0-9]+).',
            "replace_with": "Replacement text if the action is 'sub'.",
        },
    ),
    "transform.router": NodeSpec(
        category="transform",
        runtime_fn="router",
        optional={"condition": "truthy", "field": "", "value": ""},
        label='Conditional Router',
        output='emits through the OK port if the condition is met, or through ERROR if not',
        choices={
            "condition": [
                {"value": "truthy", "label": 'True or non-empty'},
                {"value": "falsy", "label": 'False or empty'},
                {"value": "equals", "label": 'Equal to the value'},
                {"value": "not_equals", "label": 'Different from the value'},
                {"value": "contains", "label": 'Contains text'},
            ]
        },
        help={
            "field": 'Dictionary field to check (optional).',
            "value": 'Value to compare against (for equals, not_equals or contains).',
        },
    ),
    "transform.agent": NodeSpec(
        category="transform",
        runtime_fn="agent",
        required=frozenset({"prompt"}),
        optional={
            "llm": "",
            "per_item": False,
            "as_field": "",
            "tokens": DEFAULT_AGENT_TOKENS,
            "max_context": 0,
            "model": "",
            "url": "",
            "secret": "",
            "temperature": 0.7,
            "system_prompt": "",
            "json_mode": False,
        },
        label='LLM Agent',
        output='generated text; list if per_item',
        choices_from={"llm": "llms", "secret": "secrets"},
        help={
            "llm": 'LLM endpoint configuration to use (empty uses the default global endpoint).',
            "prompt": 'Instructions for the LLM.',
            "system_prompt": 'System prompt to guide the behavior (optional).',
            "json_mode": 'Force a structured JSON response.',
            "temperature": 'Creativity (0.0 very deterministic, 1.0 very creative).',
            "per_item": 'Runs one model call for each item in a list.',
            "as_field": 'If per_item is enabled, stores the response in this field of the original object instead of replacing it.',
            "tokens": 'Maximum tokens to generate in the response (max_tokens).',
            "max_context": 'Manual token limit for the context window (0 to disable; compacts the input with Luhn+ when 90% is reached).',
            "model": 'Model name (empty uses the one set in the LLM profile or global).',
            "url": 'Base URL of the OpenAI API (empty uses the one from the LLM profile or global).',
            "secret": 'Secret with the API key (if applicable).',
        },
    ),
    "transform.coder": NodeSpec(
        category="transform",
        runtime_fn="coder_agent",
        required=frozenset({"prompt"}),
        optional={
            "llm": "",
            "workspace_dir": ".",
            "tools_preset": "coding",
            "custom_tools": "",
            "max_turns": 5,
            "max_tool_chars": 4000,
            "max_context": 0,
            "compaction_engine": "blackhole",
            "output_format": "full",
            "model": "",
            "url": "",
            "secret": "",
            "temperature": 0.1,
            "system_prompt": "",
            "timeout": 120.0,
        },
        label='Coder Agent',
        output='dict with summary, turns, modified files and tool log (or text)',
        choices={
            "tools_preset": ["coding", "read_only", "all", "custom"],
            "compaction_engine": ["blackhole", "blackhole_llm", "luhn+"],
            "output_format": ["full", "text"],
        },
        choices_from={"llm": "llms", "secret": "secrets"},
        help={
            "llm": 'LLM endpoint configuration to use (empty uses the default global endpoint).',
            "prompt": 'Programming task or instructions for the agent to solve.',
            "workspace_dir": 'Root directory, relative or absolute, on which the tools will operate.',
            "tools_preset": 'Tool set: coding (read, bash, edit, write), read_only (read, grep, find, ls), all (all with web search) or custom.',
            "custom_tools": 'Tools enabled if tools_preset is custom (read, write, edit, bash, grep, find, ls, web_search).',
            "max_turns": 'Maximum number of reasoning and tool-invocation turns.',
            "max_tool_chars": 'Character limit on the output of each tool to protect the context.',
            "max_context": 'Token limit of the context window (0 to auto-detect from the server; compacts when 90% is reached).',
            "compaction_engine": "Context compaction engine: 'blackhole' (structured deterministic, no LLM), 'blackhole_llm' (mixed deterministic + synthesis with a local LLM) or 'luhn+' (frequency-based extractive).",
            "output_format": "Output format: 'full' (structured object with summary and metrics) or 'text' (text response only).",
            "model": 'Model name on the inference server (empty uses the one from the LLM profile or global).',
            "url": 'OpenAI-compatible base URL (empty uses the one from the LLM profile or global).',
            "secret": 'Name of the secret with the API key (if applicable).',
            "temperature": 'Model temperature (0.0 for maximum precision).',
            "system_prompt": 'System prompt to guide the behavior (optional).',
            "timeout": 'Maximum time in seconds per HTTP turn.',
        },
    ),
    "transform.analyzer": NodeSpec(
        category="transform",
        runtime_fn="analyzer_agent",
        optional={
            "llm": "",
            "prompt": "",
            "system_prompt": "",
            "temperature": 0.2,
            "tokens": DEFAULT_AGENT_TOKENS,
            "sanitize_secrets": True,
            "per_item": False,
            "as_field": "",
            "json_mode": False,
            "model": "",
            "url": "",
            "secret": "",
        },
        label='Analyzer Agent (SecOps / SRE)',
        output='dict with analysis, parsed, meta (or list of enriched items if per_item)',
        choices_from={"llm": "llms", "secret": "secrets"},
        help={
            "llm": 'LLM profile to use (empty uses the configured default).',
            "prompt": 'Custom analysis instruction (by default performs root cause and severity diagnosis).',
            "system_prompt": 'Specialized system role for the analysis.',
            "sanitize_secrets": 'Masks tokens, passwords and private keys before sending the telemetry to the LLM.',
            "per_item": 'Runs the analysis individually for each ticket or list item.',
            "as_field": 'Field in which to insert the resulting analysis within each object if per_item is True.',
            "json_mode": 'Forces a structured JSON response.',
            "temperature": 'Creativity (0.0 to 0.2 recommended for forensic analysis).',
            "tokens": 'Token limit for the response.',
            "model": 'Specific model name (optional).',
            "url": 'Inference endpoint URL (optional).',
            "secret": 'Name of the secret with the API key, if applicable.',
        },
    ),
    "transform.planner": NodeSpec(
        category="transform",
        runtime_fn="planner_agent",
        optional={
            "llm": "",
            "prompt": "",
            "system_prompt": "",
            "temperature": 0.2,
            "tokens": DEFAULT_AGENT_TOKENS,
            "output_format": "markdown",
            "json_mode": False,
            "model": "",
            "url": "",
            "secret": "",
        },
        label='Planner Agent (Remediation / Mitigation)',
        output='dict with plan, parsed and format',
        choices={
            "output_format": [
                {"value": "markdown", "label": 'Structured Markdown'},
                {"value": "json", "label": 'Structured JSON (steps and checks)'},
            ]
        },
        choices_from={"llm": "llms", "secret": "secrets"},
        help={
            "llm": 'LLM profile to use (empty uses the configured default).',
            "prompt": 'Plan instruction (by default generates mitigation and verification steps based on the root cause).',
            "system_prompt": 'Specialized system role for action planning.',
            "output_format": 'Plan output format: Markdown or structured JSON.',
            "temperature": 'Creativity (0.0 to 0.2 for deterministic plans).',
            "tokens": 'Token limit for the response.',
            "model": 'Specific model name (optional).',
            "url": 'Inference endpoint URL (optional).',
            "secret": 'Name of the secret with the API key, if applicable.',
        },
    ),
    "transform.clean_email": NodeSpec(
        category="transform",
        runtime_fn="clean_email_node",
        optional={
            "strip_signatures": True,
            "strip_disclaimers": True,
            "max_chars": 3000,
        },
        label='Clean Email',
        output='text or list of emails with HTML, signatures and injections removed',
        help={
            "strip_signatures": "Removes mechanical signatures (e.g. 'Sent from my iPhone', '-- ').",
            "strip_disclaimers": 'Removes legal notices and confidentiality/GDPR clauses.',
            "max_chars": 'Maximum number of characters allowed per email.',
        },
    ),
    "transform.summarize": NodeSpec(
        category="transform",
        runtime_fn="summarize_text",
        optional={
            "engine": "luhn+",
            "length": "medium",
            "format": "paragraph",
            "llm": "",
            "prompt": "",
            "field": "",
            "sentences": 0,
            "ratio": 0.0,
        },
        label='Summarize Text',
        output='summarized text',
        choices={
            "engine": [
                {"value": "luhn+", "label": 'Luhn+ (stopwords, bias and diversity)'},
                {"value": "textrank", "label": 'TextRank (graph and centrality)'},
                {"value": "luhn", "label": 'Classic Luhn (simple frequency)'},
                {"value": "llm", "label": 'LLM (generative)'},
            ],
            "length": ["short", "medium", "long", "full"],
            "format": [
                {"value": "paragraph", "label": 'Continuous paragraph'},
                {"value": "bullets", "label": 'Bulleted list (- ...)'},
            ],
        },
        choices_from={"llm": "llms"},
        show_when={"prompt": {"engine": ["llm"]}, "llm": {"engine": ["llm"]}},
        help={
            "engine": 'Summarization engine: Luhn+ (optimized), TextRank (lexical graph), classic Luhn or LLM.',
            "llm": "LLM endpoint configuration to use when the engine is 'llm' (empty uses the default global endpoint).",
            "format": "Output format: 'paragraph' (running text) or 'bullets' (bulleted list).",
            "length": 'Preset length (short, medium, long, full). Ignored if sentences > 0 or ratio > 0.',
            "field": "Specific field if the input is a dictionary (e.g. 'extract', or dot notation 'query.pages.0.extract').",
            "sentences": "Exact number of sentences to extract (0 uses 'length').",
            "ratio": 'Percentage of sentences to extract (0.0 to 1.0, e.g. 0.3 for 30%).',
        },
    ),
    "transform.threshold": NodeSpec(
        category="transform",
        runtime_fn="threshold",
        required=frozenset({"comparison", "limit"}),
        label='Compare with Limit',
        output='value or SKIPPED if not met',
        choices={
            "comparison": [
                {"value": "menor_que", "label": 'less than (number)'},
                {"value": "menor_o_igual", "label": 'less than or equal to (number)'},
                {"value": "mayor_que", "label": 'greater than (number)'},
                {"value": "mayor_o_igual", "label": 'greater than or equal to (number)'},
                {"value": "igual", "label": 'equal to (number)'},
                {"value": "distinto", "label": 'not equal to (number)'},
                {"value": "contiene", "label": 'contains text'},
                {"value": "no_contiene", "label": 'does not contain text'},
                {"value": "empieza_por", "label": 'starts with text'},
                {"value": "termina_por", "label": 'ends with text'},
                {"value": "regex", "label": 'matches regex'},
                {"value": "no_vacio", "label": 'is not empty'},
                {"value": "es_vacio", "label": 'is empty'},
            ]
        },
        help={"limit": 'If the condition is not met, the flow silently stops here.'},
    ),
    "transform.filter": NodeSpec(
        category="transform",
        runtime_fn="filter_items",
        required=frozenset({"contains"}),
        optional={"field": "", "mode": "contains"},
        label='Filter Items',
        output='filtered list',
        choices={
            "mode": [
                {"value": "contains", "label": 'Contains text'},
                {"value": "not_contains", "label": 'Does not contain text'},
                {"value": "regex", "label": 'Regular expression'},
                {"value": "gt", "label": 'Greater than (number)'},
                {"value": "lt", "label": 'Less than (number)'},
            ]
        },
        help={
            "contains": 'Value or pattern to search for.',
            "field": 'Object field to search in (optional).',
        },
    ),
    "transform.clean_html": NodeSpec(
        category="transform",
        runtime_fn="clean_html",
        optional={"url": ""},
        label='Clean HTML',
        output='plain text without tags',
    ),
    "transform.html_extract": NodeSpec(
        category="transform",
        runtime_fn="html_extract",
        required=frozenset({"selector"}),
        optional={
            "fields": "",
            "output_format": "records",
            "item_template": "",
            "header_text": "",
            "base_url": "",
            "limit": 10,
        },
        label='Extract with CSS Selector',
        output='list of dicts, texts or formatted report',
        choices={
            "output_format": [
                {"value": "records", "label": 'Records (list of dictionaries)'},
                {"value": "text", "label": 'Formatted text (using template)'},
                {"value": "list", "label": 'Simple list of values'},
            ],
        },
        help={
            "selector": "CSS selector for the container elements (e.g. 'div.new', 'article', 'h2 a').",
            "fields": "JSON or key=selector mapping of subfields (e.g. {'title': 'h2 a', 'link': 'h2 a@href'}).",
            "output_format": 'Desired output format.',
            "item_template": "Per-item template if the output is text (e.g. '### {title}\\n{link}\\n{summary}').",
            "header_text": 'Optional header at the start of the formatted text (supports {fecha}).',
            "base_url": 'Base URL to convert relative links (/noticias/...) into absolute ones.',
            "limit": 'Maximum number of items to extract (0 for all).',
        },
    ),
    "transform.cve_audit": NodeSpec(
        category="transform",
        runtime_fn="cve_audit",
        optional={
            "ecosystem": "",
            "severity_threshold": "LOW",
            "max_results": 100,
            "timeout": 25.0,
        },
        label='CVE / SBOM Audit (OSV.dev)',
        output='dict with vulnerabilities, metrics and Markdown report',
        choices={
            "ecosystem": [
                {"value": "", "label": 'Auto-detect (PURL / SBOM)'},
                {"value": "PyPI", "label": "Python (PyPI)"},
                {"value": "npm", "label": "Node.js (npm)"},
                {"value": "Maven", "label": "Java (Maven)"},
                {"value": "Go", "label": "Go"},
                {"value": "Debian", "label": "Debian (apt)"},
                {"value": "Alpine", "label": "Alpine (apk)"},
                {"value": "NuGet", "label": ".NET (NuGet)"},
                {"value": "Packagist", "label": "PHP (Composer)"},
                {"value": "crates.io", "label": "Rust (Cargo)"},
            ],
            "severity_threshold": [
                {"value": "ALL", "label": 'All severities'},
                {"value": "LOW", "label": 'Low or higher'},
                {"value": "MEDIUM", "label": 'Medium or higher'},
                {"value": "HIGH", "label": 'High or higher'},
                {"value": "CRITICAL", "label": 'Critical Only (Critical)'},
            ],
        },
        help={
            "ecosystem": 'Default package ecosystem if no PURL is specified (e.g. PyPI, npm, Maven).',
            "severity_threshold": 'Minimum severity level to include in the report (ALL, LOW, MEDIUM, HIGH, CRITICAL).',
            "max_results": 'Maximum number of vulnerabilities to include in the report.',
            "timeout": 'Maximum time in seconds for the batch query to OSV.dev.',
        },
    ),
    "transform.secret_scanner": NodeSpec(
        category="transform",
        runtime_fn="secret_scanner",
        optional={
            "mode": "detect_and_redact",
            "min_entropy": 4.3,
            "check_patterns": True,
            "check_entropy": True,
        },
        label='Secret Scanner (DLP)',
        output='dict with findings, has_secrets, count and sanitized text',
        choices={
            "mode": [
                {"value": "detect_and_redact", "label": 'Detect and Obfuscate ([REDACTED])'},
                {"value": "detect_only", "label": 'Detect Only (flag findings)'},
                {"value": "mask", "label": 'Partial mask (e.g. ab***12)'},
            ],
        },
        help={
            "mode": 'Operation mode on the detected secrets.',
            "min_entropy": 'Minimum Shannon entropy threshold (4.0 - 5.0) to alert on random strings.',
            "check_patterns": 'Check regex signatures for AWS, GitHub, Slack, JWTs, private keys, etc.',
            "check_entropy": 'Check Shannon entropy to detect unknown random keys and passwords.',
        },
    ),
    "transform.json_path": NodeSpec(
        category="transform",
        runtime_fn="json_path_extract",
        required=frozenset({"path"}),
        optional={"default": "", "flatten": True},
        label='Extract JSON Path',
        output='value or list extracted from the object',
        help={
            "path": 'Extraction path (e.g. items[0].name, spec.containers[*].image, issues.*.key).',
            "default": 'Default value if the path does not exist.',
            "flatten": 'Flatten nested lists into a single level.',
        },
    ),
    "transform.data_mapper": NodeSpec(
        category="transform",
        runtime_fn="data_mapper",
        required=frozenset({"mapping"}),
        optional={"per_item": True},
        label='Data Mapper',
        output='dict or list with the new projected structure',
        help={
            "mapping": "JSON or key=path mapping (e.g. {'id': 'metadata.name', 'estado': 'status.phase'}).",
            "per_item": 'If the input is a list, applies the mapping to each element individually.',
        },
    ),
    "transform.approval": NodeSpec(
        category="transform",
        runtime_fn="human_approval",
        optional={
            "prompt_message": 'Do you want to authorize the execution of this step?',
            "on_reject": "skip",
            "auto_approve": False,
        },
        label='Human Approval (Approval Gate)',
        output='same data if approved, or SKIPPED if rejected',
        choices={
            "on_reject": [
                {"value": "skip", "label": 'Skip following nodes (SKIPPED)'},
                {"value": "error", "label": 'Stop with Error (Abort flow)'},
            ],
        },
        help={
            "prompt_message": 'Question or warning message to show to the operator in the console.',
            "on_reject": 'Action if the user rejects the approval or there is no interactive terminal (skip/error).',
            "auto_approve": 'If enabled, approves automatically without asking (can be overridden with --yes / --non-interactive).',
        },
    ),
    "transform.merge": NodeSpec(
        category="transform",
        runtime_fn="merge",
        label='Merge Inputs',
        output='list with the results of the inputs',
    ),
    "transform.script": NodeSpec(
        category="transform",
        runtime_fn="script",
        required=frozenset({"code"}),
        label='Python Code',
        code_field="code",
        output='the value returned by your code',
    ),
    # -- Salidas ------------------------------------------------------------
    "output.file": NodeSpec(
        category="output",
        runtime_fn="file",
        optional={
            "filename": "salida.md",
            "directory": "",
            "content": "inputs",
            "message": "",
            "mode": "sobrescribir",
            "format": "raw",
        },
        label='Save to File',
        output='path of the written file',
        choices={
            "content": [
                {"value": "inputs", "label": 'Content of the inputs'},
                {"value": "static", "label": 'Fixed message'},
            ],
            "mode": [
                {"value": "sobrescribir", "label": 'Overwrite'},
                {"value": 'añadir', "label": 'Append to the end'},
                {"value": "numerar", "label": 'Number if it already exists'},
            ],
            "format": [
                {"value": "raw", "label": 'Text / Raw'},
                {"value": "json_pretty", "label": 'Formatted JSON'},
            ],
        },
        show_when={"message": {"content": ["static"]}},
        help={"filename": 'Supports {fecha}, {hora}, {timestamp}.'},
    ),
    "output.evidence_vault": NodeSpec(
        category="output",
        runtime_fn="evidence_vault",
        optional={
            "directory": "data/vault",
            "compress": True,
            "filename_pattern": "evidence_{incident_id}_{hash}",
            "include_metadata": True,
        },
        label='Evidence Vault',
        output='dict with vault_path, sha256, incident_id, timestamp and telemetry',
        help={
            "directory": 'Secure directory where the evidence is stored.',
            "compress": 'Compresses the data in gzip format (.json.gz) to save space and optimize I/O.',
            "filename_pattern": 'File name pattern. Supports {incident_id}, {hash} and {timestamp}.',
            "include_metadata": 'Generates a companion .meta.json manifest with a SHA-256 cryptographic hash and forensic information.',
        },
    ),
    "output.jira_issue": NodeSpec(
        category="output",
        runtime_fn="jira_issue_output",
        required=frozenset({"instance_url"}),
        optional={
            "action": "create",
            "issue_key": "",
            "project_key": "",
            "email": "",
            "secret": "",
            "issue_type": "Task",
            "summary": "",
            "description": "",
            "comment": "",
            "priority": "",
            "status_transition": "",
            "labels": "",
            "timeout": 20.0,
        },
        label='Jira Operations (Create / Comment / Modify)',
        output='dict with the result of the operation (or list of results if the input is a batch)',
        choices={
            "action": [
                {"value": "create", "label": 'Create new ticket'},
                {"value": "add_comment", "label": 'Add comment to existing ticket'},
                {"value": "update", "label": 'Update fields / priority / labels'},
                {"value": "transition", "label": 'Change status (transition)'},
            ]
        },
        choices_from={"secret": "secrets"},
        show_when={
            "project_key": {"action": ["create"]},
            "issue_type": {"action": ["create"]},
            "issue_key": {"action": ["add_comment", "update", "transition"]},
            "comment": {"action": ["add_comment", "update", "transition"]},
            "status_transition": {"action": ["transition"]},
        },
        help={
            "instance_url": 'Base URL of the Jira Cloud instance (e.g. https://your-company.atlassian.net).',
            "action": 'Action to perform: create ticket, add comment, update fields or transition status.',
            "issue_key": "Key of the ticket to modify (e.g. SEC-101). Optional if the input already contains the 'key' field.",
            "project_key": 'Project key for creation (e.g. SEC, OPS).',
            "email": 'Atlassian user email.',
            "secret": 'Name of the secret holding the Atlassian API Token.',
            "comment": 'Comment text (if left empty, the analysis/plan of the previous node is injected).',
            "status_transition": "Name of the target status (e.g. 'In Progress', 'Done', 'Triaged').",
            "priority": 'New or initial priority (Lowest, Low, Medium, High, Highest).',
            "labels": 'Comma-separated labels (e.g. dedalo,secops,autofix).',
        },
    ),
    "output.confluence_page": NodeSpec(
        category="output",
        runtime_fn="confluence_page_output",
        required=frozenset({"instance_url"}),
        optional={
            "page_id": "",
            "space_key": "",
            "email": "",
            "secret": "",
            "title": "",
            "content": "",
            "mode": "create_or_update",
            "timeout": 20.0,
        },
        label='Publish or Update in Confluence',
        output='dict with id, title, space, url, version and action',
        choices={
            "mode": [
                {"value": "create_or_update", "label": 'Create new or update if it already exists'},
                {"value": "append", "label": 'Append to the end of the existing content'},
                {"value": "create_only", "label": 'Always create new'},
            ]
        },
        choices_from={"secret": "secrets"},
        help={
            "instance_url": 'Confluence Cloud URL (e.g. https://your-company.atlassian.net/wiki).',
            "page_id": 'Numeric ID of the page to update (e.g. 20001). Optional if it comes from input.confluence_pages or is looked up by space_key and title.',
            "space_key": 'Key of the space where to save or look up the page (e.g. SEC, OPS).',
            "email": 'Atlassian user email.',
            "secret": 'Name of the secret holding the Atlassian API Token.',
            "title": 'Page title (if empty, it is taken from the page read or the current date).',
            "content": 'Document body (if empty, the content of the previous node is used).',
            "mode": 'Save mode: create/update by full version or append to the end (append).',
        },
    ),
    "output.git": NodeSpec(
        category="output",
        runtime_fn="git_commit_push",
        optional={
            "repo_path": ".",
            "message": "update: automated commit from dedalo",
            "files": ".",
            "push": False,
            "branch": "main",
            "remote": "origin",
            "author_name": "Dedalo Bot",
            "author_email": "dedalo@local",
            "timeout": 30.0,
        },
        label='Publish to Git',
        output='dict with commit_hash, message and push status',
        help={
            "repo_path": 'Local path to the Git repository.',
            "message": 'Commit message to generate.',
            "files": 'Paths or file patterns to add (default . for the whole tree).',
            "push": 'If true, runs git push to the configured remote repository.',
            "branch": 'Remote branch to push to (default main).',
            "remote": 'Name of the configured remote (default origin).',
            "author_name": 'Commit author name.',
            "author_email": 'Commit author email.',
        },
    ),
    "output.sqlite_exec": NodeSpec(
        category="output",
        runtime_fn="sqlite_exec",
        required=frozenset({"query"}),
        optional={"path": "data/store.db"},
        label='Run on SQLite (INSERT/UPDATE)',
        output='number of affected rows',
        help={
            "query": 'SQL statement (INSERT INTO ... VALUES (:field1, :field2)).',
            "path": 'Path to the SQLite database.',
        },
    ),
    "output.email_smtp": NodeSpec(
        category="output",
        runtime_fn="email_smtp",
        required=frozenset({"to_addr"}),
        optional={
            "host": "smtp.gmail.com",
            "port": 587,
            "user": "",
            "secret": "",
            "from_addr": "",
            "subject": 'Dédalo Notice',
            "use_tls": True,
            "is_html": False,
        },
        label='Send Email (SMTP)',
        output='send confirmation',
        choices_from={"secret": "secrets"},
        help={
            "to_addr": 'Recipient address.',
            "secret": 'Secret containing the password or App Password.',
            "subject": 'Email subject.',
        },
    ),
    "output.ntfy": NodeSpec(
        category="output",
        runtime_fn="ntfy",
        required=frozenset({"topic"}),
        optional={
            "server": "https://ntfy.sh",
            "title": 'Dédalo',
            "priority": "3",
            "tags": "robot",
            "secret": "",
        },
        label='Push Notification (ntfy.sh)',
        output='send confirmation',
        choices={
            "priority": [
                {"value": "1", "label": '1 - Minimal'},
                {"value": "2", "label": '2 - Low'},
                {"value": "3", "label": "3 - Normal"},
                {"value": "4", "label": '4 - High'},
                {"value": "5", "label": '5 - Urgent'},
            ]
        },
        choices_from={"secret": "secrets"},
        help={
            "topic": 'Name of the ntfy channel/topic.',
            "tags": 'Comma-separated icons or tags (e.g. warning,skull,bell).',
        },
    ),
    "output.discord": NodeSpec(
        category="output",
        runtime_fn="discord",
        optional={
            "webhook_url": "",
            "secret": "",
            "username": 'Dédalo',
            "title": "",
            "content": "",
            "use_embed": False,
        },
        label="Discord Webhook",
        output='send confirmation',
        choices_from={"secret": "secrets"},
        help={
            "webhook_url": 'Discord webhook URL.',
            "secret": 'Or select a secret holding the URL.',
            "content": 'Message (by default the content of the inputs).',
        },
    ),
    "output.slack": NodeSpec(
        category="output",
        runtime_fn="slack",
        optional={
            "webhook_url": "",
            "secret": "",
            "channel": "",
            "text": "",
        },
        label="Slack Webhook",
        output='send confirmation',
        choices_from={"secret": "secrets"},
        help={
            "webhook_url": 'Slack Incoming Webhook URL.',
            "secret": 'Or select a secret holding the URL.',
            "text": 'Message to send (supports {entrada}).',
        },
    ),
    "output.teams": NodeSpec(
        category="output",
        runtime_fn="teams_webhook",
        optional={
            "webhook_url": "",
            "secret": "",
            "title": "",
            "text": "",
            "theme_color": "0076D7",
            "summary": "",
            "button_text": "",
            "button_url": "",
            "timeout": 15.0,
        },
        label="Microsoft Teams Webhook",
        output='submission confirmation',
        choices_from={"secret": "secrets"},
        help={
            "webhook_url": 'Microsoft Teams / Workflows Incoming Webhook URL.',
            "secret": 'Or select a secret holding the Webhook URL.',
            "title": 'Card title.',
            "text": 'Message body (by default the content of the inputs).',
            "theme_color": 'Hexadecimal color of the bar (e.g. 0076D7 for blue, D83B01 for red alert).',
            "button_text": "Text for an optional link button (e.g. 'View in Jira').",
            "button_url": 'URL to open when the button is clicked.',
        },
    ),
    "output.mqtt": NodeSpec(
        category="output",
        runtime_fn="mqtt_publish",
        required=frozenset({"topic"}),
        optional={
            "broker": "localhost",
            "port": 1883,
            "payload": "{entrada}",
            "retain": False,
            "qos": 0,
            "username": "",
            "secret": "",
            "timeout": 5.0,
        },
        label='MQTT Publish',
        output='publication confirmation',
        choices_from={"secret": "secrets"},
        help={
            "topic": 'MQTT topic to publish to.',
            "payload": 'Message content (supports {entrada}).',
            "broker": 'Broker address (e.g. localhost or 192.168.1.50).',
            "secret": 'Secret with the password if the broker requires auth.',
        },
    ),
    "output.command": NodeSpec(
        category="output",
        runtime_fn="command_action",
        required=frozenset({"cmd"}),
        optional={"timeout": 15.0, "cwd": "", "sandbox": ""},
        label='Run Action (Shell)',
        output='dict with stdout, stderr, exit_code',
        help={
            "cmd": 'Action command to run in the shell (e.g. systemctl restart service).',
            "sandbox": 'Isolation with bubblewrap: sees only /usr, /etc and this working directory; data/ hidden.',
        },
        choices={
            "sandbox": [
                {"value": "", "label": 'According to DEDALO_SANDBOX (default)'},
                {"value": "on", "label": 'Isolate with bubblewrap (fails if unavailable)'},
                {"value": "off", "label": 'Not isolated'},
            ],
        },
    ),
    "output.rest_webhook": NodeSpec(
        category="output",
        runtime_fn="rest_webhook",
        required=frozenset({"url"}),
        optional={
            "method": "POST",
            "body": "",
            "headers": "",
            "secret": "",
            "auth_header": "Authorization",
        },
        label='REST Webhook',
        output='submission confirmation',
        choices={"method": ["POST", "PUT", "PATCH"]},
        choices_from={"secret": "secrets"},
        help={
            "url": 'Webhook destination (Discord, Slack, n8n, etc.).',
            "body": 'JSON to send. Use {entrada} to inject the data.',
        },
    ),
    "output.telegram": NodeSpec(
        category="output",
        runtime_fn="telegram",
        optional={
            "chat_id": "",
            "token_secret": "telegram-token",
            "parse_mode": "HTML",
            "silent": False,
        },
        label="Telegram",
        output='submission confirmation',
        choices={
            "parse_mode": [
                {"value": "HTML", "label": "HTML"},
                {"value": "Markdown", "label": "Markdown"},
                {"value": "texto", "label": 'Plain text'},
            ]
        },
        choices_from={"token_secret": "secrets"},
        help={
            "token_secret": 'Name of the secret containing the bot token.',
            "chat_id": 'ID of the receiving chat or channel.',
        },
    ),
    "output.healthcheck": NodeSpec(
        category="output",
        runtime_fn="healthcheck_ping",
        required=frozenset({"url"}),
        optional={
            "method": "GET",
            "send_status": "auto",
            "send_payload": False,
        },
        label='Monitoring Ping (Heartbeat)',
        output='HTTP ping confirmation (dict)',
        choices={
            "method": [
                {"value": "GET", "label": "GET"},
                {"value": "POST", "label": "POST"},
            ],
            "send_status": [
                {"value": "auto", "label": 'Automatic (success if previous nodes OK, failure if error)'},
                {"value": "success", "label": 'Force success signal'},
                {"value": "fail", "label": 'Force failure signal'},
            ],
        },
        help={
            "url": 'Ping URL (Healthchecks.io, Uptime Kuma push or similar).',
            "method": 'HTTP method for the heartbeat request.',
            "send_status": 'Status to report to the monitor.',
            "send_payload": 'If True, sends the payload or data excerpt in the POST body.',
        },
    ),
    "output.alert": NodeSpec(
        category="output",
        runtime_fn="alert",
        optional={"text": "", "urgency": "info"},
        label='Log / Alert',
        output='logged line',
        choices={
            "urgency": [
                {"value": "info", "label": 'Informational'},
                {"value": "alerta", "label": 'Alert'},
                {"value": "error", "label": 'Critical'},
            ]
        },
    ),
}


def _topological_order(graph: Graph) -> list[Node] | None:
    """Algoritmo de Kahn estable para obtener el orden de ejecución de los nodos."""
    remaining = {n.id: n for n in graph.nodes}
    indegree = {n.id: 0 for n in graph.nodes}
    for e in graph.edges:
        if e.target in indegree:
            indegree[e.target] += 1

    ordered: list[Node] = []
    while remaining:
        ready = [n for n in graph.nodes if n.id in remaining and indegree[n.id] == 0]
        if not ready:
            return None  # Ciclo detectado
        chosen = ready[0]
        ordered.append(chosen)
        del remaining[chosen.id]
        for e in graph.edges:
            if e.source == chosen.id and e.target in indegree:
                indegree[e.target] -= 1
    return ordered


def get_upstream_nodes(graph: Graph, target_node_id: str) -> list[Node]:
    """Devuelve los nodos necesarios para ejecutar target_node_id en orden topológico."""
    full_order = _topological_order(graph)
    if not full_order:
        return []

    needed: set[str] = {target_node_id}
    queue = [target_node_id]
    while queue:
        curr = queue.pop(0)
        for e in graph.incoming(curr):
            if e.source not in needed:
                needed.add(e.source)
                queue.append(e.source)

    return [n for n in full_order if n.id in needed]


def get_node_inputs_from_results(graph: Graph, node_id: str, results: dict[str, Any]) -> list[Any]:
    """Obtiene las entradas de un nodo a partir de los resultados previos de sus fuentes."""
    incoming = graph.incoming(node_id)
    inputs_list = []
    for edge in incoming:
        if edge.source in results:
            inputs_list.append(results[edge.source])
    return inputs_list


def validate(graph: Graph) -> list[str]:
    """Comprueba todos los problemas del grafo antes de compilarlo."""
    errors: list[str] = []
    node_ids = {n.id for n in graph.nodes}

    seen_ids: set[str] = set()
    for n in graph.nodes:
        if n.id in seen_ids:
            errors.append(tr('duplicate node id: "{id}"', id=n.id))
        seen_ids.add(n.id)
        if not _ID_RE.match(n.id):
            errors.append(
                tr('invalid node id: "{id}" -- must be a valid Python identifier', id=n.id)
            )
        if n.type not in NODE_TYPES:
            errors.append(tr('"{id}": unknown node type "{type}"', id=n.id, type=n.type))
            continue
        spec = NODE_TYPES[n.type]
        missing = spec.required - set(n.config)
        if missing:
            errors.append(
                tr('"{id}" ({type}): missing required configuration: {missing}', id=n.id, type=n.type, missing=', '.join(sorted(missing)))
            )

    for e in graph.edges:
        if e.source not in node_ids:
            errors.append(tr('edge with unknown source: "{source}"', source=e.source))
        if e.target not in node_ids:
            errors.append(tr('edge with unknown target: "{target}"', target=e.target))
        if e.port not in PORTS:
            errors.append(tr('invalid edge port: "{port}" (only {PORTS})', port=e.port, PORTS=PORTS))

    if _topological_order(graph) is None:
        errors.append(tr('the graph contains a cycle -- it cannot be executed in any order'))

    # Validación de referencias en nodos script (entrada["id"])
    for n in graph.nodes:
        spec = NODE_TYPES.get(n.type)
        if not spec or not spec.code_field:
            continue
        sources = {e.source for e in graph.incoming(n.id)}
        for ref in _ENTRADA_REF_RE.findall(n.config.get(spec.code_field, "") or ""):
            if ref and ref not in sources:
                errors.append(
                    tr('"{id}": the code reads entrada["{ref}"], but that node is not connected. Connected: {ninguno}', id=n.id, ref=ref, ninguno=', '.join(sorted(sources)) or tr('none'))
                )

    for n in graph.nodes:
        incoming = graph.incoming(n.id)
        if len({e.source for e in incoming}) > 1:
            policy = n.config.get("fan_in")
            if policy not in FAN_IN_POLICIES:
                errors.append(
                    tr('"{id}": has several incoming edges and requires config.fan_in = \'all\' or \'any\'', id=n.id)
                )

    return errors


def _guard_expression(graph: Graph, node: Node) -> str | None:
    """Expresión condicional para determinar si el nodo debe ejecutarse."""
    incoming = graph.incoming(node.id)
    if not incoming:
        return None

    def term(edge: Edge) -> str:
        attr = "ok" if edge.port == "ok" else "failed"
        return f"r_{edge.source}.{attr}"

    if len(incoming) == 1:
        return term(incoming[0])

    policy = node.config.get("fan_in", "all")
    joiner = " and " if policy == "all" else " or "
    return "(" + joiner.join(term(e) for e in incoming) + ")"


def _inputs_expression(graph: Graph, node: Node) -> str:
    incoming = graph.incoming(node.id)
    return "[" + ", ".join(f"r_{e.source}" for e in incoming) + "]"


def _render_config_kwargs(node: Node, spec: NodeSpec) -> str:
    values = {**spec.optional, **node.config}
    values.pop("fan_in", None)
    values.pop(spec.code_field, None)
    return ", ".join(f"{k}={v!r}" for k, v in sorted(values.items()))


def _render_code_node(graph: Graph, node: Node, spec: NodeSpec) -> list[str]:
    body = textwrap.dedent(node.config.get(spec.code_field, "") or "").strip("\n")
    sources = [e.source for e in graph.incoming(node.id)]
    lines = [f"def node_{node.id}(ctx, inputs):", '    def _cuerpo(entrada, ctx):']
    lines += [f"        {line}" if line.strip() else "" for line in body.splitlines()] or [
        "        return None"
    ]
    lines.append(f"    return runtime.script(ctx, inputs, _cuerpo, sources={sources!r})")
    return lines


def compile(graph: Graph) -> str:
    """Compila el grafo a código fuente Python `run(ctx)`."""
    errors = validate(graph)
    if errors:
        raise GraphError(errors)

    order = _topological_order(graph)
    assert order is not None

    lines: list[str] = [
        '# GENERATED BY DÉDALO -- do not edit by hand, it is regenerated from the graph.',
        f"# dedalo.VERSION = {VERSION}",
        "from dedalo import runtime",
        "from dedalo.runtime import SKIPPED",
        "",
        "",
    ]

    for node in order:
        spec = NODE_TYPES[node.type]
        if spec.code_field:
            lines += _render_code_node(graph, node, spec)
        else:
            kwargs = _render_config_kwargs(node, spec)
            call = f"runtime.{spec.runtime_fn}(ctx, inputs" + (
                f", {kwargs})" if kwargs else ")"
            )
            lines.append(f"def node_{node.id}(ctx, inputs):")
            lines.append(f"    return {call}")
        lines.append("")
        lines.append("")

    lines.append("def run(ctx):")
    for node in order:
        guard = _guard_expression(graph, node)
        inputs_expr = _inputs_expression(graph, node)
        call = f"node_{node.id}(ctx, {inputs_expr})"
        if guard is None:
            lines.append(f"    r_{node.id} = {call}")
        else:
            lines.append(f"    r_{node.id} = {call} if {guard} else SKIPPED")

    results_dict = "{" + ", ".join(f"{n.id!r}: r_{n.id}" for n in order) + "}"
    lines.append(f"    return runtime.summarize({results_dict}, ctx)")
    lines.append("")

    source = "\n".join(lines)
    try:
        ast.parse(source)
        _compile_source(source, "<dedalo>", "exec")
    except SyntaxError as exc:
        raise GraphError([tr('the generated code is not valid Python: {exc}', exc=exc)]) from exc

    return source


def _extract_runtime_tree_shaken(graph: Graph) -> tuple[str, list[str]]:
    """Extrae las funciones y clases del runtime estrictamente necesarias para el grafo, sin dependencias de Dédalo."""
    from pathlib import Path

    runtime_file = Path(__file__).parent / "runtime.py"
    runtime_code = runtime_file.read_text(encoding="utf-8")
    runtime_lines = runtime_code.splitlines()
    tree = ast.parse(runtime_code)

    definitions: dict[str, Any] = {}
    order: list[tuple[str, Any]] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            definitions[node.name] = node
            order.append((node.name, node))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
                    order.append((target.id, node))
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                definitions[node.target.id] = node
                order.append((node.target.id, node))

    def get_referenced_names(n: Any) -> set[str]:
        return {child.id for child in ast.walk(n) if isinstance(child, ast.Name)}

    root_fns = [NODE_TYPES[n.type].runtime_fn for n in graph.nodes if n.type in NODE_TYPES]
    root_fns += ["summarize", "compact_memory", "NodeResult", "Inputs", "SKIPPED"]

    needed = set(root_fns)
    queue = list(root_fns)
    while queue:
        curr = queue.pop(0)
        if curr in definitions:
            refs = get_referenced_names(definitions[curr])
            for ref in refs:
                if ref in definitions and ref not in needed:
                    needed.add(ref)
                    queue.append(ref)

    # Identificar dependencias externas reales
    external_pkgs: list[str] = []
    all_needed_nodes = [definitions[name] for name in needed if name in definitions]
    all_refs: set[str] = set()
    for n in all_needed_nodes:
        all_refs.update(get_referenced_names(n))

    if "httpx" in all_refs or any(
        n.type in (
            "input.http_call",
            "input.web_read",
            "input.web_search",
            "input.jira_issues",
            "input.confluence_pages",
            "input.whois_rdap",
            "input.dns_record",
            "transform.agent",
            "transform.coder",
            "transform.analyzer",
            "transform.planner",
            "transform.cve_audit",
            "output.jira_issue",
            "output.confluence_page",
            "output.teams",
        )
        for n in graph.nodes
    ):
        external_pkgs.append("httpx>=0.27.0")

    uses_curl = any(n.config.get("impersonate") or n.config.get("antibot") for n in graph.nodes)
    if uses_curl or "curl_cffi" in all_refs:
        external_pkgs.append("curl_cffi>=0.7.0")

    seen_nodes: set[int] = set()
    chunks: list[str] = []
    for name, node in order:
        if name in needed and id(node) not in seen_nodes:
            seen_nodes.add(id(node))
            start_line = (
                min(d.lineno for d in node.decorator_list)
                if getattr(node, "decorator_list", None)
                else node.lineno
            )
            chunks.append("\n".join(runtime_lines[start_line - 1 : node.end_lineno]))

    return "\n\n".join(chunks), sorted(set(external_pkgs))


def _analyze_standalone_requirements(graph: Graph) -> dict[str, Any]:
    """Inspecciona los nodos del grafo para identificar secretos y uso de LLM."""
    required_secrets: set[str] = set()
    uses_llm = False
    has_webhook = False

    for node in graph.nodes:
        cfg = node.config or {}
        for key in ("secret", "token_secret"):
            val = cfg.get(key)
            if val and isinstance(val, str) and val.strip():
                required_secrets.add(val.strip())

        if node.type in ("transform.agent", "transform.coder", "transform.analyzer", "transform.planner") or "llm" in cfg:
            uses_llm = True

        if node.type == "input.webhook":
            has_webhook = True

    return {
        "required_secrets": sorted(required_secrets),
        "uses_llm": uses_llm,
        "has_webhook": has_webhook,
    }


def compile_standalone(graph: Graph) -> str:
    """Compila el grafo a un script Python 100% autónomo sin ninguna dependencia del paquete Dédalo."""
    errors = validate(graph)
    if errors:
        raise GraphError(errors)

    order = _topological_order(graph)
    assert order is not None

    reqs = _analyze_standalone_requirements(graph)
    shaken_runtime, external_deps = _extract_runtime_tree_shaken(graph)
    flow_name = graph.name or "flujo"

    lines: list[str] = [
        "#!/usr/bin/env python3",
        "# /// script",
        '# requires-python = ">=3.11"',
        "# dependencies = [",
    ]
    for dep in external_deps:
        lines.append(f'#     "{dep}",')
    lines += [
        "# ]",
        "# ///",
        '"""Dédalo Flow: {flow_name}'.format(flow_name=flow_name),
        'Fully standalone Python script generated by Dédalo.',
        "",
        'Usage:',
        f"    python3 {flow_name}.py --help",
        f"    python3 {flow_name}.py --create-venv",
        '    python3 {flow_name}.py [options]'.format(flow_name=flow_name),
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import argparse",
        "import csv",
        "import ctypes",
        "import datetime",
        "import difflib",
        "import email",
        "import email.header",
        "import gc",
        "import gzip",
        "import hashlib",
        "import html",
        "import imaplib",
        "import json",
        "import math",
        "import os",
        "import random",
        "import re",
        "import select",
        "import shutil",
        "import smtplib",
        "import socket",
        "import ssl",
        "import statistics",
        "import subprocess",
        "import sys",
        "import time",
        "import urllib.parse",
        "import xml.etree.ElementTree as ET",
        "from collections import Counter, defaultdict",
        "from dataclasses import dataclass, field",
        "from email.mime.multipart import MIMEMultipart",
        "from email.mime.text import MIMEText",
        "from html.parser import HTMLParser",
        "from pathlib import Path",
        "from typing import Any, Callable",
        "",
    ]

    # Import condicional de librerías externas
    if "httpx>=0.27.0" in external_deps:
        lines += [
            "try:",
            "    import httpx",
            "except ImportError:",
            "    httpx = None",
            "",
        ]

    if "curl_cffi>=0.7.0" in external_deps:
        lines += [
            "try:",
            "    from curl_cffi import requests as curl_requests",
            "except ImportError:",
            "    curl_requests = None",
            "",
        ]

    # Verificación de dependencias en arranque
    if external_deps:
        dep_checks = []
        if "httpx>=0.27.0" in external_deps:
            dep_checks.append('if httpx is None: missing.append("httpx>=0.27.0")')
        if "curl_cffi>=0.7.0" in external_deps:
            dep_checks.append('if curl_requests is None: missing.append("curl_cffi>=0.7.0")')

        lines += [
            "def _verify_dependencies() -> None:",
            "    missing = []",
            *[f"    {dc}" for dc in dep_checks],
            '    if missing and "--create-venv" not in sys.argv and "-h" not in sys.argv and "--help" not in sys.argv:',
            '        print(f"Error: Missing dependencies in the active environment: {\', \'.join(missing)}\\n"',
            '              f"Run this script with \'--create-venv\' to set up the environment automatically:\\n"',
            '              f"    {sys.executable} {sys.argv[0]} --create-venv\\n", file=sys.stderr)',
            "        sys.exit(1)",
            "_verify_dependencies()",
            "",
        ]

    # Shims para Secretos, LLMs y Contexto de ejecución
    lines += [
        "@dataclass",
        "class FlowContext:",
        "    flow_name: str",
        "    started_at: float = field(default_factory=time.time)",
        "    payload: Any = None",
        "    headers: dict[str, str] = field(default_factory=dict)",
        "    auto_approve: bool = False",
        "",
        "def tr(msg, /, **params):",
        "    return msg.format(**params) if params else msg",
        "",
        "class _SecretsShim:",
        "    @staticmethod",
        "    def get(name: str) -> str | None:",
        "        if not name:",
        "            return None",
        '        return os.environ.get("DEDALO_SECRET_" + name.upper().replace("-", "_")) or None',
        "",
        "secrets = _SecretsShim()",
        "",
        "class _LLMsShim:",
        "    @staticmethod",
        "    def resolve(name: str = '', overrides: dict[str, Any] | None = None) -> dict[str, Any]:",
        "        o = overrides or {}",
        "        sec_name = str(o.get('secret') or '')",
        "        api_key = secrets.get(sec_name) if sec_name else ''",
        "        if not api_key:",
        '            api_key = os.environ.get("DEDALO_LLM_KEY") or os.environ.get("OPENAI_API_KEY") or ""',
        "        return {",
        "            'name': name or 'default',",
        "            'url': (o.get('url') or os.environ.get('DEDALO_LLM_URL') or os.environ.get('OPENAI_BASE_URL') or 'http://localhost:11434/v1').rstrip('/'),",
        "            'model': o.get('model') or os.environ.get('DEDALO_LLM_MODEL') or 'llama3',",
        "            'api_key': api_key,",
        "            'secret': sec_name,",
        "            'temperature': float(o.get('temperature') if o.get('temperature') is not None else os.environ.get('DEDALO_LLM_TEMPERATURE', '0.7')),",
        "            'max_context': int(o.get('max_context') or 0),",
        "        }",
        "",
        "llms = _LLMsShim()",
        "",
        "class _SandboxShim:",
        "    SandboxUnavailable = RuntimeError",
        "    @staticmethod",
        "    def run_shell(cmd, *, kind='command', timeout=15.0, cwd=None, mode=''):",
        "        deny = ('DEDALO_SECRET_', 'DEDALO_MASTER_KEY', 'DEDALO_LLM_KEY', 'DEDALO_TELEGRAM_')",
        "        env = {k: v for k, v in os.environ.items() if not k.startswith(deny)}",
        "        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)",
        "",
        "sbx = _SandboxShim()",
        "",
        "# ==========================================================================",
        '# Embedded runtime',
        "# ==========================================================================",
        shaken_runtime,
        "",
        "runtime = sys.modules[__name__]",
        "",
        "",
    ]

    # Renderizado de los nodos del DAG
    for node in order:
        spec = NODE_TYPES[node.type]
        if spec.code_field:
            lines += _render_code_node(graph, node, spec)
        else:
            kwargs = _render_config_kwargs(node, spec)
            call = f"runtime.{spec.runtime_fn}(ctx, inputs" + (
                f", {kwargs})" if kwargs else ")"
            )
            lines.append(f"def node_{node.id}(ctx, inputs):")
            lines.append(f"    return {call}")
        lines.append("")
        lines.append("")

    # Función run(ctx)
    lines.append("def run(ctx):")
    for node in order:
        guard = _guard_expression(graph, node)
        inputs_expr = _inputs_expression(graph, node)
        call = f"node_{node.id}(ctx, {inputs_expr})"
        if guard is None:
            lines.append(f"    r_{node.id} = {call}")
        else:
            lines.append(f"    r_{node.id} = {call} if {guard} else SKIPPED")

    results_dict = "{" + ", ".join(f"{n.id!r}: r_{n.id}" for n in order) + "}"
    lines.append(f"    return runtime.summarize({results_dict}, ctx)")
    lines.append("")
    lines.append("")

    # Función setup_virtual_environment
    lines += [
        'def setup_virtual_environment(venv_dir: str = ".venv") -> None:',
        '    """Creates a virtual environment and installs the required dependencies."""',
        "    import subprocess",
        "    import venv",
        "",
        "    target = Path(venv_dir).resolve()",
        '    print(f"[*] Setting up virtual environment at: {target}")',
        "    if not target.exists():",
        '        print("    -> Creating virtual environment...")',
        "        venv.EnvBuilder(with_pip=True).create(target)",
        "    else:",
        '        print("    -> Existing virtual environment detected.")',
        "",
        '    is_win = sys.platform == "win32"',
        '    pip_exe = target / ("Scripts" if is_win else "bin") / ("pip.exe" if is_win else "pip")',
        '    python_exe = target / ("Scripts" if is_win else "bin") / ("python.exe" if is_win else "python")',
        "",
        "    if not pip_exe.exists():",
        '        print(f"Error: pip not found at {pip_exe}", file=sys.stderr)',
        "        sys.exit(1)",
        "",
        '    print("    -> Upgrading pip...")',
        "    try:",
        '        subprocess.run([str(pip_exe), "install", "--upgrade", "pip"], check=True)',
        "    except Exception as exc:",
        '        print(f"Warning while updating pip: {exc}", file=sys.stderr)',
        "",
        f"    deps = {external_deps!r}",
        "    if deps:",
        '        print(f"    -> Installing dependencies: {\', \'.join(deps)}...")',
        "        try:",
        '            subprocess.run([str(pip_exe), "install"] + deps, check=True)',
        "        except Exception as exc:",
        '            print(f"Error instalando dependencias: {exc}", file=sys.stderr)',
        "            sys.exit(1)",
        "    else:",
        '        print("    -> This flow does not require any additional external dependencies.")',
        "",
        '    print("[✓] Virtual environment configured successfully.")',
        '    print("    To run this flow, use:")',
        '    print(f"    {python_exe} {sys.argv[0]} [options]")',
        "",
        "",
    ]

    # Función parse_args
    lines += [
        "def parse_args():",
        "    parser = argparse.ArgumentParser(",
        f"        description={flow_name!r},",
        "        formatter_class=argparse.RawDescriptionHelpFormatter,",
        "    )",
        "    parser.add_argument(",
        '        "--create-venv",',
        '        action="store_true",',
        '        help="Creates a local .venv virtual environment and installs the required dependencies.",',
        "    )",
        "    parser.add_argument(",
        '        "--json",',
        '        action="store_true",',
        '        help="Shows the full result of the flow as structured JSON.",',
        "    )",
        "    parser.add_argument(",
        '        "--verbose",',
        '        "-v",',
        '        action="store_true",',
        '        help="Shows detailed information about the execution of each node.",',
        "    )",
        "    parser.add_argument(",
        '        "--input",',
        '        "-i",',
        '        dest="input_data",',
        '        help="Input payload for the flow (text or JSON).",',
        "    )",
        "    parser.add_argument(",
        '        "--input-file",',
        '        "-f",',
        '        dest="input_file",',
        '        help="Path to a file whose contents will be used as input for the flow.",',
        "    )",
        "    parser.add_argument(",
        '        "--yes",',
        '        "-y",',
        '        "--non-interactive",',
        '        dest="auto_approve",',
        '        action="store_true",',
        '        help="Automatically approves steps that require human confirmation (without prompting interactively).",',
        "    )",
    ]

    # Argumentos de LLM si se detecta uso
    if reqs["uses_llm"]:
        lines += [
            "",
            '    llm_grp = parser.add_argument_group("Language Model (LLM) Configuration")',
            "    llm_grp.add_argument(",
            '        "--llm-url",',
            '        default=os.environ.get("DEDALO_LLM_URL") or os.environ.get("OPENAI_BASE_URL") or "http://localhost:11434/v1",',
            '        help="Base URL of the OpenAI-compatible endpoint (default: http://localhost:11434/v1). (Env: DEDALO_LLM_URL or OPENAI_BASE_URL)",',
            "    )",
            "    llm_grp.add_argument(",
            '        "--llm-model",',
            '        default=os.environ.get("DEDALO_LLM_MODEL") or "llama3",',
            '        help="LLM model name (default: llama3). (Env: DEDALO_LLM_MODEL)",',
            "    )",
            "    llm_grp.add_argument(",
            '        "--llm-key",',
            '        default=os.environ.get("DEDALO_LLM_KEY") or os.environ.get("OPENAI_API_KEY") or "",',
            '        help="API key for the LLM. (Env: DEDALO_LLM_KEY or OPENAI_API_KEY)",',
            "    )",
            "    llm_grp.add_argument(",
            '        "--llm-temperature",',
            "        type=float,",
            '        default=float(os.environ.get("DEDALO_LLM_TEMPERATURE", "0.7")),',
            '        help="Sampling temperature (default: 0.7).",',
            "    )",
        ]

    # Argumentos para secretos si se detectan
    if reqs["required_secrets"]:
        lines += [
            "",
            '    sec_grp = parser.add_argument_group("Required Secrets and Credentials")',
        ]
        for sec in reqs["required_secrets"]:
            cli_flag = f"--secret-{sec.lower().replace('_', '-')}"
            dest_var = f"secret_{sec.lower().replace('-', '_')}"
            env_var = f"DEDALO_SECRET_{sec.upper().replace('-', '_')}"
            lines += [
                "    sec_grp.add_argument(",
                f"        {cli_flag!r},",
                f"        dest={dest_var!r},",
                f"        default=os.environ.get({env_var!r}, ''),",
                '        help="Value for the secret \'{sec}\'. (Env: {env_var})",'.format(sec=sec, env_var=env_var),
                "    )",
            ]

    lines += [
        "    return parser.parse_args()",
        "",
        "",
        "def main():",
        "    args = parse_args()",
        "",
        "    if args.create_venv:",
        "        setup_virtual_environment()",
        "        return",
        "",
    ]

    # Inyección de LLM en variables de entorno
    if reqs["uses_llm"]:
        lines += [
            '    if getattr(args, "llm_url", None):',
            '        os.environ["DEDALO_LLM_URL"] = args.llm_url',
            '    if getattr(args, "llm_model", None):',
            '        os.environ["DEDALO_LLM_MODEL"] = args.llm_model',
            '    if getattr(args, "llm_key", None):',
            '        os.environ["DEDALO_LLM_KEY"] = args.llm_key',
            '    if getattr(args, "llm_temperature", None) is not None:',
            '        os.environ["DEDALO_LLM_TEMPERATURE"] = str(args.llm_temperature)',
            "",
        ]

    # Inyección de secretos en variables de entorno
    if reqs["required_secrets"]:
        lines += [
            f"    secrets_list = {reqs['required_secrets']!r}",
            "    for sec in secrets_list:",
            '        dest_var = "secret_" + sec.lower().replace("-", "_")',
            '        env_var = "DEDALO_SECRET_" + sec.upper().replace("-", "_")',
            "        val = getattr(args, dest_var, None)",
            "        if val:",
            "            os.environ[env_var] = str(val)",
            "",
        ]

    # Resolución de entrada y ejecución
    lines += [
        "    payload = None",
        "    if args.input_file:",
        "        try:",
        '            payload = Path(args.input_file).read_text(encoding="utf-8")',
        "        except Exception as err:",
        '            print(f"Error reading input file \'{args.input_file}\': {err}", file=sys.stderr)',
        "            sys.exit(1)",
        "    elif args.input_data is not None:",
        "        payload = args.input_data",
        "    elif not sys.stdin.isatty():",
        "        try:",
        "            import select",
        "            if select.select([sys.stdin], [], [], 0.0)[0]:",
        "                stdin_data = sys.stdin.read().strip()",
        "                if stdin_data:",
        "                    payload = stdin_data",
        "        except Exception:",
        "            pass",
        "",
        "    if isinstance(payload, str):",
        "        try:",
        "            payload = json.loads(payload)",
        "        except Exception:",
        "            pass",
        "",
        f"    ctx = FlowContext(flow_name={flow_name!r}, payload=payload, auto_approve=bool(getattr(args, 'auto_approve', False)))",
        "    t0 = time.time()",
        "    try:",
        "        results = run(ctx)",
        "    except Exception as exc:",
        '        print(f"Error during flow execution: {exc}", file=sys.stderr)',
        "        sys.exit(1)",
        "    duration = time.time() - t0",
        "",
        "    has_error = False",
        "    if isinstance(results, dict):",
        '        has_error = any(',
        '            (isinstance(r, dict) and r.get("status") == "error") or getattr(r, "failed", False)',
        '            for r in results.values()',
        '        )',
        "",
        "    if args.json:",
        "        out = {}",
        "        if isinstance(results, dict):",
        "            for k, v in results.items():",
        '                if isinstance(v, dict):',
        "                    out[k] = v",
        '                elif hasattr(v, "status"):',
        "                    out[k] = {",
        '                        "status": v.status,',
        '                        "data": v.data,',
        '                        "error": v.error,',
        '                        "duration_ms": getattr(v, "duration", 0.0),',
        "                    }",
        "                else:",
        "                    out[k] = str(v)",
        "        else:",
        '            out = {"result": str(results)}',
        "        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))",
        "    else:",
        "        if args.verbose:",
        '            print(f"[*] Flow completed in {duration:.3f}s")',
        "            if isinstance(results, dict):",
        "                for k, v in results.items():",
        '                    if isinstance(v, dict):',
        '                        st = str(v.get("status", "ok")).upper()',
        '                        dur = v.get("duration_ms", 0.0)',
        '                        err = v.get("error") or ""',
        '                    else:',
        '                        st = getattr(v, "status", "ok").upper()',
        '                        dur = getattr(v, "duration", 0.0)',
        '                        err = getattr(v, "error", "")',
        '                    print(f"  - [{st}] {k} ({dur:.3f}s){\': \' + err if err else \'\'}")',
        "        elif isinstance(results, dict):",
        '            for v in reversed(list(results.values())):',
        '                data = v.get("data") if isinstance(v, dict) else getattr(v, "data", None)',
        '                st = v.get("status") if isinstance(v, dict) else getattr(v, "status", None)',
        '                if st == "ok" and data is not None:',
        '                    if isinstance(data, (dict, list)):',
        '                        print(json.dumps(data, indent=2, ensure_ascii=False))',
        '                    else:',
        '                        print(data)',
        '                    break',
        "",
        "    sys.exit(1 if has_error else 0)",
        "",
        "",
        'if __name__ == "__main__":',
        "    main()",
        "",
    ]

    source = "\n".join(lines)
    try:
        ast.parse(source)
        _compile_source(source, "<dedalo_standalone>", "exec")
    except SyntaxError as exc:
        raise GraphError([tr('the code generated for standalone mode is not valid Python: {exc}', exc=exc)]) from exc

    return source
