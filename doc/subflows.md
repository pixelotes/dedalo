# Sub-flows: Flows That Call Other Flows

## 1. What They Are and Why

Every Dédalo flow is independent: it has its own schedule, its own state in `data/state/` and its own history. That is great until you have five flows that each send a message every morning (news, mail, security news, calendar, weather) and what you really want is **one** report.

The alternative used to be a giant flow with every branch in it. Sub-flows avoid that: each topic stays a small, testable flow, and a **master flow** runs them, collects what they produce and assembles the result.

```text
                      ┌─► news ───────┐
                      ├─► mail ───────┤
[input.subflows] ─────┼─► security ───┼──► [transform.script] ──► [output.telegram]
  (runs them at       ├─► agenda ─────┤     (keeps the sections
   the same time)     └─► weather ────┘      that arrived)
```

It is a single node, `input.subflows`. There is no new execution model: the compiler is unchanged and every sub-flow is an ordinary flow.

What you get:

- **Real parallelism.** A compiled flow runs its nodes one after another, so two independent branches inside a flow never overlap. Sub-flows run in their own threads: the report takes as long as the slowest flow, not the sum.
- **Failure isolation.** A flow that fails, or does not arrive in time, is reported in its own entry. The report is built with whatever did arrive.
- **Reuse.** The same flow can run alone (with its own message) and as a piece of the report.
- **Debugging per flow.** Each sub-run has its own entry in the history, linked to the master run.

## 2. Quick Start

