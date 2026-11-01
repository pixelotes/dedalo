"""Tests for external secret references (env://, file://, op://, vault://) and the TTL cache."""

import stat
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from dedalo import runtime, sandbox, secret_providers as providers, secrets
from dedalo.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "DIR", tmp_path / "secrets")
    monkeypatch.setattr(secrets, "KEY_FILE", tmp_path / "master.key")
    for var in ("DEDALO_MASTER_KEY", "DEDALO_MASTER_KEY_FILE", "DEDALO_SECRETS_CACHE_TTL", "DEDALO_ENV_ALLOW",
                "VAULT_ADDR", "VAULT_TOKEN", "BAO_ADDR", "BAO_TOKEN", "VAULT_TOKEN_FILE", "DEDALO_OP_BIN"):
        monkeypatch.delenv(var, raising=False)
    providers.clear_cache()
    providers._ref_locks.clear()
    yield tmp_path
    providers.clear_cache()


def test_is_reference():
    for ref in ("op://Infra/Telegram/token", "vault://secret/app#key", "env://TOKEN", "file:///run/secrets/x"):
        assert providers.is_reference(ref)
    for plain in ("hunter2", "https://example.com/x", "op:// spaced", "", "op:/missing"):
        assert not providers.is_reference(plain)


def test_env_and_file_providers(store, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "from-env")
    secrets.set("a", "env://MY_TOKEN")
    assert secrets.get("a") == "from-env"

    f = store / "token.txt"
    f.write_text("from-file\n")
    secrets.set("b", f"file://{f}")
    assert secrets.get("b") == "from-file"

    secrets.set("c", "file://relative/path")
    with pytest.raises(secrets.SecretError, match="absolute path"):
        secrets.get("c")
    secrets.set("d", "env://NOT_DEFINED_ANYWHERE")
    with pytest.raises(secrets.SecretError, match="NOT_DEFINED_ANYWHERE"):
        secrets.get("d")


def _fake_op(tmp_path, monkeypatch, body):
    exe = tmp_path / "op"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("DEDALO_OP_BIN", str(exe))


def test_op_provider_uses_cli_and_service_account_token(store, monkeypatch):
    # $1=read $2=--no-newline $3=reference
    _fake_op(store, monkeypatch, 'printf "%s:%s" "$OP_SERVICE_ACCOUNT_TOKEN" "$3"\n')
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "svc-token")
    secrets.set("tg", "op://Infra/Telegram/token")
    assert secrets.get("tg") == "svc-token:op://Infra/Telegram/token"


def test_op_errors_are_clear_and_do_not_leak(store, monkeypatch):
    _fake_op(store, monkeypatch, 'echo "[ERROR] item not found" >&2; exit 1\n')
    secrets.set("tg", "op://Infra/Missing/token")
    with pytest.raises(secrets.SecretError, match="item not found"):
        secrets.get("tg")
    monkeypatch.setenv("DEDALO_OP_BIN", "no-such-binary-xyz")
    providers.clear_cache()
    with pytest.raises(secrets.SecretError, match="not installed"):
        secrets.get("tg")


