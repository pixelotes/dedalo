# Export to Standalone Scripts and Tree-Shaking

## 1. The Standalone Export Concept

One of Dédalo's most powerful and differentiating capabilities is that you can visually design and debug a complex flow on the canvas and, once validated, **export it as a single, independent Python file** (`.py`) that:
- **Does not depend on Dédalo**: You don't need the Dédalo server installed or running.
- **Does not depend on heavy packages**: It runs with the Python standard library plus the minimal external dependencies that particular flow actually needs (for example, `httpx`).
- **Is 100% readable and maintainable**: You can open the generated file, audit it, modify it with any editor, and version it in your Git repository.

---

## 2. Dynamic Tree-Shaking in the Compiler

When a standalone script is generated through the `compiler.compile_standalone(graph)` function or from the web interface (download/export button):

1. **Inspection of Used Nodes**:
   The compiler analyzes the node types present in the graph.
2. **Selective Library Inclusion (Tree-Shaking)**:
   Unlike a static template that would import every possible library, Dédalo generates only the code blocks and imports that are strictly necessary:
   - If the flow makes no HTTP requests or LLM calls $\to$ **it does not import `httpx`**.
   - If it does not use `output.evidence_vault` $\to$ **it does not import `gzip` or `hashlib`**.
   - If it does not interact with SQLite $\to$ **it does not import `sqlite3`**.
   - If it does not use regular expressions $\to$ **it does not import `re`**.
   - If it does not use agents $\to$ **it does not include the micro-harness or the `call_llm` function**.

```text
┌───────────────────────────────────────────────┐
│                Graph on the Canvas            │
│  [input.rss] -> [transform.filter] -> [file]  │
└───────────────────────┬───────────────────────┘
                        │
                        ▼ compile_standalone()
┌───────────────────────────────────────────────┐
│            Self-Contained Python Script       │
│  - Minimal imports (xml, urllib, pathlib)     │
│  - Embedded, specific runtime (rss, filter)   │
│  - run(ctx) function + __main__ block         │
└───────────────────────────────────────────────┘
```

---

## 3. Structure of the Exported Script

The generated file contains three well-delimited sections:

1. **Header and Sentinels**:
   Defines `SKIPPED`, the `NodeResult` class, and the `Inputs` class to handle the data flowing between nodes in a unified way.
2. **Minimal Runtime Functions**:
   Copies only the implementations of the functions corresponding to the nodes in the graph (for example, `rss`, `filter_items`, and `file_output`).
3. **Main `run()` Function and Execution Block**:
   ```python
   def run(ctx=None):
       # Deterministic topological evaluation with skip sentinels
       ...

   if __name__ == "__main__":
       results = run()
       print(results)
   ```

---

## 4. Deployment Methods for the Exported Script

Once the script is downloaded (e.g. `k8s_audit.py`), you can run it on any Linux, Mac, or Windows machine without setting up additional infrastructure.

### 4.1. Direct Execution from the Console
```bash
python3 k8s_audit.py
```

#### Command-Line Flags and Arguments in Standalone Scripts

The generated scripts include a standard argument parser (`argparse`) with the following switches:
- `-i / --input <json|text>`: Injects a payload or data directly into the first node of the flow.
- `-f / --input-file <path>`: Reads the specified file and injects it as input.
- `-j / --json`: Formats the flow's final output as a JSON object structured by node.
- `-v / --verbose`: Shows detailed traces and step-by-step execution times on `stderr`.
- `-y / --yes / --non-interactive`: **Auto-approval at human gates**. In flows that include confirmation nodes (`transform.approval`), it automatically approves execution without asking for interactive input in the terminal (ideal for GitHub Actions runners, GitLab CI, cron, or Docker).
- `--create-venv`: Automatically creates a local Python virtual environment and installs the required packages (specified via PEP 723).
- Dynamic secret flags (`--secret-<name>`): If the flow uses stored secrets, they can be passed via CLI or environment variables (`DEDALO_SECRET_<NAME>`).
- Dynamic LLM flags (`--llm-url`, `--llm-model`, `--llm-key`): If the flow contains nodes with agents or LLMs.

### 4.2. Scheduling with the System Cron
To automate periodic execution without background services:
```cron
# Run at the top of every hour and dump logs
0 * * * * /usr/bin/python3 /opt/scripts/k8s_audit.py >> /var/log/k8s_audit.log 2>&1
```

### 4.3. Deployment with Systemd Timers
Ideal for modern Linux distributions and production servers:

1. Create the service `/etc/systemd/system/k8s-audit.service`:
   ```ini
   [Unit]
   Description=K8s Security Audit (Standalone Script)
   After=network.target

   [Service]
   Type=oneshot
   User=appuser
   WorkingDirectory=/opt/scripts
   Environment="ATLASSIAN_TOKEN=your_token_here"
   ExecStart=/usr/bin/python3 /opt/scripts/k8s_audit.py
   ```

2. Create the timer `/etc/systemd/system/k8s-audit.timer`:
   ```ini
   [Unit]
   Description=Run K8s Audit every 30 minutes

   [Timer]
   OnCalendar=*:0/30
   Persistent=true

   [Install]
   WantedBy=timers.target
   ```

3. Enable the timer:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now k8s-audit.timer
   ```

### 4.4. Docker Micro-Container (Under 60 MB)
```dockerfile
FROM python:3.12-alpine
WORKDIR /app
RUN pip install --no-cache-dir httpx
COPY k8s_audit.py .
CMD ["python", "k8s_audit.py"]
```
