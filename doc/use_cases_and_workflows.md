# Representative Use Cases and Workflows in Dédalo

Below are 6 real-world use cases with their node flow diagrams, recommended configurations, and data architecture.

---

## 1. K8s Security Audit with Forensic Preservation

### Goal
Periodically inspect the state of a Kubernetes cluster, preserve the original evidence immutably through compression and cryptographic sealing (for regulatory compliance or forensic analysis), and notify the technical team with a diagnosis and action plan.

```text
               ┌─── [input.command / script] ───┐
               │    (kubectl get pods -A ...)   │
               └──────────────┬─────────────────┘
                              │ Raw telemetry
            ┌─────────────────┴─────────────────┐
            ▼                                   ▼
 [output.evidence_vault]              [transform.analyzer]
 (Immutable Gzip + SHA-256)           (Sanitizes secrets + Diagnosis)
                                                │
                                                ▼
                                      [transform.planner]
                                      (Structured mitigation plan)
                                                │
                                                ▼
                                      [output.jira_issue]
                                      (Opens ticket with cause and plan)
```

### Step-by-Step Configuration:
1. **Extraction**: A `transform.script` node runs `kubectl` with a secure kubeconfig mounted in memory, extracting the pods that are not in `Running` state. (Shell commands do not inherit Dédalo's secrets from the environment; the kubeconfig must be mounted explicitly, and with `DEDALO_SANDBOX` enabled its directory must be exposed via `DEDALO_SANDBOX_RO`.)
2. **Evidence Custody (`output.evidence_vault`)**:
   - `vault_directory: "data/vault/k8s"`
   - Generates `k8s-evidence-20260914-1530.json.gz` and its `.meta.json` manifest with the tamper-proof SHA-256 hash.
3. **Analysis (`transform.analyzer`)**:
   - `sanitize_secrets: True` (guarantees that if a pod contained a secret in its debug environment variables, it is masked before going to the LLM).
   - Diagnoses the root cause (e.g. OOMKilled due to insufficient memory limits).
4. **Planning (`transform.planner`)**:
   - Generates recommendations for adjusting the deployment manifests.
5. **Output (`output.jira_issue`)**:
   - `action: "create"`
   - Creates a "Task" or "Bug" ticket in the infrastructure project with high priority.

---

## 2. Automated Jira Issue Triage and Investigation

### Goal
Monitor the queue of open issues in Jira Cloud, delegate the initial investigation to a triage agent, and update each ticket with a detailed resolution comment, adjusting the priority automatically.

```text
[input.jira_issues] 
  └─ JQL: project = SEC AND status = "Open"
         │
         ▼ List of tickets
[transform.analyzer]
  ├─ per_item: True
  ├─ as_field: "triage"
  └─ prompt: "Investigate the symptom, determine impact and reproduction steps."
         │
         ▼ Batch of enriched tickets
[output.jira_issue]
  ├─ action: "add_comment" (or "transition")
  ├─ batch: True
  └─ comment: "{triage}"
```

### Advantages:
- **Zero heavy frameworks**: Requires neither LangGraph nor external orchestrators.
- **Native batch processing**: The output node detects the enriched list and executes the corresponding REST requests to Jira Cloud in Atlassian Document Format (ADF).

---

## 3. SaaS Users vs Entra ID Review (Zombie User Detection)

### Goal
Run weekly reviews of accounts in corporate SaaS services (GitHub, Jira, Salesforce, AWS) by comparing them against the active users in Microsoft Entra ID (Azure AD). If a user who has left the company is still active in the SaaS, an immediate security alert is triggered.

```text
[input.http_call] (SaaS API: list users) ───────────┐
                                                    ├──► [transform.script]
[input.http_call] (Microsoft Graph API: Entra ID) ──┘    (Set cross-check in Python)
                                                               │
                                                               ▼
                                                     [transform.threshold]
                                                     (Total zombies > 0)
                                                               │
                                                               ▼
                                                     [output.telegram / ntfy]
                                                     (Immediate alert to CISO/IT)
```

### Code in `transform.script`:
```python
# Cross-check data with in-memory Python sets
saas_data = inp.item("input.http_call_1") or []
entra_data = inp.item("input.http_call_2") or []

saas_emails = {u["email"].lower() for u in saas_data}
entra_active_emails = {u["userPrincipalName"].lower() for u in entra_data if u.get("accountEnabled")}

zombies = list(saas_emails - entra_active_emails)

return {
    "total_zombies": len(zombies),
    "zombies_list": zombies,
}
```

---

## 4. Living Documentation in Confluence: In-Place Read and Update

### Goal
Continuously keep an architecture or security posture documentation page up to date in Confluence Cloud, by reading the previous content, recalculating the new state, and republishing the page without destructively overwriting historical information.

```text
[input.confluence_pages]
  ├─ space_key: "SEC"
  └─ title: "Weekly Security Posture"
          │
          ▼ (id, version, content_text)
[transform.analyzer / script]
  └─ Generates new findings and incorporates the updated section
          │
          ▼
[output.confluence_page]
  ├─ mode: "create_or_update" (or "append")
  └─ Handles version + 1 and Storage Format XHTML automatically
```

---

## 5. Secure Morning Email Briefing (Prompt-Injection Defense)

### Goal
Process incoming emails received overnight to generate an executive daily summary, neutralizing *prompt injection* attack attempts hidden in invisible text or malicious marketing code.

```text
[input.imap] ──────────► [transform.clean_email] ───► [transform.agent] ───► [output.telegram]
(Unread mailbox)         - Strips CSS-hidden text      (Executive summary)    (Delivery to phone)
                         - Removes zero-width characters
                         - Truncates signatures and disclaimers
```

---

## 6. Hardware and Network Monitoring on Raspberry Pi

### Goal
Unattended 24/7 monitoring of a home or industrial node. The flow consumes 0% CPU most of the time and runs every 15 minutes, checking temperature, disk, and SSL certificate expiry.

```text
[input.pi_health] (CPU temp and memory) ─┐
[input.disk_free] (Space on /) ──────────┼──► [transform.threshold] ──► [output.ntfy]
[input.ssl_cert]  (TLS days remaining) ──┘    (Temp > 75 or Disk < 10%    (Instant push)
                                               or TLS days < 15)
```
If the parameters are within normal ranges, `transform.threshold` emits the `SKIPPED` sentinel, and the flow ends without generating network traffic or unnecessary alerts.
