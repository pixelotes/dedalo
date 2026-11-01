"""Tests para el almacén de secretos."""

import pytest
from dedalo import secrets


def test_set_and_get_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "DIR", tmp_path)

    secrets.set("api-token", "secreto123")
    assert "api-token" in secrets.names()
    assert secrets.get("api-token") == "secreto123"

    secrets.delete("api-token")
    assert "api-token" not in secrets.names()
    assert secrets.get("api-token") is None


def test_invalid_name():
    with pytest.raises(secrets.SecretError):
        secrets.set("../escape", "val")

    with pytest.raises(secrets.SecretError):
        secrets.set("MAYUSCULAS", "val")


def test_env_override(monkeypatch):
    monkeypatch.setenv("DEDALO_SECRET_OPENAI_KEY", "env-key-456")
    assert "openai-key" in secrets.names()
    assert secrets.get("openai-key") == "env-key-456"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "DIR", tmp_path / "secrets")
    monkeypatch.setattr(secrets, "KEY_FILE", tmp_path / "master.key")
    monkeypatch.delenv("DEDALO_MASTER_KEY", raising=False)
    monkeypatch.delenv("DEDALO_MASTER_KEY_FILE", raising=False)
    return tmp_path


def test_secret_encrypted_on_disk(store):
    secrets.set("api-token", "valor-super-secreto")
    raw = (store / "secrets" / "api-token.secret").read_text()
    assert raw.startswith(secrets.PREFIX)
    assert "valor-super-secreto" not in raw
    assert secrets.get("api-token") == "valor-super-secreto"
    assert oct((store / "secrets" / "api-token.secret").stat().st_mode & 0o777) == "0o600"
    assert oct((store / "master.key").stat().st_mode & 0o777) == "0o600"


def test_legacy_plaintext_readable_and_migrated(store):
    d = store / "secrets"
    d.mkdir()
    (d / "viejo.secret").write_text("en-claro\n")
    assert secrets.get("viejo") == "en-claro"  # compatibilidad con ficheros antiguos
    assert secrets.migrate() == 1
    assert (d / "viejo.secret").read_text().startswith(secrets.PREFIX)
    assert secrets.get("viejo") == "en-claro"
    assert secrets.migrate() == 0  # idempotente


def test_master_key_from_env(store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DEDALO_MASTER_KEY", Fernet.generate_key().decode())
    secrets.set("tok", "abc")
    assert not (store / "master.key").exists()
    assert secrets.get("tok") == "abc"


def test_wrong_master_key_gives_clear_error(store, monkeypatch):
    from cryptography.fernet import Fernet

    secrets.set("tok", "abc")
    monkeypatch.setenv("DEDALO_MASTER_KEY", Fernet.generate_key().decode())
    with pytest.raises(secrets.SecretError, match="clave maestra"):
        secrets.get("tok")


def test_invalid_master_key(store, monkeypatch):
    monkeypatch.setenv("DEDALO_MASTER_KEY", "no-es-una-clave")
    with pytest.raises(secrets.SecretError, match="no válida"):
        secrets.set("tok", "abc")
