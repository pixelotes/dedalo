# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `input.subflows` node: runs other flows in parallel with a timeout and returns each result (status, data, error); sub-flows skip their `output.*` nodes by default, are limited to 3 levels with cycle detection, and are linked to the parent run in the history. Not available in the standalone export. See `doc/subflows.md`.
- `input.calendar` node: reads iCalendar (`.ics`) URLs, such as Google Calendar's secret address, with recurring event expansion; the URL is meant to be stored as a secret.
- External secret references (`op://`, `vault://`, `env://`, `file://`) with an in-memory TTL cache, `GET /api/secrets/info` and `POST /api/secrets/test`; `OP_*`, `VAULT_*`, `BAO_*` and `BWS_*` are hidden from shell commands (`DEDALO_ENV_ALLOW` re-exposes some).

- Internationalization (English/Spanish) of the UI, node catalog, templates and messages.
- Optional webhook token (`DEDALO_WEBHOOK_TOKEN`), body size limit (`DEDALO_MAX_BODY_MB`) and security headers with CSP.
- Encryption of secrets at rest, environment filtering and optional command isolation with bubblewrap.
- `Host` (`DEDALO_ALLOWED_HOSTS`) and `Origin` validation; CORS is removed.
- Elastic License 2.0, `NOTICE`, security policy and contribution guide.

### Changed

- BREAKING: the default language is now English (UI, messages, docs). Set DEDALO_LANG=es to keep Spanish messages in scheduled runs and webhooks; the UI language is chosen in Settings.
- Webhooks run in a thread and no longer block the server.
- `/api/history/purge` requires `days` between 1 and 3650.
- `list_flows` uses a cache keyed by file date and size, 500 errors on save, import and export no longer expose internal details, and startup is ~100 ms faster (lazy imports).
- The scheduler runs flows in parallel (`DEDALO_MAX_CONCURRENT_FLOWS`, 2 by default).
- The history SQLite database is initialized only once per process, and that initialization is now thread-safe (concurrent first runs no longer fail with `database is locked`).
- The CI TruffleHog and Trivy actions are pinned by SHA.
