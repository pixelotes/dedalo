"""Gestión de secretos y credenciales para integraciones de Dédalo.

Un fichero por secreto en `data/secrets/<nombre>.secret` con permisos estrictos (0600),
o mediante variables de entorno `DEDALO_SECRET_<NOMBRE>`.

Los ficheros se cifran en reposo con Fernet (AES-128-CBC + HMAC-SHA256). La clave maestra sale de
`DEDALO_MASTER_KEY` o `DEDALO_MASTER_KEY_FILE`; si no hay ninguna, se genera `data/master.key` (0600).
Los ficheros en claro de versiones anteriores se siguen leyendo y `migrate()` los cifra. La librería
`cryptography` solo se importa al tocar un secreto en disco: los flujos que no los usan no pagan nada.
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import tempfile
from pathlib import Path
from dedalo.i18n import tr

log = logging.getLogger("dedalo.secrets")

DIR = Path(os.environ.get("DEDALO_SECRETS_DIR", "data/secrets"))
KEY_FILE = Path(os.environ.get("DEDALO_KEY_FILE", "data/master.key"))
PREFIX = "dedalo:v1:"

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SUFFIX = ".secret"


class SecretError(Exception):
    """Error al validar o manipular un secreto."""


_fernet_cache: tuple[object, object] | None = None


def _master_key() -> bytes:
    """Clave maestra: entorno > fichero indicado > fichero generado automáticamente."""
    env = os.environ.get("DEDALO_MASTER_KEY", "").strip()
    if env:
        return env.encode()
    key_path = os.environ.get("DEDALO_MASTER_KEY_FILE", "").strip()
    if key_path:
        try:
            return Path(key_path).read_bytes().strip()
        except OSError as exc:
            raise SecretError(tr('could not read DEDALO_MASTER_KEY_FILE: {exc}', exc=exc)) from exc

    if not KEY_FILE.is_file():
        from cryptography.fernet import Fernet

        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # otro proceso la acaba de crear
        else:
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
    return KEY_FILE.read_bytes().strip()


def _fernet():
    """Instancia de Fernet creada una sola vez por proceso (y por clave configurada)."""
    global _fernet_cache
    marker = (
        os.environ.get("DEDALO_MASTER_KEY"),
        os.environ.get("DEDALO_MASTER_KEY_FILE"),
        str(KEY_FILE),
    )
    if _fernet_cache is None or _fernet_cache[0] != marker:
        from cryptography.fernet import Fernet

        try:
            _fernet_cache = (marker, Fernet(_master_key()))
        except ValueError as exc:
            raise SecretError(
                tr('invalid master key: it must be a Fernet key (32 bytes in url-safe base64)')
            ) from exc
    return _fernet_cache[1]


def encrypt(value: str) -> str:
    """Cifra un texto y devuelve un token con prefijo `dedalo:v1:`."""
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Descifra un token de `encrypt()`. Un texto sin prefijo se devuelve tal cual (formato antiguo)."""
    if not token.startswith(PREFIX):
        return token
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError(
            tr('could not decrypt the secret: the master key does not match the one used when it was saved')
        ) from exc


def _write_private(p: Path, text: str) -> None:
    """Escritura atómica con permisos 0600 desde el primer byte (sin ventana legible)."""
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _path(name: str) -> Path:
    if not _NAME.match(name or ""):
        raise SecretError(
            tr('invalid secret name: {name!r} (lowercase letters, numbers, hyphen and underscore only)', name=name)
        )
    return DIR / f"{name}{SUFFIX}"


def _env_key(name: str) -> str:
    return f"DEDALO_SECRET_{name.upper().replace('-', '_')}"


def names() -> list[str]:
    """Lista todos los nombres de secretos disponibles (en disco o en el entorno)."""
    found: set[str] = builtins.set()
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
    except OSError:
        return None
    if not val:
        return None
    return decrypt(val) or None


def set_secret(name: str, value: str) -> None:
    """Guarda un secreto en disco con permisos 0600."""
    val = (value or "").strip()
    if not val:
        raise SecretError(tr('the secret value cannot be empty'))

    p = _path(name)
    DIR.mkdir(parents=True, exist_ok=True)
    try:
        DIR.chmod(0o700)
    except OSError:
        pass
    _write_private(p, encrypt(val))


# Alias para retrocompatibilidad
set = set_secret


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


def migrate() -> int:
    """Cifra los secretos en claro de versiones anteriores. Devuelve cuántos ha convertido."""
    if not DIR.is_dir():
        return 0
    done = 0
    for p in DIR.glob(f"*{SUFFIX}"):
        try:
            val = p.read_text(encoding="utf-8").strip()
            if val and not val.startswith(PREFIX):
                _write_private(p, encrypt(val))
                done += 1
        except (OSError, SecretError) as exc:
            log.warning(tr('could not encrypt %s: %s'), p.name, exc)
    if done:
        log.info(tr('encrypted %d secrets that were stored in plaintext'), done)
    return done