1. **Create the small flows** as usual, each ending in the node that produces its section (a summary, a list, a text). Give them an empty schedule if you only want them to run from the report. Examples: `news`, `weather`, `agenda`.
2. **Create the master flow**, for example `morning-report`, with:
   - `input.subflows` with `flows` = `news, weather, agenda` and `timeout` = `90`.
   - `transform.script` connected to it, which builds the text (see [section 6](#6-worked-example-morning-report)).
   - An output node (`output.telegram`, `output.ntfy`, …).
3. **Schedule the master** (for example `0 7 * * *`).
4. **Try it** with the run button in the editor. The sub-flows really run (without their outputs, see [section 4](#4-silent-sub-flows-skip_outputs)), and you will see each of them in the history.

The `flows` value is the id of each flow: the name of its file in `data/flows/` without the extension (the flow name in lowercase, with other characters replaced by hyphens: `Security news` → `security-news`).

## 3. The `input.subflows` Node

### Parameters

| Parameter | Default | Description |
|---|---|---|
| `flows` | (required) | Flow ids separated by commas or new lines. Duplicates are ignored; at most 20. |
| `timeout` | `60` | Seconds to wait for all of them (minimum 1). |
| `max_parallel` | `3` | Flows running at the same time. The rest wait their turn. |
| `payload` | empty | JSON (or plain text) that every sub-flow receives as its payload. |
| `skip_outputs` | on | Runs the sub-flows without their `output.*` nodes. |

### Output

A dictionary keyed by flow id, in the order given in `flows`:

```json
{
  "weather":  {"status": "ok",      "data": "Madrid 21 °C, clear", "error": null, "duration": 1.2, "run_id": "run_1793555038_9a8b7c"},
  "mail":     {"status": "timeout", "data": null, "error": "Did not finish within 90s", "duration": null, "run_id": null},
  "security": {"status": "error",   "data": null, "error": "HTTP 503: ...", "duration": 0.8, "run_id": "run_..."}
}
```

| `status` | Meaning |
|---|---|
| `ok` | At least one of the flow's last nodes produced data. `data` holds it. |
| `error` | The flow failed (it does not exist, it does not compile, a node raised, or all of its last nodes failed). `error` explains why. |
| `skipped` | The flow ran but none of its last nodes produced data (for example a `transform.threshold` that was not met). It is not an error. |
| `timeout` | It did not finish within `timeout` seconds. |

When some inner node failed even though the flow still produced a result, the entry also carries `errors` (node id → message).

The node itself only fails if `flows` is empty. A failing sub-flow never stops the master.

### What a sub-flow returns

The data of its **last nodes** (those without outgoing edges):

- one last node → its value as it is (text, list, dict…);
- several → a dictionary `{node id: value}` with those that produced data.

Design each sub-flow so it ends in the node that holds its section.

## 4. Silent Sub-flows: `skip_outputs`

With `skip_outputs` on (the default), a sub-flow is compiled **without its `output.*` nodes**, so inside the report it sends no messages, writes no files and touches no databases. Its result is the data of the nodes that fed those outputs.

That lets one flow play two roles:

```text
news:  input.rss ─► transform.agent ─► output.telegram
         alone (07:30 schedule):   sends its own message
         inside morning-report:    only returns the summary
```

Turn it off (`skip_outputs` = off) if a sub-flow's output is the point (for example it must save to SQLite) and you want it to run complete.

> A flow that exceeds the timeout cannot be killed and keeps running in the background. With `skip_outputs` on, whatever it produces late sends nothing. With it off, its outputs would still fire, so use a generous `timeout` in that case.

## 5. Payload, Errors and State

### Passing data down: `payload`

`payload` (JSON or text) reaches each sub-flow as its payload, which the `input.webhook` node reads. It is the way to parameterize a flow from the master:

```text
payload: {"city": "Madrid", "days": 2}
```

If no payload is given, the `input.webhook` of a sub-flow returns its usual `manual_preview` placeholder. Sub-flows that do not use `input.webhook` ignore the payload.

### Handling failures

The recommended pattern is the one in the example: iterate the dictionary in a `transform.script` and keep only what has `status == "ok"` and data. The section of a flow that failed or timed out simply does not appear. If you want to know about it, add a line to the report or send an alert when any entry has `status` `error` or `timeout`.

### State and history

- **State**: each sub-flow keeps its own state in `data/state/<flow>_*.json` (for example the already-seen items of `input.rss` with `only_new`), exactly as if it ran alone.
- **History**: every sub-run is its own entry, with `trigger_type` = `subflow:<master run id>`. To debug a section, open the history of that flow.
- **Secrets**: all flows share the secret store; nothing needs to be passed down.
- **Language**: messages follow the master's language (`DEDALO_LANG` or the request language).

## 6. Worked Example: Morning Report

Sub-flows (each one ends in the node that produces its section):

| Flow | Nodes | Section |
|---|---|---|
| `agenda` | `input.calendar` (secret `gcal`, `days_ahead` = 1) → `transform.script` | Today's events |
| `weather` | `input.weather` | Forecast |
| `mail` | `input.imap` → `transform.clean_email` → `transform.agent` | Summary of unread mail |
| `news` | `input.rss` → `transform.agent` | News summary |
| `security` | `input.rss` → `transform.filter` → `transform.agent` | Security news |

The `agenda` script turns the events into lines (an empty list gives an empty text, so the section is hidden):

```python
lines = []
for e in entrada[0]:
    when = "all day" if e["all_day"] else e["start"][11:16]
    place = f" ({e['location']})" if e["location"] else ""
    lines.append(f"- {when} {e['summary']}{place}")
return "\n".join(lines)
```

The master, `morning-report` (schedule `0 7 * * *`): `input.subflows` (`flows` = `agenda, weather, mail, news, security`, `timeout` = `90`) → `transform.script` → `output.telegram`. The script:

```python
import json

SECTIONS = [
    ("agenda", "📅 Today"),
    ("weather", "☀️ Weather"),
    ("mail", "✉️ Mail"),
    ("news", "📰 News"),
    ("security", "🛡️ Security"),
]
parts = []
late = []
for flow_id, title in SECTIONS:
    r = entrada[0].get(flow_id)
    if not r or r["status"] in ("error", "timeout"):
        late.append(flow_id)
        continue
    data = r["data"]
    if not data:
        continue
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)
    parts.append(f"{title}\n{text}")
if late:
    parts.append("⚠️ Not available: " + ", ".join(late))
return "\n\n".join(parts)
```

`entrada[0]` is the first connected input (the `input.subflows` dictionary). Empty sections (`skipped`, no data) are dropped silently; failed or late ones are listed at the end.

## 7. Limits and Behavior to Know

- **Depth and cycles**: sub-flows can call other sub-flows up to 3 levels. A cycle (A → B → A) is detected and that entry returns an `error` mentioning the cycle.
- **Timeout**: a running flow cannot be interrupted (Python cannot kill a thread). The master stops waiting and goes on; the flow finishes in the background. Flows still waiting for a free slot (`max_parallel`) when the time runs out never start.
- **Concurrency**: sub-flows are not counted by `DEDALO_MAX_CONCURRENT_FLOWS` (which only limits scheduled runs). On constrained hardware (Raspberry Pi) keep `max_parallel` low: 2 or 3 is a good value, and LLM-heavy flows spend their time waiting on the network, not on CPU.
- **A flow with its own schedule** still runs on its schedule too. If it happens to run at the same time as the master, both use the same state files; give sub-flows no schedule if they only exist for the report.
- **`enabled`**: the on/off switch of a flow only controls its schedule. A disabled flow can still be called as a sub-flow.
- **Editor runs**: running the master from the editor runs the sub-flows for real (with `skip_outputs` as configured).
- **Standalone export**: not supported yet; exporting a graph with `input.subflows` fails with an explicit error. Daemon mode (`./run.sh --daemon`) does support them.
- **`transform.approval`** (human gate) inside a sub-flow would wait for a person; avoid it in sub-flows or set its behavior to skip.

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Flow "x" not found` | The id does not match a file in `data/flows/`. Use the id (lowercase, hyphens), not the display name. |
| `timeout` in a flow that works alone | The timeout is shorter than the flow (LLM calls, slow feeds), or `max_parallel` is too low and it waited its turn. Raise `timeout` or `max_parallel`. |
| `status` is `skipped` | None of the flow's last nodes produced data. Open that flow's run in the history to see which node did not pass. |
| A sub-flow sent its own message | `skip_outputs` is off, or the flow was run by its own schedule at the same time. |
| `Flow cycle detected` | Two flows call each other. Break the loop: one of them must not use `input.subflows` on the other. |
| `Maximum sub-flow depth (3) exceeded` | Too many nested levels. Flatten the structure. |

See also: [Node catalog](node_catalog.md#inputsubflows), [Use cases](use_cases_and_workflows.md) and [Standalone export](standalone_export.md).
