"""compile() y ast.parse() serializados entre hilos.

CPython no garantiza que construir un AST sea seguro cuando varios hilos lo hacen a la vez:
con sub-flujos en paralelo o flujos programados que coinciden, aparecía
"AST constructor recursion depth mismatch". Toda compilación de código Python de Dédalo
pasa por aquí. El candado solo cubre la compilación (milisegundos), nunca la ejecución.
"""

from __future__ import annotations

import ast
import threading
from types import CodeType

_LOCK = threading.RLock()


def parse(source: str, filename: str = "<unknown>", mode: str = "exec") -> ast.AST:
    with _LOCK:
        return ast.parse(source, filename, mode)


def compile_code(source: str, filename: str, mode: str = "exec") -> CodeType:
    with _LOCK:
        return compile(source, filename, mode)
