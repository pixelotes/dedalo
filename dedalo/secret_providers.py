"""External secret providers: a secret file may hold a reference instead of a value.

Supported references:
    env://VAR                          environment variable of the Dédalo process
    file:///run/secrets/name           file (Docker/systemd secret)
    op://Vault/Item/field              1Password, through the `op` CLI (service account)
    vault://mount/path/to/secret#field HashiCorp Vault / OpenBao (KV v2) over HTTP

Resolved values live only in an in-memory cache (TTL `DEDALO_SECRETS_CACHE_TTL`, default 300 s,
0 disables it), are never logged and never written to disk. Providers are imported lazily, so
nothing here costs anything unless a reference is actually used.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from dedalo.i18n import tr

REFERENCE = re.compile(r"^(env|file|op|vault)://\S+$")

_ERROR_TTL = 5.0  # failures are cached briefly so a dead store is not hammered (rate limits)

_cache: dict[str, tuple[float, str | None, str | None]] = {}  # ref -> (expires, value, error)
_lock = threading.Lock()
_ref_locks: dict[str, threading.Lock] = {}


class ProviderError(Exception):
    """The reference could not be resolved. The message never contains secret material."""


def is_reference(value: str) -> bool:
    return bool(REFERENCE.match(value or ""))


def scheme(ref: str) -> str:
    return ref.split("://", 1)[0]


def _ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("DEDALO_SECRETS_CACHE_TTL", "300")))
    except ValueError:
        return 300.0


def _timeout(var: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(var, default)))
    except ValueError:
        return default


# -- providers --------------------------------------------------------------


def _env(ref: str) -> str:
    var = ref[len("env://"):]
    value = os.environ.get(var)
    if not value:
        raise ProviderError(tr("environment variable {var} is not set", var=var))
    return value


def _file(ref: str) -> str:
    path = Path(ref[len("file://"):])
    if not path.is_absolute():
        raise ProviderError(tr("file references must use an absolute path (file:///...)"))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProviderError(tr("could not read {path}: {error}", path=str(path), error=exc.strerror or "error")) from exc


def _op(ref: str) -> str:
    exe = shutil.which(os.environ.get("DEDALO_OP_BIN", "op"))
    if not exe:
        raise ProviderError(tr("the 1Password CLI (`op`) is not installed or not in PATH"))
    try:
        proc = subprocess.run(
            [exe, "read", "--no-newline", ref],
            capture_output=True,
            text=True,
            timeout=_timeout("DEDALO_OP_TIMEOUT", 15.0),
            env=os.environ.copy(),  # needs OP_SERVICE_ACCOUNT_TOKEN, which child commands of flows never see
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(tr("1Password did not answer in time")) from exc
    except OSError as exc:
        raise ProviderError(tr("could not run the 1Password CLI: {error}", error=exc.strerror or "error")) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        raise ProviderError(tr("1Password could not read the reference: {detail}", detail=detail[0][:200]))
    return proc.stdout


def _vault(ref: str) -> str:
    import httpx

    body = ref[len("vault://"):]
    path, _, field = body.partition("#")
    mount, _, secret_path = path.strip("/").partition("/")
    if not (mount and secret_path and field):
        raise ProviderError(tr("vault references look like vault://mount/path#field"))

    addr = (os.environ.get("VAULT_ADDR") or os.environ.get("BAO_ADDR") or "").rstrip("/")
    token = os.environ.get("VAULT_TOKEN") or os.environ.get("BAO_TOKEN") or ""
    token_file = os.environ.get("VAULT_TOKEN_FILE")
    if not token and token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderError(tr("could not read VAULT_TOKEN_FILE: {error}", error=exc.strerror or "error")) from exc
    if not addr or not token:
        raise ProviderError(tr("set VAULT_ADDR and VAULT_TOKEN (or BAO_ADDR / BAO_TOKEN) to use vault:// references"))

    headers = {"X-Vault-Token": token}
    if os.environ.get("VAULT_NAMESPACE"):
        headers["X-Vault-Namespace"] = os.environ["VAULT_NAMESPACE"]
    url = f"{addr}/v1/{quote(mount, safe='')}/data/{quote(secret_path, safe='/')}"
    verify: bool | str = os.environ.get("VAULT_CACERT") or True
    try:
        resp = httpx.get(url, headers=headers, timeout=_timeout("DEDALO_VAULT_TIMEOUT", 10.0), verify=verify)
    except httpx.HTTPError as exc:
        raise ProviderError(tr("could not reach Vault: {error}", error=type(exc).__name__)) from exc
    if resp.status_code != 200:
        raise ProviderError(tr("Vault answered HTTP {status} for {path}", status=resp.status_code, path=path))
    try:
        value = resp.json()["data"]["data"][field]
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError(tr("field {field!r} not found in {path}", field=field, path=path)) from exc
    return str(value)


_PROVIDERS = {"env": _env, "file": _file, "op": _op, "vault": _vault}


# -- resolution with cache --------------------------------------------------


def resolve(ref: str) -> str:
    """Return the secret value behind `ref`, using the in-memory TTL cache."""
    if not is_reference(ref):
        raise ProviderError(tr("not a valid secret reference"))
    now = time.monotonic()
    with _lock:
        hit = _cache.get(ref)
        ref_lock = _ref_locks.setdefault(ref, threading.Lock())
    if hit and hit[0] > now:
        if hit[2]:
            raise ProviderError(hit[2])
        return hit[1] or ""

    with ref_lock:  # concurrent callers share one remote read
        with _lock:
            hit = _cache.get(ref)
        if hit and hit[0] > time.monotonic():
            if hit[2]:
                raise ProviderError(hit[2])
            return hit[1] or ""
        ttl = _ttl()
        try:
            value = _PROVIDERS[scheme(ref)](ref)
        except ProviderError as exc:
            if ttl:
                with _lock:
                    _cache[ref] = (time.monotonic() + min(_ERROR_TTL, ttl), None, str(exc))
            raise
        if ttl:
            with _lock:
                _cache[ref] = (time.monotonic() + ttl, value, None)
        return value


def clear_cache() -> None:
    with _lock:
        _cache.clear()
