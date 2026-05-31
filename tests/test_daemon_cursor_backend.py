"""Tests for the Cursor Agent CLI daemon backend.

Cursor exposes its headless agent as the ``agent`` executable.  The daemon
backend uses print mode with stream-json output so it behaves like the other
external CLI backends: command construction is deterministic, JSONL progress is
persisted to the daemon run dir, and ``daemon(ask)`` resumes by session id.

The tests monkey-patch ``subprocess.Popen``; Cursor itself is not required.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from lingtai_kernel.config import AgentConfig
from lingtai.core.daemon import DaemonManager
from lingtai.core.daemon.run_dir import DaemonRunDir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_agent(tmp_path):
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["daemon"],
        config=AgentConfig(),
    )


def _make_run_dir(agent, *, handle="em-cursor"):
    return DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle=handle,
        task="dummy task",
        tools=[],
        model="cursor",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="cursor",
    )


class _FakeProc:
    def __init__(self, stdout_lines=(), stderr_lines=(), returncode=0):
        self.stdout = iter(list(stdout_lines))
        self.stderr = iter(list(stderr_lines))
        self.returncode = returncode
        self.pid = 0

    def wait(self, timeout=None):
        return self.returncode


# ---------------------------------------------------------------------------
# Schema surface
# ---------------------------------------------------------------------------


def test_schema_enum_includes_cursor():
    from lingtai.core.daemon import get_schema

    schema = get_schema("en")
    backend = schema["properties"]["backend"]
    assert "cursor" in backend["enum"]
    assert "cursor" in backend["description"]


def test_schema_backend_options_description_mentions_cursor():
    from lingtai.core.daemon import get_schema

    schema = get_schema("en")
    bo = schema["properties"]["tasks"]["items"]["properties"]["backend_options"]
    assert "cursor" in bo["description"]
    assert "agent --help" in bo["description"]


# ---------------------------------------------------------------------------
# Cursor event shapes
# ---------------------------------------------------------------------------


def test_cursor_documented_result_event_extracts_session_and_text():
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "full assistant text",
        "session_id": "cursor-session-123",
    }
    assert DaemonManager._opencode_extract_session_id(event) == "cursor-session-123"
    assert DaemonManager._opencode_extract_text(event) == "full assistant text"


# ---------------------------------------------------------------------------
# Command construction / streaming
# ---------------------------------------------------------------------------


def test_cursor_emanate_cmd_uses_agent_print_stream_json(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _FakeProc()

    run_dir = _make_run_dir(agent, handle="em-cur-cmd")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_cursor_emanation(
            "em-cur-cmd", run_dir, "Refactor the auth module.",
            cancel, timeout,
        )

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[:5] == ["agent", "-p", "--force", "--output-format", "stream-json"]
    assert cmd[-1].rstrip().endswith("Refactor the auth module.")
    assert "LingTai daemon" in cmd[-1]


def test_cursor_emanate_appends_backend_argv_before_prompt(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _FakeProc()

    run_dir = _make_run_dir(agent, handle="em-cur-opts")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_cursor_emanation(
            "em-cur-opts", run_dir, "Find the bug.",
            cancel, timeout,
            backend_argv=["--model", "gpt-5", "--stream-partial-output"],
        )

    cmd = captured[0]
    assert cmd[:5] == ["agent", "-p", "--force", "--output-format", "stream-json"]
    assert cmd.index("--model") > cmd.index("stream-json")
    assert cmd.index("--stream-partial-output") < len(cmd) - 1
    assert cmd[-1].rstrip().endswith("Find the bug.")


def test_cursor_emanate_persists_session_id_and_final_result(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    stdout_lines = [
        '{"type":"system","session_id":"cursor-session-XYZ"}\n',
        '{"type":"assistant","text":"working..."}\n',
        '{"type":"result","subtype":"success","result":"final cursor answer","session_id":"cursor-session-XYZ"}\n',
    ]

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=stdout_lines)

    run_dir = _make_run_dir(agent, handle="em-cur-sid")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr._run_cursor_emanation(
            "em-cur-sid", run_dir, "What is the answer?",
            cancel, timeout,
        )

    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["cursor_session_id"] == "cursor-session-XYZ"
    assert result == "final cursor answer"


def test_cursor_emanate_marks_error_result_failed(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    stdout_lines = [
        '{"type":"result","subtype":"error","is_error":true,"result":"Cursor failed to apply patch"}\n',
    ]

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=stdout_lines)

    run_dir = _make_run_dir(agent, handle="em-cur-error")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        try:
            mgr._run_cursor_emanation(
                "em-cur-error", run_dir, "Please fail", cancel, timeout,
            )
        except RuntimeError as exc:
            assert "error result" in str(exc)
            assert "Cursor failed to apply patch" in str(exc)
        else:  # pragma: no cover - test must fail if no exception is raised
            raise AssertionError("Cursor error result should fail the emanation")

    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["state"] == "failed"



def test_emanate_cursor_routes_to_cli_handler(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event,
                 backend_argv=None):
        captured["em_id"] = em_id
        captured["task"] = task
        captured["backend_argv"] = list(backend_argv or [])
        captured["state"] = json.loads(run_dir.daemon_json_path.read_text())
        run_dir.mark_done("[fake cursor done]")
        return "[fake cursor done]"

    with patch.object(mgr, "_run_cursor_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "cursor",
            "tasks": [{
                "task": "Summarise the changelog.",
                "tools": [],
                "backend_options": {"model": "gpt-5"},
            }],
        })
        assert result["status"] == "dispatched"
        assert result["backend"] == "cursor"
        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["task"] == "Summarise the changelog."
    assert captured["backend_argv"] == ["--model", "gpt-5"]
    assert captured["state"]["backend"] == "cursor"
    assert captured["state"]["backend_options"] == {"model": "gpt-5"}


# ---------------------------------------------------------------------------
# ask routing
# ---------------------------------------------------------------------------


def _register_cursor_entry(mgr, agent, run_dir, em_id="em-cur-resume"):
    fut = Future()
    fut.set_result("[fake done]")
    mgr._emanations[em_id] = {
        "future": fut,
        "task": "x",
        "start_time": 0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": "cursor",
        "ask_in_flight": False,
        "ask_future": None,
    }


def test_ask_cursor_errors_when_no_session_id(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = _make_run_dir(agent, handle="em-cur-noresume")
    _register_cursor_entry(mgr, agent, run_dir, em_id="em-cur-noresume")

    result = mgr.handle({
        "action": "ask",
        "id": "em-cur-noresume",
        "message": "any update?",
    })

    assert result["status"] == "error"
    assert "cursor session ID" in result["message"]
    assert "em-cur-noresume" in result["message"]


def test_ask_cursor_resumes_with_captured_session_id(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    captured_cmd: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        return _FakeProc(stdout_lines=[
            '{"type":"result","subtype":"success","result":"follow-up done"}\n',
        ])

    run_dir = _make_run_dir(agent, handle="em-cur-resume")
    run_dir._state["cursor_session_id"] = "cursor-resumable-123"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
    _register_cursor_entry(mgr, agent, run_dir, em_id="em-cur-resume")

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr.handle({
            "action": "ask",
            "id": "em-cur-resume",
            "message": "how is it going?",
        })

    assert result["status"] == "sent"
    assert result.get("async") is True
    ask_future = mgr._emanations["em-cur-resume"]["ask_future"]
    if ask_future is not None:
        ask_future.result(timeout=5)

    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    assert cmd[:3] == ["agent", "-p", "--force"]
    assert "--resume" in cmd
    assert cmd[cmd.index("--resume") + 1] == "cursor-resumable-123"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[-1] == "how is it going?"


def test_ask_cursor_error_result_publishes_failure(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=[
            '{"type":"result","subtype":"error","is_error":true,"result":"resume failed"}\n',
        ])

    run_dir = _make_run_dir(agent, handle="em-cur-resume-error")
    run_dir._state["cursor_session_id"] = "cursor-resumable-error"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
    _register_cursor_entry(mgr, agent, run_dir, em_id="em-cur-resume-error")

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr.handle({
            "action": "ask",
            "id": "em-cur-resume-error",
            "message": "try again",
        })

    assert result["status"] == "sent"
    ask_future = mgr._emanations["em-cur-resume-error"]["ask_future"]
    assert ask_future is not None
    followup = ask_future.result(timeout=5)
    assert followup["status"] == "error"
    assert "error result" in followup["message"]
    assert "resume failed" in followup["message"]



def test_ask_cursor_concurrent_returns_busy(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    run_dir = _make_run_dir(agent, handle="em-cur-busy")
    run_dir._state["cursor_session_id"] = "cursor-busy-1"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
    _register_cursor_entry(mgr, agent, run_dir, em_id="em-cur-busy")
    mgr._emanations["em-cur-busy"]["ask_in_flight"] = True

    result = mgr._handle_ask("em-cur-busy", "second concurrent ask")

    assert result["status"] == "busy"
    assert "em-cur-busy" in result["message"]
