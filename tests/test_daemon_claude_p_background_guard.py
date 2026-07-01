"""MCP completion contract for daemon-capable backends."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests._daemon_helpers import (
    FiniteFakeProc,
    make_daemon_agent,
    make_daemon_run_dir,
)


def _write_completion(run_dir, status: str, **extra) -> None:
    payload = {
        "schema": "lingtai.daemon_completion.v1",
        "status": status,
        "run_id": run_dir.run_id,
    }
    payload.update(extra)
    (run_dir.path / "daemon_completion.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


def _mark_common_mcp_loaded(run_dir) -> None:
    run_dir._state.setdefault("call_parameters", {})["mcp"] = [
        {"name": "daemon_common", "transport": "stdio"}
    ]
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)


def _drive_print_runner(mgr, run_dir, final_text, monkeypatch):
    stdout_lines = [
        json.dumps({"type": "system", "session_id": "sess-1"}),
        json.dumps({
            "type": "assistant",
            "session_id": "sess-1",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "working..."}]},
        }),
        json.dumps({
            "type": "result",
            "session_id": "sess-1",
            "result": final_text,
            "is_error": False,
        }),
    ]
    fake = FiniteFakeProc(
        stdout_lines=[line + "\n" for line in stdout_lines],
        stderr_lines=[],
        returncode=0,
        pid=4321,
    )
    import lingtai.core.daemon as daemon_mod

    monkeypatch.setattr(
        daemon_mod.subprocess, "Popen", lambda *a, **k: fake,
    )
    return mgr._run_claude_code_emanation(
        "em-1",
        run_dir,
        "do the task",
        threading.Event(),
        threading.Event(),
        backend_argv=[],
    )


def test_claude_command_includes_per_run_mcp_config(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict[str, object] = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["argv"] = list(backend_argv or [])
        captured["task"] = task
        captured["run_dir"] = run_dir
        _write_completion(run_dir, "done", summary="ok")
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    monkeypatch.setattr(mgr, "_run_claude_code_emanation", fake_run)

    result = mgr.handle({
        "action": "emanate",
        "backend": "claude-p",
        "tasks": [{"task": "Run validation.", "tools": []}],
    })
    assert result["status"] == "dispatched"
    mgr._emanations[result["ids"][0]]["future"].result(timeout=5)

    argv = captured["argv"]
    assert "--mcp-config" in argv
    assert "--strict-mcp-config" in argv
    config_path = Path(argv[argv.index("--mcp-config") + 1])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    common = config["mcpServers"]["daemon_common"]
    assert common["args"] == ["-m", "lingtai.mcp_servers.daemon_common"]
    assert common["env"]["LINGTAI_DAEMON_RUN_ID"] == captured["run_dir"].run_id
    assert common["env"]["LINGTAI_DAEMON_COMPLETION_FILE"].endswith(
        "daemon_completion.json"
    )
    task = captured["task"]
    assert "call the MCP tool `finish`" in task
    assert "background-and-wait is invalid" in task


def test_done_sentinel_permits_done(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = make_daemon_run_dir(agent=agent, handle="em-1", backend="claude-p")
    _mark_common_mcp_loaded(run_dir)
    _write_completion(run_dir, "done", summary="completed")

    result = _drive_print_runner(mgr, run_dir, "Done. Suite green.", monkeypatch)

    assert result == "Done. Suite green."
    data = json.loads((run_dir.path / "daemon.json").read_text())
    assert data["state"] == "done"


@pytest.mark.parametrize("status", ["failed", "incomplete"])
def test_failed_or_incomplete_sentinel_prevents_done(tmp_path, monkeypatch, status):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = make_daemon_run_dir(agent=agent, handle="em-1", backend="claude-p")
    _mark_common_mcp_loaded(run_dir)
    _write_completion(run_dir, status, reason="blocked")

    with pytest.raises(RuntimeError):
        _drive_print_runner(mgr, run_dir, "I could not validate.", monkeypatch)

    data = json.loads((run_dir.path / "daemon.json").read_text())
    assert data["state"] == "failed"
    assert "I could not validate" in (run_dir.path / "result.txt").read_text()


def test_missing_sentinel_prevents_done(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = make_daemon_run_dir(agent=agent, handle="em-1", backend="claude-p")
    _mark_common_mcp_loaded(run_dir)

    with pytest.raises(RuntimeError):
        _drive_print_runner(mgr, run_dir, "Done without finish.", monkeypatch)

    data = json.loads((run_dir.path / "daemon.json").read_text())
    assert data["state"] == "failed"
    assert "missing completion" in data["error"]["message"]


def test_invalid_sentinel_prevents_done(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = make_daemon_run_dir(agent=agent, handle="em-1", backend="claude-p")
    _mark_common_mcp_loaded(run_dir)
    (run_dir.path / "daemon_completion.json").write_text(
        json.dumps({"status": "wat"}), encoding="utf-8",
    )

    with pytest.raises(RuntimeError):
        _drive_print_runner(mgr, run_dir, "Done maybe.", monkeypatch)

    data = json.loads((run_dir.path / "daemon.json").read_text())
    assert data["state"] == "failed"
    assert "completion status" in data["error"]["message"]


def test_lingtai_backend_gets_default_common_mcp_registration(tmp_path, monkeypatch):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict[str, object] = {}

    def fake_connect(registrations):
        captured["registrations"] = registrations
        return {}, {}, []

    def fake_run(em_id, run_dir, schemas, dispatch, task, cancel_event,
                 timeout_event, preset_llm, max_turns, mcp_clients):
        captured["prompt"] = run_dir.prompt_path.read_text(encoding="utf-8")
        _write_completion(run_dir, "done", summary="ok")
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    monkeypatch.setattr(mgr, "_connect_task_mcp_registrations", fake_connect)
    monkeypatch.setattr(mgr, "_run_emanation", fake_run)

    result = mgr.handle({
        "action": "emanate",
        "tasks": [{"task": "Do work.", "tools": []}],
    })
    assert result["status"] == "dispatched"
    mgr._emanations[result["ids"][0]]["future"].result(timeout=5)

    regs = captured["registrations"]
    assert regs[0]["name"] == "daemon_common"
    assert regs[0]["args"] == ["-m", "lingtai.mcp_servers.daemon_common"]
    assert "LINGTAI_DAEMON_COMPLETION_FILE" in regs[0]["env"]
    assert "call the MCP tool `finish`" in captured["prompt"]


@pytest.mark.parametrize("backend", ["codex", "opencode", "qwen-code"])
def test_cli_backend_receives_common_mcp_configuration(tmp_path, monkeypatch, backend):
    agent = make_daemon_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict[str, object] = {}
    runner_attr = {
        "codex": "_run_codex_emanation",
        "opencode": "_run_opencode_emanation",
        "qwen-code": "_run_qwen_code_emanation",
    }[backend]

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event, backend_argv=None):
        captured["argv"] = list(backend_argv or [])
        captured["task"] = task
        captured["mcp"] = run_dir._state["call_parameters"]["mcp"]
        _write_completion(run_dir, "done", summary="ok")
        run_dir.mark_done("[fake done]")
        return "[fake done]"

    monkeypatch.setattr(mgr, runner_attr, fake_run)

    result = mgr.handle({
        "action": "emanate",
        "backend": backend,
        "tasks": [{"task": f"Run with {backend}.", "tools": []}],
    })
    assert result["status"] == "dispatched"
    mgr._emanations[result["ids"][0]]["future"].result(timeout=5)

    assert captured["mcp"][0]["name"] == "daemon_common"
    assert "call the MCP tool `finish`" in captured["task"]
    argv = captured["argv"]
    if backend == "codex":
        joined = "\n".join(argv)
        assert "mcp_servers.daemon_common.command" in joined
        assert "mcp_servers.daemon_common.args" in joined
        assert "mcp_servers.daemon_common.env" in joined
    elif backend == "opencode":
        idx = argv.index("__lingtai_opencode_config_content")
        config = json.loads(argv[idx + 1])
        common = config["mcp"]["daemon_common"]
        assert common["command"][1:] == ["-m", "lingtai.mcp_servers.daemon_common"]
        assert common["environment"]["LINGTAI_DAEMON_COMPLETION_FILE"].endswith(
            "daemon_completion.json"
        )
    else:
        idx = argv.index("__lingtai_qwen_system_settings_path")
        settings_path = Path(argv[idx + 1])
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        common = settings["mcpServers"]["daemon_common"]
        assert common["args"] == ["-m", "lingtai.mcp_servers.daemon_common"]
        assert common["env"]["LINGTAI_DAEMON_COMPLETION_FILE"].endswith(
            "daemon_completion.json"
        )
