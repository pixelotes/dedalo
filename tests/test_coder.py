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
    _tool_web_search,
    _tool_write,
    coder_agent,
    ALL_TOOL_NAMES,
    CODER_TOOL_DEFINITIONS,
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
            compaction_engine="luhn+",
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


def test_template_triaje_errores_coder_compiles():
    graph_dict = get_template("triaje-errores-coder")
    assert graph_dict is not None
    assert graph_dict["name"] == "triaje-errores-coder"
    assert len(graph_dict["nodes"]) == 3
    assert len(graph_dict["edges"]) == 2

    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_webhook_error" in code
    assert "def node_analista_coder" in code
    assert "def node_notificacion" in code
    assert "runtime.webhook_trigger" in code
    assert "runtime.coder_agent" in code
    assert "runtime.telegram" in code

    compiled_obj = compile(code, "<test_triaje_errores_coder>", "exec")
    assert compiled_obj is not None


def test_template_generar_tests_coder_compiles():
    graph_dict = get_template("generar-tests-coder")
    assert graph_dict is not None
    assert graph_dict["name"] == "generar-tests-coder"
    assert len(graph_dict["nodes"]) == 3
    assert len(graph_dict["edges"]) == 2

    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_lector_git" in code
    assert "def node_generador_tests" in code
    assert "def node_guardar_git" in code
    assert "runtime.git_read" in code
    assert "runtime.coder_agent" in code
    assert "runtime.git_commit" in code

    compiled_obj = compile(code, "<test_generar_tests_coder>", "exec")
    assert compiled_obj is not None


def test_compact_conversation_history_with_blackhole():
    messages = [
        {"role": "system", "content": "Eres un programador."},
        {"role": "user", "content": "Refactoriza el módulo de autenticación."},
        {
            "role": "assistant",
            "content": "Voy a inspeccionar auth.py",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "read", "arguments": json.dumps({"path": "src/auth.py", "start_line": 1, "end_line": 50})},
            }],
        },
        {"role": "tool", "name": "read", "content": "def login(): pass\n" * 20},
        {
            "role": "assistant",
            "content": "Editando auth.py",
            "tool_calls": [{
                "id": "c2",
                "type": "function",
                "function": {"name": "edit", "arguments": json.dumps({"path": "src/auth.py", "old_str": "inexistente", "new_str": "nuevo"})},
            }],
        },
        {"role": "tool", "name": "edit", "content": "Error: No se encontró 'old_str' en src/auth.py"},
        {
            "role": "assistant",
            "content": "Ejecutando tests",
            "tool_calls": [{
                "id": "c3",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "pytest tests/test_auth.py"})},
            }],
        },
        {"role": "tool", "name": "bash", "content": "PASSED tests/test_auth.py [Código de salida: 0]"},
        {"role": "assistant", "content": "¿Deseas que continúe?", "tool_calls": []},
    ]

    tokens_before = _estimate_messages_tokens(messages)
    assert tokens_before > 150

    compacted, did_compact = _compact_conversation_history(messages, context_window=180, engine="blackhole")
    assert did_compact is True

    summary_msg = compacted[2]
    assert summary_msg["role"] == "user"
    content = summary_msg["content"]
    assert "[Memoria resumida del progreso previo mediante Blackhole" in content
    assert "### Objetivo Principal" in content
    assert "Refactoriza el módulo de autenticación." in content
    assert "### Archivos y Rutas" in content
    assert "src/auth.py" in content
    assert "### Registro de Acciones Recientes" in content
    assert "read('src/auth.py'" in content
    assert "edit('src/auth.py')" in content
    assert "### Errores e Incidencias Detectadas" in content
    assert "No se encontró 'old_str'" in content


def test_compact_conversation_history_with_blackhole_llm():
    messages = [
        {"role": "system", "content": "Eres un programador."},
        {"role": "user", "content": "Agrega tests para la base de datos."},
        {
            "role": "assistant",
            "content": "Escribiendo test_db.py",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "write", "arguments": json.dumps({"path": "tests/test_db.py", "content": "import pytest"})},
            }],
        },
        {"role": "tool", "name": "write", "content": "guardado 'tests/test_db.py'\n" * 15},
        {
            "role": "assistant",
            "content": "Continuando con la configuración",
            "tool_calls": [{
                "id": "c2",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "pytest"})},
            }],
        },
        {"role": "tool", "name": "bash", "content": "1 passed in 0.05s [Código de salida: 0]"},
        {"role": "assistant", "content": "Listo para continuar.", "tool_calls": []},
    ]

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "- Se implementó el archivo tests/test_db.py\n- Se verificó compatibilidad SQLite WAL",
            }
        }]
    }
    mock_client.post.return_value = mock_resp

    compacted, did_compact = _compact_conversation_history(
        messages,
        context_window=100,
        engine="blackhole_llm",
        client=mock_client,
        endpoint="http://localhost:11434/v1/chat/completions",
        headers={},
        target_model="local-model",
        temperature=0.1,
    )
    assert did_compact is True
    summary_msg = compacted[2]
    content = summary_msg["content"]
    assert "[Memoria resumida del progreso previo mediante Blackhole con LLM" in content
    assert "### Reflexiones y Decisiones Clave (LLM local)" in content
    assert "Se verificó compatibilidad SQLite WAL" in content


