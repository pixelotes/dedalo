"""Pruebas unitarias para la integración de Git en Dédalo (input.git, output.git y plantilla)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from dedalo import compiler
from dedalo.runtime import git_commit_push, git_read
from dedalo.templates import get_template


def _setup_test_repo(path: Path) -> None:
    """Inicializa un repositorio Git local para pruebas y crea un commit inicial."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "tester@example.com"], check=True, capture_output=True)

    # Crear archivo inicial
    (path / "saludo.py").write_text("print('hola mundo')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "feat: commit inicial"], check=True, capture_output=True)


def test_git_read_log(tmp_path: Path):
    repo_dir = tmp_path / "test_repo_log"
    _setup_test_repo(repo_dir)

    # Crear segundo commit
    (repo_dir / "modulo.py").write_text("x = 10\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "feat: segundo commit"], check=True, capture_output=True)

    res = git_read(None, [], action="log", repo_path=str(repo_dir), limit=5)
    assert res.status == "ok"
    assert len(res.data) == 2

    latest = res.data[0]
    assert latest["message"] == "feat: segundo commit"
    assert latest["author"] == "Tester"
    assert latest["email"] == "tester@example.com"
    assert "modulo.py" in latest["files"]

    first = res.data[1]
    assert first["message"] == "feat: commit inicial"
    assert "saludo.py" in first["files"]


def test_git_read_diff(tmp_path: Path):
    repo_dir = tmp_path / "test_repo_diff"
    _setup_test_repo(repo_dir)

    # Modificar archivo existente
    (repo_dir / "saludo.py").write_text("print('hola mundo modificado')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fix: saludo"], check=True, capture_output=True)

    res = git_read(None, [], action="diff", repo_path=str(repo_dir), revision="HEAD~1..HEAD")
    assert res.status == "ok"
    assert "saludo.py" in res.data["files"]
    assert "-print('hola mundo')" in res.data["diff"]
    assert "+print('hola mundo modificado')" in res.data["diff"]
    assert res.data["truncated"] is False


def test_git_read_diff_truncation(tmp_path: Path):
    repo_dir = tmp_path / "test_repo_diff_trunc"
    _setup_test_repo(repo_dir)

    # Modificar con contenido largo
    large_code = "\n".join([f"def func_{i}(): pass" for i in range(200)])
    (repo_dir / "largo.py").write_text(large_code, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "feat: archivo largo"], check=True, capture_output=True)

    # Pedir con max_chars=100
    res = git_read(None, [], action="diff", repo_path=str(repo_dir), revision="HEAD~1..HEAD", max_chars=100)
    assert res.status == "ok"
    assert len(res.data["diff"]) <= 100
    assert res.data["truncated"] is True
    assert res.data["total_chars"] > 100


def test_git_read_status(tmp_path: Path):
    repo_dir = tmp_path / "test_repo_status"
    _setup_test_repo(repo_dir)

    # Estado limpio
    res_clean = git_read(None, [], action="status", repo_path=str(repo_dir))
    assert res_clean.status == "ok"
    assert res_clean.data["clean"] is True
    assert res_clean.data["total_changes"] == 0

    # Crear archivo nuevo sin trackear
    (repo_dir / "nuevo.txt").write_text("nuevo", encoding="utf-8")
    res_dirty = git_read(None, [], action="status", repo_path=str(repo_dir))
    assert res_dirty.status == "ok"
    assert res_dirty.data["clean"] is False
    assert res_dirty.data["total_changes"] == 1
    assert res_dirty.data["changes"][0]["file"] == "nuevo.txt"


def test_git_commit_push(tmp_path: Path):
    repo_dir = tmp_path / "test_repo_commit"
    _setup_test_repo(repo_dir)

    # Añadir archivo de notas
    (repo_dir / "nota.md").write_text("# Informe diario\nTodo correcto.", encoding="utf-8")

    res = git_commit_push(
        None,
        [],
        repo_path=str(repo_dir),
        message="docs: actualizar informe diario",
        author_name="Dedalo Bot",
        author_email="bot@dedalo.local",
    )

    assert res.status == "ok"
    assert res.data["committed"] is True
    assert res.data["message"] == "docs: actualizar informe diario"
    assert len(res.data["commit_hash"]) == 40

    # Si se vuelve a ejecutar sin cambios
    res_no_changes = git_commit_push(None, [], repo_path=str(repo_dir))
    assert res_no_changes.status == "ok"
    assert res_no_changes.data["committed"] is False


def test_git_invalid_repo_and_action():
    res_err_dir = git_read(None, [], repo_path="/ruta/que/no/existe/jamas")
    assert res_err_dir.status == "error"
    assert "no es un repositorio Git válido" in res_err_dir.error

    res_err_action = git_read(None, [], action="accion_invalida", repo_path=".")
    assert res_err_action.status == "error"
    assert "Acción de git no reconocida" in res_err_action.error


def test_template_auditoria_commits_git_compiles():
    graph_dict = get_template("auditoria-commits-git")
    assert graph_dict is not None
    assert graph_dict["name"] == "auditoria-commits-git"
    assert len(graph_dict["nodes"]) == 3
    assert len(graph_dict["edges"]) == 2

    # Compilar el flujo a Python y verificar que no genera errores de sintaxis
    g = compiler.Graph.from_dict(graph_dict)
    code = compiler.compile(g)
    assert "def node_lector_git" in code
    assert "def node_auditor_ia" in code
    assert "def node_telegram" in code
    assert "runtime.git_read" in code
    assert "runtime.agent" in code

    compiled_obj = compile(code, "<test_git_flow>", "exec")
    assert compiled_obj is not None
