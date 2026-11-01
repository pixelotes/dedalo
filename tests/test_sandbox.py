"""Tests del entorno filtrado y el aislamiento opcional con bubblewrap."""

import subprocess

import pytest

from dedalo import compiler, runtime, sandbox


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for var in ("DEDALO_SANDBOX", "DEDALO_SANDBOX_NET", "DEDALO_SANDBOX_RO", "DEDALO_SANDBOX_RW", "DEDALO_ENV_DENY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sandbox, "_probe", {})
    monkeypatch.setattr(sandbox, "_warned", False)


def test_clean_env_removes_dedalo_secrets(monkeypatch):
    monkeypatch.setenv("DEDALO_MASTER_KEY", "k")
    monkeypatch.setenv("DEDALO_SECRET_TOKEN", "t")
    monkeypatch.setenv("DEDALO_LLM_KEY", "l")
    monkeypatch.setenv("DEDALO_TELEGRAM_TOKEN", "x")
    monkeypatch.setenv("DEDALO_PORT", "8080")
    monkeypatch.setenv("MI_VAR_EXTRA", "v")
    env = sandbox.clean_env()
    assert not [k for k in env if k.startswith(("DEDALO_SECRET_", "DEDALO_MASTER_KEY", "DEDALO_LLM_KEY", "DEDALO_TELEGRAM_"))]
    assert env["DEDALO_PORT"] == "8080" and env["MI_VAR_EXTRA"] == "v"


def test_env_deny_extra_patterns(monkeypatch):
    monkeypatch.setenv("DEDALO_ENV_DENY", "MI_*")
    monkeypatch.setenv("MI_TOKEN", "t")
    assert "MI_TOKEN" not in sandbox.clean_env()


def test_command_node_does_not_leak_secret_env(monkeypatch):
    monkeypatch.setenv("DEDALO_MASTER_KEY", "no-debe-verse")
    monkeypatch.setenv("DEDALO_SECRET_X", "tampoco")
    r = runtime.command_input(None, [], cmd="env")
    assert r.status == "ok"
    assert "no-debe-verse" not in r.data["stdout"] and "tampoco" not in r.data["stdout"]


def test_effective_mode_defaults_and_overrides(monkeypatch):
    assert sandbox.effective_mode("command") == "off"
    assert sandbox.effective_mode("coder") == "auto"
    assert sandbox.effective_mode("command", "on") == "on"
    assert sandbox.effective_mode("coder", "off") == "off"
    monkeypatch.setenv("DEDALO_SANDBOX", "on")
    assert sandbox.effective_mode("command", "off") == "on"  # un nodo no puede saltarse un `on` global
    monkeypatch.setenv("DEDALO_SANDBOX", "onn")  # error tipográfico: falla cerrado
    assert sandbox.effective_mode("command") == "on"


def test_build_argv_hides_data_and_binds_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "secrets").mkdir(parents=True)
    monkeypatch.setattr(sandbox.secrets, "DIR", tmp_path / "data" / "secrets")
    monkeypatch.setattr(sandbox.secrets, "KEY_FILE", tmp_path / "data" / "master.key")
    argv = sandbox.build_argv("echo hi", tmp_path)
    assert argv[0] == "bwrap" and argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    assert ["--bind", str(tmp_path), str(tmp_path)] == argv[argv.index("--bind") : argv.index("--bind") + 3]
    assert ["--tmpfs", str(tmp_path / "data")] == argv[argv.index(str(tmp_path / "data")) - 1 : argv.index(str(tmp_path / "data")) + 1]
    assert argv.count(str(tmp_path / "data" / "secrets")) == 0  # ya cubierto por el directorio superior
    assert "--unshare-net" not in argv


def test_build_argv_net_deny_and_no_cwd(monkeypatch):
    monkeypatch.setenv("DEDALO_SANDBOX_NET", "deny")
    argv = sandbox.build_argv("true", None)
    assert "--unshare-net" in argv
    assert argv[argv.index("--chdir") + 1] == "/tmp"


def test_on_without_bwrap_fails_closed(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    r = runtime.command_input(None, [], cmd="echo hi", sandbox="on")
    assert r.status == "error" and "bwrap" in r.error


def test_auto_without_bwrap_runs_unsandboxed(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    monkeypatch.setenv("DEDALO_SANDBOX", "auto")
    r = runtime.command_input(None, [], cmd="echo hola")
    assert r.status == "ok" and r.data["stdout"] == "hola"


def test_sandbox_used_when_available(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="dentro", stderr="")

    monkeypatch.setattr(sandbox, "available", lambda: True)
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    r = runtime.command_input(None, [], cmd="echo x", sandbox="on")
    assert r.data["stdout"] == "dentro"
    assert calls[0][0] == "/usr/bin/bwrap" and "--die-with-parent" in calls[0]


def test_standalone_export_runs_command_node_without_dedalo():
    g = compiler.Graph(
        name="cmd_flow",
        nodes=[
            compiler.Node(id="c", type="input.command", config={"cmd": "echo exportado", "sandbox": "on"}),
            compiler.Node(id="a", type="output.alert", config={"text": "{entrada}"}),
        ],
        edges=[compiler.Edge(source="c", target="a", port="ok")],
    )
    src = compiler.compile_standalone(g)
    assert "from dedalo import" not in src
    compile(src, "<export>", "exec")