def test_vault_provider(store, monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout, verify):
        seen.update(url=url, headers=headers)
        return httpx.Response(200, json={"data": {"data": {"token": "s3cret"}}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.local:8200/")
    monkeypatch.setenv("VAULT_TOKEN", "hvs.abc")
    secrets.set("v", "vault://secret/dedalo/telegram#token")
    assert secrets.get("v") == "s3cret"
    assert seen["url"] == "https://vault.local:8200/v1/secret/data/dedalo/telegram"
    assert seen["headers"]["X-Vault-Token"] == "hvs.abc"

    secrets.set("w", "vault://secret/dedalo/telegram#missing")
    with pytest.raises(secrets.SecretError, match="not found"):
        secrets.get("w")
    secrets.set("x", "vault://nopath")
    with pytest.raises(secrets.SecretError, match="vault://mount/path#field"):
        secrets.get("x")
    monkeypatch.delenv("VAULT_TOKEN")
    providers.clear_cache()
    with pytest.raises(secrets.SecretError, match="VAULT_ADDR"):
        secrets.get("v")


def test_vault_http_error(store, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **k: httpx.Response(403, request=httpx.Request("GET", url)))
    monkeypatch.setenv("VAULT_ADDR", "https://v")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    secrets.set("v", "vault://secret/app#k")
    with pytest.raises(secrets.SecretError, match="HTTP 403"):
        secrets.get("v")


def test_ttl_cache_hits_expires_and_can_be_disabled(monkeypatch):
    calls = []
    monkeypatch.setitem(providers._PROVIDERS, "env", lambda ref: (calls.append(ref), "v%d" % len(calls))[1])
    monkeypatch.setenv("DEDALO_SECRETS_CACHE_TTL", "60")
    assert providers.resolve("env://X") == "v1"
    assert providers.resolve("env://X") == "v1"  # cached
    assert len(calls) == 1

    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 61)
    assert providers.resolve("env://X") == "v2"  # expired

    monkeypatch.setenv("DEDALO_SECRETS_CACHE_TTL", "0")
    providers.clear_cache()
    providers.resolve("env://X")
    providers.resolve("env://X")
    assert len(calls) == 4  # no caching at all


def test_failures_are_cached_briefly(monkeypatch):
    calls = []

    def boom(ref):
        calls.append(1)
        raise providers.ProviderError("store down")

    monkeypatch.setitem(providers._PROVIDERS, "env", boom)
    for _ in range(3):
        with pytest.raises(providers.ProviderError, match="store down"):
            providers.resolve("env://X")
    assert len(calls) == 1  # a dead store is not hammered


def test_concurrent_callers_share_one_remote_read(monkeypatch):
    calls = []

    def slow(ref):
        calls.append(1)
        time.sleep(0.2)
        return "value"

    monkeypatch.setitem(providers._PROVIDERS, "env", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(providers.resolve("env://X"))) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results == ["value"] * 6 and len(calls) == 1


def test_kind_and_describe_never_expose_values(store, monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_FROM_ENV", "shh")
    secrets.set("plain", "hunter2")
    secrets.set("ref", "op://Infra/Telegram/token")
    info = {i["name"]: i["kind"] for i in secrets.describe()}
    assert info == {"from-env": "env", "plain": "local", "ref": "op"}
    assert "hunter2" not in str(secrets.describe())


def test_api_info_and_test_endpoints(store, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abcdef")
    secrets.set("viaenv", "env://MY_TOKEN")
    secrets.set("broken", "env://NOPE_NOT_SET")

    info = client.get("/api/secrets/info").json()
    assert {"name": "viaenv", "kind": "env"} in info

    ok = client.post("/api/secrets/test?name=viaenv").json()
    assert ok == {"status": "ok", "name": "viaenv", "kind": "env", "length": 6}
    assert "abcdef" not in str(ok)
    assert client.post("/api/secrets/test?name=broken").status_code == 502
    assert client.post("/api/secrets/test?name=missing").status_code == 404
    # el listado clásico de nombres no cambia (lo usa la interfaz)
    assert sorted(client.get("/api/secrets").json()) == ["broken", "viaenv"]


def test_bootstrap_credentials_hidden_from_shell_nodes(monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "svc-secret")
    monkeypatch.setenv("VAULT_TOKEN", "hvs.secret")
    monkeypatch.setenv("BAO_ADDR", "https://bao")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example")  # no empieza por OP_
    out = runtime.command_input(None, [], cmd="env").data["stdout"]
    assert "svc-secret" not in out and "hvs.secret" not in out and "https://bao" not in out
    assert "OPENAI_BASE_URL" in out


def test_env_allow_reexposes_selected_variables(monkeypatch):
    monkeypatch.setenv("VAULT_ADDR", "https://vault.local")
    monkeypatch.setenv("VAULT_TOKEN", "hvs.secret")
    monkeypatch.setenv("DEDALO_ENV_ALLOW", "VAULT_ADDR")
    env = sandbox.clean_env()
    assert env["VAULT_ADDR"] == "https://vault.local" and "VAULT_TOKEN" not in env
