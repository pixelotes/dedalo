"""Internacionalización de los textos del servidor.

Los textos del código están en inglés (el idioma por defecto) y actúan de identificador: el catálogo
`locales/<idioma>.json` mapea cada texto en inglés a su traducción. El inglés no carga ningún fichero.

El idioma de una petición viene de la cabecera `X-Dedalo-Lang` (la envía la interfaz); sin ella, de
`DEDALO_LANG` (por defecto `en`). Las ejecuciones planificadas y los webhooks usan `DEDALO_LANG`.
"""

from __future__ import annotations

import contextvars
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).parent / "locales"
FALLBACK = "en"

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("dedalo_lang", default=None)


def available() -> list[str]:
    """Idiomas disponibles: el inglés y un fichero por cada catálogo en `locales/`."""
    return sorted({FALLBACK, *(p.stem for p in LOCALES_DIR.glob("*.json"))})


def normalize(code: str | None) -> str | None:
    """Reduce `es-ES` o `ES` a un idioma disponible; devuelve None si no hay catálogo."""
    primary = (code or "").strip().lower().replace("_", "-").split("-")[0]
    return primary if primary in available() else None


def default_lang() -> str:
    return normalize(os.environ.get("DEDALO_LANG")) or FALLBACK


def current() -> str:
    return _current.get() or default_lang()


def set_lang(code: str | None) -> contextvars.Token:
    return _current.set(normalize(code))


def reset(token: contextvars.Token) -> None:
    _current.reset(token)


@lru_cache(maxsize=8)
def _catalog(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if lang == FALLBACK or not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gettext(msg: str, lang: str | None = None) -> str:
    """Traducción de `msg` (texto en inglés) o el propio `msg` si no existe."""
    return _catalog(lang or current()).get(msg, msg)


def tr(msg: str, /, **params: Any) -> str:
    """Traduce `msg` e interpola `{nombre}`. Sin parámetros no se formatea (las llaves son literales)."""
    text = gettext(msg)
    return text.format(**params) if params else text


def translate_tree(obj: Any, lang: str | None = None) -> Any:
    """Copia de `obj` (dict/list/str) con todas las cadenas traducidas cuando hay traducción."""
    lang = lang or current()
    if lang == FALLBACK:
        return obj
    if isinstance(obj, str):
        return gettext(obj, lang)
    if isinstance(obj, list):
        return [translate_tree(v, lang) for v in obj]
    if isinstance(obj, dict):
        return {k: translate_tree(v, lang) for k, v in obj.items()}
    return obj


class LangMiddleware:
    """ASGI: fija el idioma de la petición a partir de `X-Dedalo-Lang`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        code = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"x-dedalo-lang"), None)
        token = set_lang(code)
        try:
            await self.app(scope, receive, send)
        finally:
            reset(token)
