"""Ejecución de comandos de shell con entorno filtrado y aislamiento opcional con bubblewrap.

- Entorno: los subprocesos no heredan `DEDALO_SECRET_*`, `DEDALO_MASTER_KEY*`, `DEDALO_LLM_KEY` ni
  `DEDALO_TELEGRAM_*` (más los patrones de `DEDALO_ENV_DENY`, separados por comas).
- Aislamiento (`DEDALO_SANDBOX`): `off` | `auto` (bwrap si funciona, si no sin aislar) | `on` (obligatorio,
  falla si no hay bwrap). Por defecto: `off` para los nodos de comando y `auto` para el `bash` del agente
  `transform.coder`. Un nodo puede pedir `on`, pero nunca desactivar un `on` global.
- Dentro del sandbox solo se ve `/usr`, `/bin`, `/lib*` y `/etc` en solo lectura, un `/tmp` vacío y el
  directorio de trabajo indicado (lectura y escritura). `data/` queda oculto. Extras:
  `DEDALO_SANDBOX_RO` / `DEDALO_SANDBOX_RW` (rutas separadas por `:`) y `DEDALO_SANDBOX_NET=deny`.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

from dedalo import secrets
from dedalo.i18n import tr

log = logging.getLogger("dedalo.sandbox")

# Dédalo secrets plus the bootstrap credentials of external secret stores (op://, vault://)
_SECRET_ENV = (
    "DEDALO_SECRET_*", "DEDALO_MASTER_KEY*", "DEDALO_LLM_KEY", "DEDALO_TELEGRAM_*",
    "OP_*", "VAULT_*", "BAO_*", "BWS_*",
)
_DEFAULT_MODE = {"command": "off", "coder": "auto"}
_SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")

_probe: dict[bool, bool] = {}
_warned = False


class SandboxUnavailable(Exception):
    """Se exige aislamiento (`on`) y bwrap no está disponible o no funciona."""


def clean_env() -> dict[str, str]:
    """Entorno actual sin las variables que contienen secretos de Dédalo."""
    extra = [p.strip() for p in os.environ.get("DEDALO_ENV_DENY", "").split(",") if p.strip()]
    allow = [p.strip() for p in os.environ.get("DEDALO_ENV_ALLOW", "").split(",") if p.strip()]
    patterns = (*_SECRET_ENV, *extra)

    def hidden(key: str) -> bool:
        return any(fnmatch.fnmatchcase(key, p) for p in patterns) and not any(fnmatch.fnmatchcase(key, a) for a in allow)

    return {k: v for k, v in os.environ.items() if not hidden(k)}


def effective_mode(kind: str, node_mode: str = "") -> str:
    """Modo final (`off`, `auto` u `on`) a partir de la variable global y la opción del nodo."""
    glob = os.environ.get("DEDALO_SANDBOX", "").strip().lower() or _DEFAULT_MODE.get(kind, "off")
    node = (node_mode or "").strip().lower()
    if glob not in ("off", "auto", "on"):
        log.warning(tr('DEDALO_SANDBOX=%r is not valid (off|auto|on); isolation is enforced'), glob)
        glob = "on"
    if glob == "on" or node == "on":
        return "on"
    if node == "off":
        return "off"
    return glob


def _net_allowed() -> bool:
    return os.environ.get("DEDALO_SANDBOX_NET", "allow").strip().lower() != "deny"


def _paths(var: str) -> list[str]:
    return [p for p in os.environ.get(var, "").split(":") if p.strip()]


def _protected() -> list[Path]:
    """Rutas con datos sensibles que no deben verse aunque queden dentro del directorio de trabajo."""
    return [Path("data").resolve(), secrets.DIR.resolve(), secrets.KEY_FILE.resolve()]


def build_argv(cmd: str, cwd: str | Path | None, exe: str = "bwrap") -> list[str]:
    """Línea de comandos de bwrap que ejecuta `cmd` con `/bin/sh -c`."""
    argv = [exe, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    argv += ["--cap-drop", "ALL"]
    if not _net_allowed():
        argv.append("--unshare-net")
    for p in _SYSTEM_DIRS:
        if os.path.islink(p):
            argv += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            argv += ["--ro-bind", p, p]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for p in _paths("DEDALO_SANDBOX_RO"):
        argv += ["--ro-bind-try", p, p]
    for p in _paths("DEDALO_SANDBOX_RW"):
        argv += ["--bind-try", p, p]

    workdir = "/tmp"
    if cwd:
        wd = Path(cwd).resolve()
        workdir = str(wd)
        argv += ["--bind", workdir, workdir]
        covered: list[Path] = []
        for hidden in sorted(_protected(), key=lambda h: len(h.parts)):
            if hidden == wd or wd not in hidden.parents or not hidden.exists():
                continue
            if any(c in hidden.parents or c == hidden for c in covered):
                continue  # ya oculto por un directorio superior
            covered.append(hidden)
            argv += ["--tmpfs", str(hidden)] if hidden.is_dir() else ["--ro-bind", "/dev/null", str(hidden)]
    argv += ["--setenv", "HOME", "/tmp", "--chdir", workdir, "--", "/bin/sh", "-c", cmd]
    return argv


def available() -> bool:
    """Indica si bwrap está instalado y puede crear el aislamiento (se comprueba una vez por proceso)."""
    net = _net_allowed()
    if net not in _probe:
        exe = shutil.which("bwrap")
        ok = False
        if exe:
            try:
                r = subprocess.run(build_argv("true", None, exe), capture_output=True, timeout=10)
                ok = r.returncode == 0
                if not ok:
                    log.warning(tr('bwrap cannot isolate in this environment: %s'), r.stderr.decode(errors="replace").strip())
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning(tr('could not test bwrap: %s'), exc)
        _probe[net] = ok
    return _probe[net]


def status() -> dict[str, object]:
    """Estado para la API: modos por defecto y si bwrap funciona."""
    return {
        "command": effective_mode("command"),
        "coder": effective_mode("coder"),
        "available": available() if shutil.which("bwrap") else False,
    }


def run_shell(
    cmd: str,
    *,
    kind: str = "command",
    timeout: float = 15.0,
    cwd: str | Path | None = None,
    mode: str = "",
) -> subprocess.CompletedProcess:
    """Ejecuta `cmd` en `/bin/sh -c` con entorno filtrado y, según el modo, dentro de bwrap."""
    global _warned
    env = clean_env()
    m = effective_mode(kind, mode)
    if m != "off":
        if available():
            exe = shutil.which("bwrap") or "bwrap"
            return subprocess.run(
                build_argv(cmd, cwd, exe), capture_output=True, text=True, timeout=timeout, env=env
            )
        if m == "on":
            raise SandboxUnavailable(tr('isolation is enabled but bubblewrap (bwrap) is not available'))
        if not _warned:
            _warned = True
            log.warning(tr('sandbox in auto mode without a working bwrap: commands run without isolation'))
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