def test_coder_agent_triggers_blackhole_compaction(tmp_path: Path):
    large_code = "\n".join([f"def funcion_{i}(): return {i}" for i in range(120)])
    (tmp_path / "modulo.py").write_text(large_code, encoding="utf-8")

    responses = [
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Leyendo modulo",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": json.dumps({"path": "modulo.py"}),
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
                        "content": "Análisis finalizado con éxito.",
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
            prompt="Audita modulo.py",
            workspace_dir=str(tmp_path),
            tools_preset="coding",
            max_turns=3,
            max_context=450,
            compaction_engine="blackhole",
            model="qwen2.5-coder",
        )

    assert res.ok is True
    assert res.data["completed"] is True
    assert res.data["compactions_count"] >= 1
    assert res.data["compaction_engine"] == "blackhole"
    tools_called = [item["tool"] for item in res.data["tool_history"]]
    assert "blackhole_compact" in tools_called


def test_coder_agent_loop_detection_consecutive_failures(tmp_path: Path):
    (tmp_path / "app.py").write_text("def hello(): pass\n", encoding="utf-8")

    def fail_edit_call(call_id):
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "edit",
                "arguments": json.dumps({
                    "path": "app.py",
                    "old_str": "string_que_no_existe",
                    "new_str": "nuevo",
                }),
            },
        }

    recorded_requests = []

    def mock_post(url, headers, json):
        recorded_requests.append(json)
        current_turn = len(recorded_requests)
        if current_turn in (1, 2, 3):
            # Turnos 1, 2 y 3: el modelo insiste en el mismo edit fallido
            return MagicMock(
                status_code=200,
                json=lambda: {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": f"Intento de edición {current_turn}",
                            "tool_calls": [fail_edit_call(f"call_{current_turn}")],
                        }
                    }]
                },
            )
        else:
            # Turno 4: el modelo ve la alerta de bucle y se detiene
            return MagicMock(
                status_code=200,
                json=lambda: {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": "He visto la advertencia de bucle, me detengo para revisar.",
                            "tool_calls": None,
                        }
                    }]
                },
            )

    with patch("httpx.Client.post", side_effect=mock_post):
        res = coder_agent(
            None,
            [],
            prompt="Corrige app.py",
            workspace_dir=str(tmp_path),
            tools_preset="coding",
            max_turns=5,
            max_context=32768,
            model="qwen2.5-coder",
        )

    assert res.ok is True
    assert res.data["completed"] is True

    # Comprobar que en el historial figura loop_detected_alert
    tool_names = [item["tool"] for item in res.data["tool_history"]]
    assert "loop_detected_alert" in tool_names

    # Comprobar que en el cuarto request (Turno 4), el prompt contiene la alerta de bucle
    turn4_messages = recorded_requests[3]["messages"]
    user_alerts = [m["content"] for m in turn4_messages if m.get("role") == "user" and "[Aviso de bucle del sistema]" in m.get("content", "")]
    assert len(user_alerts) >= 1
    assert "La herramienta 'edit' ha fallado 3 veces consecutivas" in user_alerts[0]


def test_tool_web_search_mocked():
    # 1. Validación de query vacía
    err_res = _tool_web_search("")
    assert "Debes especificar el parámetro 'query'" in err_res

    # 2. Mock de runtime.web_search
    with patch("dedalo.runtime.web_search") as mock_ws:
        from dedalo.runtime import NodeResult
        mock_ws.return_value = NodeResult("ok", data="- [Python](https://python.org): Lenguaje de programación.")
        res = _tool_web_search("python docs", limit=3)
        assert "- [Python](https://python.org)" in res
        mock_ws.assert_called_once()
        call_kwargs = mock_ws.call_args.kwargs
        assert call_kwargs["query"] == "python docs"
        assert call_kwargs["limit"] == 3
        assert call_kwargs["engine"] == "duckduckgo"
        assert call_kwargs["output_format"] == "markdown"

    # 3. Simulación de fallo en web_search
    with patch("dedalo.runtime.web_search") as mock_err:
        from dedalo.runtime import NodeResult
        mock_err.return_value = NodeResult("error", error="Timeout de conexión")
        res_fail = _tool_web_search("error query")
        assert "Error en búsqueda web: Timeout de conexión" in res_fail


