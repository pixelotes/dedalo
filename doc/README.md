# Dédalo Documentation

Welcome to the technical documentation and architecture guide for **Dédalo**, the ultralight visual orchestrator and workflow compiler designed to run continuously on resource-constrained hardware (such as a Raspberry Pi with 15–35 MB of RAM).

---

## 📚 Documentation Index

1. **[Philosophy and Architecture](philosophy_and_architecture.md)**
   - The project's mission and motivation.
   - Why we compile to plain Python instead of using reactive engines or heavy queues.
   - Key technical decisions: no frontend bundlers, a deterministic execution model, secrets encrypted at rest and skip sentinels (`SKIPPED`).

2. **[Complete Node Catalog](node_catalog.md)**
   - Exhaustive catalog of the **72 available nodes**:
     - **27 Input Nodes (`input.*`)**: Webhooks, API calls, Git, incident telemetry, Jira, Confluence, WHOIS/RDAP, DNS DoH, Pi/Linux sensors, RSS, IMAP, etc.
     - **28 Transform Nodes (`transform.*`)**: LLM agents, ReAct Coder, SecOps Analyzer and Planner, CVE/SBOM Audit, Secret Scanner (DLP), JSON Path extraction, Data Mapper, Human Approval Gate (`transform.approval`), list operators, hash diffs, fuzzy deduplication, regex, CSS selectors, sanitization and Python scripts.
     - **17 Output Nodes (`output.*`)**: Microsoft Teams, Evidence Vault with gzip+sha256, Jira operations (create, comment, transition), Confluence pages (create, replace, append), Git commits/push, SQLite, Telegram, Discord, Slack, MQTT, etc.

3. **[Native Agents and Artificial Intelligence](agents_and_llms.md)**
   - Why we avoid bloated frameworks such as LangChain, LangGraph or monolithic SDKs.
   - The multi-turn ReAct micro-harness (`transform.coder`) with real tools (`bash`, `read`, `write`, `edit`, `grep`, `find`, `ls`).
   - Memory compaction algorithms: **Blackhole** and **Luhn+** for finite context windows.
   - In-memory secret masking (`sanitize_secrets`) for cybersecurity and infrastructure flows.

4. **[Standalone Export and Tree-Shaking](standalone_export.md)**
   - How to compile any visual graph into a single, fully self-contained `.py` file.
   - *Tree-shaking* algorithm: dynamic inclusion of only the libraries actually used (`httpx`, `gzip`, `sqlite3`, etc.).
   - Execution modes: interactive, cron daemon, and deployment with Docker or Systemd.

5. **[Use Cases and Representative Workflows](use_cases_and_workflows.md)**
   - Step-by-step diagrams and production-ready flows:
     - **Kubernetes Triage and Remediation with Evidence Preservation**.
     - **Identity Audit: Detecting Zombie Users in SaaS vs Entra ID**.
     - **Automatic Jira Cloud Issue Lifecycle Management**.
     - **Living Documentation in Confluence: Reading, Extracting and Updating Across Spaces**.
     - **Secure Morning Email Briefing (Anti-Injection)**.
     - **Edge Infrastructure Monitoring (Raspberry Pi, SSL Certificates and Latencies)**.

6. **[Dependencies and Performance](dependencies_and_performance.md)**
   - Detailed analysis of the technology stack (FastAPI/Starlette, uvicorn, httpx, optional curl_cffi).
   - Why Node.js, React, Webpack and unnecessary dependencies are not used.
   - Benchmarks and real measurements of memory and CPU usage.
