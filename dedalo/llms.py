"""Gestión de endpoints y configuraciones de LLMs para Dédalo.

Un fichero JSON por configuración en `data/llms/<nombre>.json` o
definición de fallback mediante variables de entorno `DEDALO_LLM_*`.
"""

from __future__ import annotations

import builtins
import json
import os
import re
from pathlib import Path
from typing import Any

from dedalo import secrets
from dedalo.i18n import tr

DIR = Path(os.environ.get("DEDALO_LLMS_DIR", "data/llms"))

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SUFFIX = ".json"


class LLMConfigError(Exception):
    """Error al validar o manipular una configuración de LLM."""


def _path(name: str) -> Path:
    clean = (name or "").strip()
    if not _NAME.match(clean):
        raise LLMConfigError(
            tr('invalid LLM configuration name: {name!r} (lowercase letters, numbers, hyphen and underscore only)', name=name)
        )
    return DIR / f"{clean}{SUFFIX}"


def names() -> list[str]:
    """Lista todos los nombres de configuraciones LLM disponibles en disco."""
    found: set[str] = builtins.set()
    if DIR.is_dir():
        for p in DIR.iterdir():
            if p.is_file() and p.name.endswith(SUFFIX):
                candidate = p.name[: -len(SUFFIX)]
                if _NAME.match(candidate):
                    found.add(candidate)
    return sorted(found)


def list_all() -> list[dict[str, Any]]:
    """Devuelve la lista completa de configuraciones LLM guardadas."""
    items = []
    for name in names():
        conf = get(name)
        if conf:
            items.append(conf)
    return items


def get(name: str) -> dict[str, Any] | None:
    """Devuelve la configuración del LLM indicado, o None si no existe."""
    if not name or not name.strip():
        return None
    try:
        p = _path(name)
    except LLMConfigError:
        return None

    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data["name"] = name.strip().lower()
            return data
        return None
    except (OSError, json.JSONDecodeError):
        return None


def set_llm(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Guarda una configuración de LLM en disco."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise LLMConfigError(tr('the LLM configuration name cannot be empty'))

    p = _path(clean_name)
    DIR.mkdir(parents=True, exist_ok=True)

    url = str(config.get("url") or "http://localhost:11434/v1").strip()
    model = str(config.get("model") or "llama3").strip()
    secret = str(config.get("secret") or "").strip()
    provider = str(config.get("provider") or "").strip().lower()
    max_context = int(config.get("max_context") or 0)
    temperature = float(config.get("temperature") if config.get("temperature") is not None else 0.7)

    payload = {
        "name": clean_name,
        "provider": provider or "custom",
        "url": url,
        "model": model,
        "secret": secret,
        "max_context": max_context,
        "temperature": temperature,
    }

    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return payload


# Alias para ergonomía
set = set_llm


def delete(name: str) -> bool:
    """Elimina la configuración de LLM del disco."""
    try:
        p = _path(name)
    except LLMConfigError:
        return False

    if p.is_file():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


def resolve(name: str = "", overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resuelve la configuración efectiva para una llamada LLM.

    Aplica en cascada:
    1. Valores base del entorno global (DEDALO_LLM_*).
    2. Valores del perfil de LLM nombrado (si se especifica y existe).
    3. Overrides directos pasados desde la llamada o el nodo.
    4. Resolución de la API Key a través de `secrets.get(secret)`.
    """
    default_url = (os.environ.get("DEDALO_LLM_URL") or "http://localhost:11434/v1").rstrip("/")
    default_model = os.environ.get("DEDALO_LLM_MODEL") or "llama3"
    default_key = os.environ.get("DEDALO_LLM_KEY", "")

    url = default_url
    model = default_model
    secret_name = ""
    api_key = default_key
    max_context = 0
    temperature = 0.7
    provider = ""

    profile_name = (name or "").strip().lower()
    if profile_name:
        profile = get(profile_name)
        if profile:
            if profile.get("url"):
                url = str(profile["url"]).strip().rstrip("/")
            if profile.get("model"):
                model = str(profile["model"]).strip()
            if profile.get("secret"):
                secret_name = str(profile["secret"]).strip()
            if profile.get("max_context"):
                max_context = int(profile["max_context"])
            if profile.get("temperature") is not None:
                temperature = float(profile["temperature"])
            if profile.get("provider"):
                provider = str(profile["provider"]).strip()

    if overrides:
        if overrides.get("url"):
            url = str(overrides["url"]).strip().rstrip("/")
        if overrides.get("model"):
            model = str(overrides["model"]).strip()
        if overrides.get("secret"):
            secret_name = str(overrides["secret"]).strip()
        if overrides.get("max_context") is not None and int(overrides.get("max_context") or 0) > 0:
            max_context = int(overrides["max_context"])
        if overrides.get("temperature") is not None:
            temperature = float(overrides["temperature"])

    # Resolver API Key del secreto si está definido
    if secret_name:
        sec_val = secrets.get(secret_name)
        if sec_val:
            api_key = sec_val

    return {
        "name": profile_name or "default",
        "provider": provider,
        "url": url,
        "model": model,
        "secret": secret_name,
        "api_key": api_key,
        "max_context": max_context,
        "temperature": temperature,
    }