def test_coder_agent_react_loop_with_web_search(tmp_path: Path):
    responses = [
        # Turno 1: el modelo llama a web_search
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Voy a buscar información en la web",
                        "tool_calls": [{
                            "id": "call_ws",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": json.dumps({"query": "fastapi lifespans"}),
                            },
                        }],
                    },
                }],
            },
        ),
        # Turno 2: el modelo responde con la información encontrada
        MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Encontré que los lifespans en FastAPI se definen con asynccontextmanager.",
                        "tool_calls": None,
                    },
                }],
            },
        ),
    ]

    with patch("dedalo.runtime._tool_web_search", return_value="- [FastAPI](https://fastapi.tiangolo.com): Lifespans guide") as mock_search, \
         patch("httpx.Client.post", side_effect=responses):
        res = coder_agent(
            None,
            [],
            prompt="Investiga cómo funcionan los lifespans en FastAPI",
            workspace_dir=str(tmp_path),
            tools_preset="all",
            max_turns=3,
            max_context=32768,
            model="qwen2.5-coder",
        )

    assert res.ok is True
    assert res.data["completed"] is True
    assert res.data["turns_used"] == 2
    mock_search.assert_called_once_with("fastapi lifespans", 5, timeout=30.0)
    tool_names = [item["tool"] for item in res.data["tool_history"]]
    assert "web_search" in tool_names
    assert "lifespans" in res.data["response"]


def test_coder_agent_custom_tools_permissions(tmp_path: Path):
    # 1. Comprobar que "web_search" está en ALL_TOOL_NAMES y CODER_TOOL_DEFINITIONS
    assert "web_search" in ALL_TOOL_NAMES
    assert "web_search" in CODER_TOOL_DEFINITIONS

    # 2. Configurar custom_tools con "read,web_search" (excluyendo "bash")
    recorded_requests = []

    def mock_post(url, headers, json):
        recorded_requests.append(json)
        # El modelo intenta llamar a bash (no autorizada)
        return MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Intento ejecutar bash",
                        "tool_calls": [{
                            "id": "call_unauth",
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command": "ls"}',
                            },
                        }],
                    },
                }],
            },
        ) if len(recorded_requests) == 1 else MagicMock(
            status_code=200,
            json=lambda: {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Entendido, no tengo permiso para bash.",
                        "tool_calls": None,
                    },
                }],
            },
        )

    with patch("httpx.Client.post", side_effect=mock_post):
        res = coder_agent(
            None,
            [],
            prompt="Ejecuta un comando",
            workspace_dir=str(tmp_path),
            tools_preset="custom",
            custom_tools="read,web_search",  # bash denegado
            max_turns=3,
            max_context=32768,
            model="qwen2.5-coder",
        )

    assert res.ok is True
    # Verificar que en el payload enviado al LLM solo están "read" y "web_search"
    req_tools = recorded_requests[0].get("tools", [])
    req_tool_names = [t["function"]["name"] for t in req_tools]
    assert "read" in req_tool_names
    assert "web_search" in req_tool_names
    assert "bash" not in req_tool_names

    # Verificar que el intento de llamar a 'bash' fue bloqueado con error de autorización
    tool_hist = res.data["tool_history"]
    assert len(tool_hist) >= 1
    assert tool_hist[0]["tool"] == "bash"
    assert "no está disponible o no está autorizada" in tool_hist[0]["output_preview"]

    # 3. Verificar que custom_tools acepta lista de strings: ['read', 'grep']
    recorded_list_requests = []
    with patch("httpx.Client.post", side_effect=[
        MagicMock(status_code=200, json=lambda: {
            "choices": [{"message": {"role": "assistant", "content": "Listo", "tool_calls": None}}]
        })
    ]) as mock_list_post:
        coder_agent(
            None,
            [],
            prompt="Consulta código",
            workspace_dir=str(tmp_path),
            tools_preset="custom",
            custom_tools=["read", "grep"],
            max_turns=2,
            max_context=32768,
            model="qwen2.5-coder",
        )
        first_call = mock_list_post.call_args_list[0]
        passed_tools = [t["function"]["name"] for t in first_call.kwargs["json"]["tools"]]
        assert passed_tools == ["read", "grep"]


