"""Gestión de secretos y credenciales para integraciones de Dédalo.

Un fichero por secreto en `data/secrets/<nombre>.secret` con permisos estrictos (0600),
o mediante variables de entorno `DEDALO_SECRET_<NOMBRE>`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DIR = Path(os.environ.get("DEDALO_SECRETS_DIR", "data/secrets"))

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SUFFIX = ".secret"


class SecretError(Exception):
    """Error al validar o manipular un secreto."""


def _path(name: str) -> Path:
    if not _NAME.match(name or ""):
        raise SecretError(
            f"nombre de secreto no válido: {name!r} (solo minúsculas, números, guión y guión bajo)"
        )
    return DIR / f"{name}{SUFFIX}"


def _env_key(name: str) -> str:
    return f"DEDALO_SECRET_{name.upper().replace('-', '_')}"


def names() -> list[str]:
    """Lista todos los nombres de secretos disponibles (en disco o en el entorno)."""
    found: set[str] = set()
    if DIR.is_dir():
        for p in DIR.iterdir():
            if p.is_file() and p.name.endswith(SUFFIX):
                candidate = p.name[: -len(SUFFIX)]
                if _NAME.match(candidate):
                    found.add(candidate)

    prefix = "DEDALO_SECRET_"
    for key in os.environ:
        if key.startswith(prefix) and len(key) > len(prefix):
            name = key[len(prefix) :].lower().replace("_", "-")
            if _NAME.match(name):
                found.add(name)

    return sorted(found)


def get(name: str) -> str | None:
    """Devuelve el contenido del secreto. El entorno prevalece sobre el fichero."""
    env_val = os.environ.get(_env_key(name))
    if env_val is not None and env_val != "":
        return env_val

    p = _path(name)
    if not p.is_file():
        return None
    try:
        val = p.read_text(encoding="utf-8").strip()
        return val if val else None
    except OSError:
        return None


def set(name: str, value: str) -> None:
    """Guarda un secreto en disco con permisos 0600."""
    val = (value or "").strip()
    if not val:
        raise SecretError("el valor del secreto no puede estar vacío")

    DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name)
    p.write_text(val, encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def delete(name: str) -> bool:
    """Elimina el secreto del disco si existe."""
    p = _path(name)
    if p.is_file():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False
