"""Tests para la gestión multi-endpoint de LLMs en Dédalo."""

import pytest
from fastapi.testclient import TestClient

from dedalo import compiler, llms, runtime, secrets
from dedalo.app import app

client = TestClient(app)


def test_set_and_get_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)

    profile = {
        "provider": "ollama",
        "url": "http://localhost:11434/v1",
        "model": "qwen2.5-coder:7b",
        "secret": "ollama-token",
        "max_context": 32768,
        "temperature": 0.2,
    }
    saved = llms.set_llm("ollama-local", profile)
    assert saved["name"] == "ollama-local"
    assert saved["provider"] == "ollama"
    assert saved["model"] == "qwen2.5-coder:7b"
    assert saved["max_context"] == 32768
    assert saved["temperature"] == 0.2

    assert "ollama-local" in llms.names()
    retrieved = llms.get("ollama-local")
    assert retrieved is not None
    assert retrieved["model"] == "qwen2.5-coder:7b"
    assert retrieved["max_context"] == 32768

    all_profiles = llms.list_all()
    assert len(all_profiles) == 1
    assert all_profiles[0]["name"] == "ollama-local"

    deleted = llms.delete("ollama-local")
    assert deleted is True
    assert "ollama-local" not in llms.names()
    assert llms.get("ollama-local") is None


def test_invalid_llm_name():
    with pytest.raises(llms.LLMConfigError):
        llms.set_llm("../escape", {})

    with pytest.raises(llms.LLMConfigError):
        llms.set_llm("MAYUSCULAS", {})

    with pytest.raises(llms.LLMConfigError):
        llms.set_llm("con espacios", {})


def test_resolve_fallback(monkeypatch):
    monkeypatch.setenv("DEDALO_LLM_URL", "http://fallback:8000/v1")
    monkeypatch.setenv("DEDALO_LLM_MODEL", "fallback-model")
    monkeypatch.setenv("DEDALO_LLM_KEY", "fallback-key")

    cfg = llms.resolve()
    assert cfg["url"] == "http://fallback:8000/v1"
    assert cfg["model"] == "fallback-model"
    assert cfg["api_key"] == "fallback-key"
    assert cfg["max_context"] == 0


def test_resolve_profile_and_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)
    monkeypatch.setattr(secrets, "DIR", tmp_path / "secrets")

    secrets.set("openrouter-key", "sk-or-v1-secret-token")

    llms.set_llm(
        "openrouter-claude",
        {
            "provider": "openrouter",
            "url": "https://openrouter.ai/api/v1",
            "model": "anthropic/claude-3.5-sonnet",
            "secret": "openrouter-key",
            "max_context": 160000,
            "temperature": 0.3,
        },
    )

    resolved = llms.resolve("openrouter-claude")
    assert resolved["url"] == "https://openrouter.ai/api/v1"
    assert resolved["model"] == "anthropic/claude-3.5-sonnet"
    assert resolved["api_key"] == "sk-or-v1-secret-token"
    assert resolved["secret"] == "openrouter-key"
    assert resolved["max_context"] == 160000
    assert resolved["temperature"] == 0.3


def test_resolve_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)

    llms.set_llm(
        "local-endpoint",
        {
            "url": "http://localhost:11434/v1",
            "model": "llama3",
            "max_context": 8192,
            "temperature": 0.7,
        },
    )

    resolved = llms.resolve(
        "local-endpoint",
        overrides={
            "model": "codellama:13b",
            "temperature": 0.1,
            "max_context": 16384,
        },
    )
    assert resolved["url"] == "http://localhost:11434/v1"
    assert resolved["model"] == "codellama:13b"
    assert resolved["max_context"] == 16384
    assert resolved["temperature"] == 0.1


def test_api_llms_crud(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)

    # 1. Crear perfil
    res = client.post(
        "/api/llms",
        json={
            "name": "vllm-mistral",
            "provider": "vllm",
            "url": "http://vllm.internal:8000/v1",
            "model": "mistral-7b-instruct",
            "max_context": 32000,
            "temperature": 0.5,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["llm"]["name"] == "vllm-mistral"

    # 2. Listar
    res_list = client.get("/api/llms")
    assert res_list.status_code == 200
    items = res_list.json()
    assert len(items) == 1
    assert items[0]["name"] == "vllm-mistral"

    # 3. Obtener individual
    res_get = client.get("/api/llms/vllm-mistral")
    assert res_get.status_code == 200
    assert res_get.json()["model"] == "mistral-7b-instruct"

    # 4. Catalog choices
    res_cat = client.get("/api/catalog")
    assert res_cat.status_code == 200
    llm_choices = res_cat.json()["choices"]["llms"]
    assert any(c["value"] == "vllm-mistral" for c in llm_choices)

    # 5. Eliminar
    res_del = client.delete("/api/llms/vllm-mistral")
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

    res_get404 = client.get("/api/llms/vllm-mistral")
    assert res_get404.status_code == 404


def test_compiler_node_specs_with_llm():
    spec_agent = compiler.NODE_TYPES["transform.agent"]
    assert "llm" in spec_agent.optional
    assert spec_agent.choices_from.get("llm") == "llms"

    spec_coder = compiler.NODE_TYPES["transform.coder"]
    assert "llm" in spec_coder.optional
    assert spec_coder.choices_from.get("llm") == "llms"

    spec_summ = compiler.NODE_TYPES["transform.summarize"]
    assert "llm" in spec_summ.optional
    assert spec_summ.choices_from.get("llm") == "llms"


def test_agent_execution_with_llm_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)

    llms.set_llm(
        "mock-llm",
        {
            "url": "http://mock-llm:8000/v1",
            "model": "mock-deepseek",
            "temperature": 0.4,
        },
    )

    captured_call = {}

    def fake_call_llm(prompt, system="", **kwargs):
        captured_call["prompt"] = prompt
        captured_call["system"] = system
        captured_call.update(kwargs)
        return "respuesta mock"

    monkeypatch.setattr(runtime, "call_llm", fake_call_llm)

    res = runtime.agent(None, [], prompt="Prueba agente", llm="mock-llm")
    assert res.status == "ok"
    assert res.data == "respuesta mock"
    assert captured_call["model"] == "mock-deepseek"
    assert captured_call["url"] == "http://mock-llm:8000/v1"
    assert captured_call["temperature"] == 0.4
    assert captured_call["llm"] == "mock-llm"


def test_summarize_execution_with_llm_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(llms, "DIR", tmp_path)

    llms.set_llm(
        "mock-summ",
        {
            "url": "http://mock-summ:8000/v1",
            "model": "mock-qwen",
        },
    )

    captured_call = {}

    def fake_call_llm(prompt, system="", **kwargs):
        captured_call["prompt"] = prompt
        captured_call.update(kwargs)
        return "resumen mock"

    monkeypatch.setattr(runtime, "call_llm", fake_call_llm)

    inp = [runtime.NodeResult("ok", data="Texto largo a resumir.")]
    res = runtime.summarize_text(None, inp, engine="llm", llm="mock-summ")
    assert res.status == "ok"
    assert res.data == "resumen mock"
    assert captured_call["llm"] == "mock-summ"
    assert captured_call["prompt"] == "Texto largo a resumir."
