# Native Agents and Artificial Intelligence in Dédalo

## 1. Why We Avoid LangChain, LangGraph and Heavy Frameworks

In many AI automation solutions, it is common to find dependencies on frameworks such as **LangChain**, **LangGraph**, **LlamaIndex** or the official OpenAI SDKs. Although popular, these packages have serious drawbacks for lightweight production systems:

1. **Massive dependency inflation**: Installing LangChain and its add-ons brings hundreds of packages into `site-packages` (Pydantic, SQLAlchemy, Tenacity, tiktoken, etc.), increasing the virtual environment size by more than 350 MB and inflating the initial RAM footprint.
2. **Fragile abstractions and rapid obsolescence**: Continuous API breakages between minor versions force code rewrites and complicate long-term maintenance.
3. **Loss of control over prompts**: Many frameworks inject hidden default prompts that waste context window tokens and add latency.

**Dédalo's solution:**
- Dédalo talks directly to any LLM server over the standard OpenAI-compatible HTTP REST protocol (`/v1/chat/completions`) using the `httpx` client.
- This makes it just as simple to connect to:
  - **Local models**: Ollama, vLLM, llama.cpp, LocalAI or LM Studio.
  - **Cloud services**: Groq, DeepSeek, OpenAI, Mistral, Together AI, etc.
- All the LLM calling code, the ReAct loop and tool handling is contained in fewer than 400 lines of clean, tested code in `dedalo/runtime.py`.

---

## 2. The 4 Agent Nodes in Dédalo

Dédalo offers four specialized nodes depending on the required task:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                         Agent Nodes in Dédalo                          │
├───────────────────┬───────────────────┬────────────────────────────────┤
│ Node              │ Purpose           │ Key Features                   │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.agent   │ General-Purpose   │ JSON mode, {entrada} variable, │
│                   │ Reasoning Tasks   │ `as_field` enrichment          │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.coder   │ Coding and        │ ReAct loop, bash, atomic       │
│                   │ CLI Operations    │ read and edit, Blackhole       │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.analyzer│ SecOps, SRE and   │ Secret masking, batch          │
│                   │ Diagnostics       │ processing (per_item)          │
├───────────────────┼───────────────────┼────────────────────────────────┤
│ transform.planner │ Action and        │ Structured plans (Markdown     │
│                   │ Remediation Plans │ or JSON) with mitigation/rollb │
└───────────────────┴───────────────────┴────────────────────────────────┘
```

---

## 3. `transform.coder`: ReAct Micro-Harness with Real Tools

The `transform.coder` node is not a simple text generator; it is an **interactive autonomous agent** designed to solve code and system administration problems:

### 3.1. Available Tools and Permission Management
The agent has a list of native tools depending on the `tools_preset` or the custom permission configuration in the inspector:
- **`read`**: Reads whole files or specific line ranges with clear numbering.
- **`bash`**: Runs real commands in the server console (inside the `workspace_dir`) with a time limit and stdout/stderr capture.
- **`edit`**: Performs atomic string replacements (`old_str` -> `new_str`), requiring the occurrence to be unique to avoid ambiguous edits.
- **`write`**: Writes new files to disk.
- **`grep`**: Fast search for expressions or patterns within the working tree.
- **`find` / `ls`**: Quick exploration of the file and directory hierarchy.
- **`web_search`**: Search for documentation and solutions on the internet through DuckDuckGo (no API keys needed).

#### Granular Permission Management from the Inspector
In the node's settings side panel, the operator can select predefined profiles (`Coding`, `Read-Only`, `All`, `Custom`) or enable/disable specific permissions grouped by risk level:
- **Read and Navigation** (Safe): `read`, `grep`, `find`, `ls`.
- **Disk Modification** (Write): `edit`, `write`.
- **Console Execution** (Shell): `bash`.
- **Network / Internet Access**: `web_search`.

### 3.2. Context Compaction (Blackhole and Luhn+)
When an agent interacts over multiple turns, the message history grows quickly and can overflow the model's context window.
- Dédalo auto-detects the size of the model's context window (`context_window`).
- If the accumulated tokens reach **90%** of the window, the selected compaction engine kicks in:
  - **Blackhole (Deterministic)**: Synthesizes the history without additional LLM calls, preserving the user's original instruction, the tools invoked and their exit codes, and purging verbose intermediate outputs.
  - **Luhn+ (Weighted Algorithmic)**: Extracts the most representative sentences based on the frequency and position of keywords.

---

## 4. `transform.analyzer` and Secret Masking (`sanitize_secrets`)

In security operations (SecOps) and infrastructure investigation, logs and traces often contain authentication tokens, credentials or private keys. Sending this data to an external model poses a critical risk of information leakage.

When `sanitize_secrets: True` is enabled in `transform.analyzer`:
- The node runs an in-memory sanitization function (`_mask_secrets_in_text`) before building the payload for the LLM.
- The following are masked automatically:
  - `Authorization: Bearer <token>` headers and JWT tokens (`ey...`).
  - PEM private keys (`-----BEGIN PRIVATE KEY-----`).
  - Secrets and keys for AWS (`AKIA...`), GitHub (`ghp_...`, `ghs_...`) and Slack (`xoxb-...`).
  - Passwords and tokens in URLs (`https://user:password@host`).
  - Private IPv4 addresses and Atlassian credentials.

### Batch Processing (`per_item`)
If the input is a list of Jira tickets (`input.jira_issues`) or Confluence pages (`input.confluence_pages`), `transform.analyzer` can iterate over each item sequentially, diagnose it individually and inject the analysis directly into each record in the field given by `as_field` (e.g. `item["analysis"] = "..."`).

---

## 5. `transform.planner`: Structured Mitigation

The `transform.planner` node acts as the complementary step to the analyzer:
- It receives the incident diagnosis and the resource metadata.
- It produces a structured remediation plan covering:
  1. **Immediate Mitigation Actions** (to stop the impact).
  2. **Permanent Resolution** (to fix the root cause).
  3. **Rollback Plan** (in case the fix fails).
  4. **Verification Tests**.
- It can emit the result as Markdown ready to attach to a Jira ticket or a Confluence page, or as strict JSON for further automated processing.
