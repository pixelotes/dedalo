# Evaluation: external secret stores (1Password and others)

Status: **phase 1 implemented** (providers `env`, `file`, `op` and `vault`, TTL cache, bootstrap credentials hidden from shell commands). Goal: let Dédalo use secrets it never stores locally. See [Usage](#usage-phase-1) below; sections 1–3 keep the original analysis.

## 1. Premise, and one correction

"The safest secret is the one you don't store" is right for the **value**, but something must still be stored: the credential used to fetch it (a 1Password service account token, a Vault AppRole, ...). What changes is the blast radius:

| | Today (encrypted files) | External store |
|---|---|---|
| What an attacker gets by reading `data/` | `data/secrets/*` **and** `data/master.key` → everything (unless `DEDALO_MASTER_KEY` is set elsewhere) | Nothing useful: only references such as `op://Infra/Telegram/token` |
| What an attacker gets by owning the host/process | Everything | The bootstrap token: read-only, scoped to one vault, revocable and audited centrally |
| Rotation | Edit each secret in Dédalo | Rotate once in the store; Dédalo picks it up after the cache TTL |
| Backups / git leaks | Ciphertext + key may travel together | Contain no secret material |
| Offline / store down | Works | Needs the cache or fails closed |

So it is a real improvement, but the bootstrap credential becomes the new crown jewel and must be kept out of `data/`, out of subprocess environments and out of logs.

## 2. Options

### 2.1 Zero code (already works today): inject into the environment

`DEDALO_SECRET_<NAME>` environment variables already take precedence over files and are never written to disk by Dédalo. Any tool that injects env vars therefore works:

- 1Password: `op run --env-file=dedalo.env -- uvicorn dedalo.app:app ...`, where `dedalo.env` holds `DEDALO_SECRET_TELEGRAM=op://Infra/Telegram/token`. Secrets exist only in the process environment.
- systemd: `LoadCredentialEncrypted=` / `Environment` from a credential, Docker/Compose secrets, Vault Agent, SOPS (`sops exec-env`).
- Standalone exports use the same variables, so `op run -- python flow.py` works for them too.

Limits: resolved once at start (rotation needs a restart); the secret list is fixed at launch; the `op` binary must exist where Dédalo starts.

### 2.2 Reference-based resolution (recommended next step)

A file in `data/secrets/NAME.secret` may hold a **reference** instead of a value:

```
op://Infra/Telegram/token        # 1Password
vault://secret/data/dedalo#token # Vault / OpenBao
env://TELEGRAM_TOKEN             # any env var
file:///run/secrets/telegram     # Docker/systemd secret file
```

`secrets.get(name)` keeps its signature: environment override first, then the file; if the content is a reference, a provider resolves it, otherwise it is a plain value (encrypted or legacy). **Flows and nodes that use the secret by name do not change.** References are not secret, so they can be exported and shared.

Design points:

- **Providers lazily imported**, registered by URI scheme; nothing is loaded unless a reference uses it (no cost for current users).
- **In-memory TTL cache** (`DEDALO_SECRETS_CACHE_TTL`, default 300 s, `0` disables). Never persisted, never logged, thread-safe, short negative caching.
- **Fail closed**: an unreachable store raises a clear `SecretError`; no silent fallback to stale values after TTL.
- **Hide the bootstrap credentials from child processes**: extend `sandbox.clean_env()` with `OP_*`, `VAULT_*`, `BAO_*`, `BWS_*`, `AWS_*` (configurable). Today only `DEDALO_*` secrets are stripped, which would let a shell node read `OP_SERVICE_ACCOUNT_TOKEN`.
- `/api/secrets` would list name + provider kind (`local`, `op`, `vault`, ...), never values.
- Standalone exports keep their environment-only shim (use `op run` there).

### 2.3 Providers compared

| Provider | How | New dependency | Notes |
|---|---|---|---|
| **1Password via `op` CLI** | `op read op://...` with `OP_SERVICE_ACCOUNT_TOKEN` | none (external binary) | Simplest; one process spawn per cache miss. Check that the `op` build exists for your architecture (arm64 / 32-bit ARM) before promising Raspberry Pi support. |
| **1Password Connect** | REST to a self-hosted Connect server with a Connect token | none (`httpx`) | The server caches, so re-reads are not rate-limited; but you must run two containers. Best for many flows or personal plans. |
| **1Password SDK** | `onepassword-sdk` (`client.secrets.resolve`) with a service account | optional extra (compiled wheel) | No subprocess, but a heavy optional dependency; confirm wheels for the target platforms. |
| **Vault / OpenBao** | HTTP (KV v2) with AppRole or token | none (`httpx`) | OpenBao is the Linux Foundation fork of the last MPL Vault; Vault-compatible API. Heavy to operate for small setups. |
| **Bitwarden Secrets Manager** | `bws` CLI or API with an access token | none | Similar to the `op` model. |
| **Cloud (AWS/GCP/Azure)** | IAM role / managed identity | SDK | Best when running in that cloud: no static bootstrap token at all. |

### 2.4 1Password specifics and constraints

- **Service accounts** authenticate with one token scoped to chosen vaults; they cannot read built-in Personal/Private/Employee vaults, so secrets must live in a dedicated vault.
- **Rate limits** (per token per hour, plus a daily cap shared by the whole 1Password account): reads 10,000/h on Business and 1,000/h on Families, Teams and individual plans; writes 1,000/h vs 100/h; daily requests 50,000 (Business), 5,000 (Teams), 1,000 (Families/individual). A single CLI call can consume more than one request. Without caching, one flow running every minute that reads three secrets would use ~4,300 reads/day and exhaust a personal plan; hence the mandatory TTL cache. `op service-account ratelimit` shows current usage.
- **Latency** of `op read` is a process spawn plus a network round trip (not measured here): acceptable on a cache miss, not per node execution.

## Usage (phase 1)

Store a reference instead of a value, from the Secrets tab, `POST /api/secrets` or a file:

```bash
curl -X POST localhost:8080/api/secrets -H 'Content-Type: application/json' \
  -d '{"name": "telegram", "value": "op://Infra/Telegram/token"}'
```

| Reference | Needs |
|---|---|
| `op://Vault/Item/field` | the `op` CLI in `PATH` and `OP_SERVICE_ACCOUNT_TOKEN` in the Dédalo environment (`DEDALO_OP_BIN`, `DEDALO_OP_TIMEOUT` to tune) |
| `vault://mount/path#field` | `VAULT_ADDR` + `VAULT_TOKEN` (or `VAULT_TOKEN_FILE`; `BAO_*` for OpenBao; optional `VAULT_NAMESPACE`, `VAULT_CACERT`); KV v2 |
| `env://VAR` | the variable set in the Dédalo process |
| `file:///run/secrets/name` | an absolute, readable path (Docker/systemd secrets) |

- Flows and nodes keep using the secret **by name**; nothing changes for them.
- Resolved values are cached in memory for `DEDALO_SECRETS_CACHE_TTL` seconds (default 300; `0` disables). Failures are cached for 5 s so a down store is not hammered, and concurrent callers share one remote read.
- `GET /api/secrets/info` lists `{name, kind}` (`local`, `env`, `op`, `vault`, `file`) and `POST /api/secrets/test?name=` checks that a secret resolves; neither returns values.
- `OP_*`, `VAULT_*`, `BAO_*` and `BWS_*` are removed from the environment of `input.command`, `output.command` and the coder `bash` tool. If a shell command really needs one (for example `VAULT_ADDR`), list it in `DEDALO_ENV_ALLOW`.
- Errors never include secret material and fail closed: if the store is unreachable after the cache expires, the node reports a clear error.

Not covered yet: 1Password Connect and the SDK, Bitwarden Secrets Manager, cloud providers, Vault AppRole login (use Vault Agent or a token file), and showing the secret origin in the UI.

## 3. Recommendation

1. **Now (docs only):** document `op run --env-file`, systemd credentials and Docker secrets as the supported "no local secret storage" setup, and strip `OP_*`/`VAULT_*`/`BWS_*` from subprocess environments.
2. **Next (small, reversible):** reference resolution with providers `env`, `file`, `op` (CLI) and `vault`/OpenBao (HTTP), TTL cache, fail-closed errors, tests with fake providers. No new Python dependencies, fully backward compatible with existing secret names and flows.
3. **Later, on demand:** 1Password Connect and SDK, Bitwarden Secrets Manager, cloud providers.

Open questions for the maintainer: which provider matters first (1Password only, or also Vault/OpenBao), whether Raspberry Pi 32-bit support is required, and whether secrets should be allowed to be referenced directly in node configuration (`op://...`) in addition to by name.

## Sources

- 1Password: [Service accounts](https://developer.1password.com/docs/service-accounts/get-started), [rate limits](https://developer.1password.com/docs/service-accounts/rate-limits), [Secrets Automation](https://developer.1password.com/docs/secrets-automation)
