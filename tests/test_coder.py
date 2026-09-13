"""Pruebas unitarias para el nodo transform.coder, microarnés ReAct, herramientas y compactación Luhn+."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from dedalo import compiler
from dedalo.templates import get_template
from dedalo.runtime import (
    _compact_conversation_history,
    _detect_context_window,
    _estimate_messages_tokens,
    _tool_bash,
    _tool_edit,
    _tool_find,
    _tool_grep,
    _tool_ls,
    _tool_read,
    _tool_write,
    coder_agent,
)


def test_tool_read(tmp_path: Path):
    f = tmp_path / "ejemplo.py"
    f.write_text("linea 1\nlinea 2\nlinea 3\nlinea 4\nlinea 5\n", encoding="utf-8")

    out_all = _tool_read(tmp_path, "ejemplo.py")
    assert "linea 1" in out_all
    assert "linea 5" in out_all
    assert "líneas 1-5 de 5" in out_all

    out_range = _tool_read(tmp_path, "ejemplo.py", start_line=2, end_line=3)
    assert "linea 2" in out_range
    assert "linea 3" in out_range
    assert "linea 1" not in out_range
    assert "linea 4" not in out_range

    out_missing = _tool_read(tmp_path, "no_existe.py")
    assert "no existe" in out_missing

    (tmp_path / "subdir").mkdir()
    out_dir = _tool_read(tmp_path, "subdir")
    assert "es un directorio" in out_dir


def test_tool_write_and_edit(tmp_path: Path):
    out_w = _tool_write(tmp_path, "pkg/modulo.py", "def funcion():\n    return 42\n")
    assert "guardado" in out_w
    assert (tmp_path / "pkg/modulo.py").exists()

    out_e = _tool_edit(
        tmp_path,
        "pkg/modulo.py",
        old_str="return 42",
        new_str="return 100",
    )
    assert "editado correctamente" in out_e
    assert "return 100" in (tmp_path / "pkg/modulo.py").read_text()

    out_not_found = _tool_edit(
        tmp_path,
        "pkg/modulo.py",
        old_str="return 999",
        new_str="return 0",
    )
    assert "No se encontró 'old_str'" in out_not_found

    (tmp_path / "repetido.txt").write_text("abc\nabc\n", encoding="utf-8")
    out_ambiguous = _tool_edit(
        tmp_path,
        "repetido.txt",
        old_str="abc",
        new_str="xyz",
    )
    assert "aparece 2 veces" in out_ambiguous


def test_tool_bash(tmp_path: Path):
    out = _tool_bash(tmp_path, "echo 'prueba dedalo'")
    assert "prueba dedalo" in out
    assert "[Código de salida: 0]" in out

    out_err = _tool_bash(tmp_path, "python3 -c 'import sys; sys.exit(42)'")
    assert "[Código de salida: 42]" in out_err


def test_tool_find_grep_ls(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "alpha.py").write_text("TOKEN_SECRETO = 'xyz123'\n", encoding="utf-8")
    (tmp_path / "src" / "beta.txt").write_text("documentacion de prueba\n", encoding="utf-8")

    ls_out = _tool_ls(tmp_path, "src")
    assert "alpha.py" in ls_out
    assert "beta.txt" in ls_out

    find_out = _tool_find(tmp_path, pattern="*.py", path="src")
    assert "alpha.py" in find_out
    assert "beta.txt" not in find_out

    grep_out = _tool_grep(tmp_path, pattern="TOKEN_SECRETO", path="src")
    assert "alpha.py" in grep_out
    assert "xyz123" in grep_out


def test_sandboxing_path_traversal(tmp_path: Path):
    outside_file = tmp_path.parent / "privado.txt"
    outside_file.write_text("secreto", encoding="utf-8")

    out_read = _tool_read(tmp_path, "../privado.txt")
    assert "Error de seguridad" in out_read

    out_write = _tool_write(tmp_path, "../malicioso.txt", "hacked")
    assert "Error de seguridad" in out_write

    out_edit = _tool_edit(tmp_path, "../privado.txt", "secreto", "publico")
    assert "Error de seguridad" in out_edit


def test_detect_context_window():
    assert _detect_context_window("http://localhost:11434/v1", "gpt-4o") == 128000
    assert _detect_context_window("http://localhost:11434/v1", "claude-3-5-sonnet") == 200000
    assert _detect_context_window("http://localhost:11434/v1", "qwen2.5-coder:7b") == 32768
    assert _detect_context_window("http://localhost:11434/v1", "llama3:8b") == 8192
    assert _detect_context_window("http://localhost:11434/v1", "llama-3.1:8b") == 131072
    assert _detect_context_window("http://localhost:11434/v1", "gpt-4o", explicit_ctx=16384) == 16384
    assert _detect_context_window("http://localhost:11434/v1", "modelo-custom-desconocido") == 8192


def test_compact_conversation_history_with_luhn():
    long_text = " ".join([
        f"El sistema analizó el archivo {i} encontrando métricas de rendimiento estables y validación de seguridad."
        for i in range(100)
    ])
    messages = [
        {"role": "system", "content": "Eres un asistente."},
        {"role": "user", "content": "Audita el repositorio."},
        {"role": "assistant", "content": "Voy a leer los archivos."},
        {"role": "tool", "name": "read", "content": long_text},
        {"role": "assistant", "content": "Continuando con la auditoría."},
        {"role": "tool", "name": "ls", "content": "src/ main.py tests/"},
        {"role": "assistant", "content": "¿Qué más necesitas?", "tool_calls": []},
    ]

    tokens_before = _estimate_messages_tokens(messages)
    assert tokens_before > 180

    compacted, did_compact = _compact_conversation_history(messages, context_window=200)
    assert did_compact is True

    assert compacted[0]["role"] == "system"
    assert compacted[1]["content"] == "Audita el repositorio."

    summary_msg = compacted[2]
    assert summary_msg["role"] == "user"
    assert "Memoria resumida del progreso previo mediante Luhn+" in summary_msg["content"]

    tokens_after = _estimate_messages_tokens(compacted)
    assert tokens_after < tokens_before


def test_coder_agent_react_loop_mock(tmp_path: Path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    responses = [
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Voy a leer main.py",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "main.py"}),
                            },
                        }],
                    },
                }],
            },
        ),
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Modifico el valor",
                        "tool_calls": [{
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "edit",
                                "arguments": json.dumps({
                                    "path": "main.py",
                                    "old_str": "x = 1",
                                    "new_str": "x = 100",
                                }),
                            },
                        }],
                    },
                }],
            },
        ),
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Se actualizó main.py con x = 100 exitosamente.",
                        "tool_calls": None,
                    },
                }],
            },
        ),
    ]

    with patch("httpx.Client.post", side_effect=responses):
        res = coder_agent(
            None,
            [],
            prompt="Actualiza x a 100 en main.py",
            workspace_dir=str(tmp_path),
            tools_preset="coding",
            max_turns=5,
            max_context=32768,
            model="qwen2.5-coder",
        )

    assert res.ok is True
    assert res.data["completed"] is True
    assert res.data["turns_used"] == 3
    assert "main.py" in res.data["modified_files"]
    assert "Se actualizó main.py" in res.data["response"]
    assert (tmp_path / "main.py").read_text() == "x = 100\n"


def test_compiler_transform_coder():
    graph_dict = {
        "name": "test-coder-flow",
        "nodes": [
            {
                "id": "coder_node",
                "type": "transform.coder",
                "config": {
                    "prompt": "Corrige el archivo bug.py",
                    "workspace_dir": ".",
                    "tools_preset": "coding",
                    "max_turns": 4,
                },
            }
        ],
        "edges": [],
    }
    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_coder_node" in code
    assert "runtime.coder_agent" in code


def test_coder_agent_triggers_luhn_compaction(tmp_path: Path):
    large_code = "\n".join([f"def funcion_larga_{i}(): return {i}" for i in range(150)])
    (tmp_path / "gigante.py").write_text(large_code, encoding="utf-8")

    responses = [
        # Turno 1: el modelo llama a read(path="gigante.py")
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Leyendo archivo gigante",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "gigante.py"}),
                            },
                        }],
                    },
                }],
            },
        ),
        # Turno 2: tras recibir el archivo gigante, el contexto supera el 90% de 500 tokens
        # El arnés compacta con Luhn+ antes de llamar a la API y el modelo concluye
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Archivo analizado y compactado correctamente.",
                        "tool_calls": None,
                    },
                }],
            },
        ),
    ]

    with patch("httpx.Client.post", side_effect=responses):
        res = coder_agent(
            None,
            [],
            prompt="Lee gigante.py",
            workspace_dir=str(tmp_path),
            tools_preset="coding",
            max_turns=3,
            max_context=500,  # Umbral bajo para forzar compactación al 90% (450 tokens)
            model="qwen2.5-coder",
        )

    assert res.ok is True
    assert res.data["completed"] is True
    assert res.data["compactions_count"] >= 1
    # Verificar que en el registro de llamadas figura luhn+_compact
    tools_called = [item["tool"] for item in res.data["tool_history"]]
    assert "luhn+_compact" in tools_called


def test_template_auto_reparar_scraper_compiles():
    graph_dict = get_template("auto-reparar-scraper")
    assert graph_dict is not None
    assert graph_dict["name"] == "auto-reparar-scraper"
    assert len(graph_dict["nodes"]) == 3
    assert len(graph_dict["edges"]) == 2

    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_lector_web" in code
    assert "def node_reparador_coder" in code
    assert "def node_notificacion" in code
    assert "runtime.http_call" in code
    assert "runtime.coder_agent" in code
    assert "runtime.telegram" in code

    compiled_obj = compile(code, "<test_auto_reparar_scraper>", "exec")
    assert compiled_obj is not None


def test_template_sincronizar_docs_codigo_compiles():
    graph_dict = get_template("sincronizar-docs-codigo")
    assert graph_dict is not None
    assert graph_dict["name"] == "sincronizar-docs-codigo"
    assert len(graph_dict["nodes"]) == 3
    assert len(graph_dict["edges"]) == 2

    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_lector_git" in code
    assert "def node_sincronizador_coder" in code
    assert "def node_guardar_git" in code
    assert "runtime.git_read" in code
    assert "runtime.coder_agent" in code
    assert "runtime.git_commit" in code

    compiled_obj = compile(code, "<test_sincronizar_docs_codigo>", "exec")
    assert compiled_obj is not None

