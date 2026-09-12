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
