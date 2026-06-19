"""Tests for the OpenCode CLI backend (``opencode-ai``).

Mirrors the test surface for the claude-code / codex backends in
``test_daemon_backend_options.py``:

- Schema: ``opencode`` is in the backend enum and the description
  mentions it; ``backend_options`` description includes opencode.
- Command construction: ``opencode run --format json <prompt>`` with
  ``backend_options`` flags appearing after ``--format json`` and before
  the prompt; the prompt is the trailing positional argument.
- Session id extraction is defensive across the common opencode
  event-field naming variants.
- Text extraction handles several plausible event shapes.
- ``ask`` errors clearly when no session id has been captured yet.
- ``ask`` resumes when a session id is present and emits the expected
  ``opencode run --session <id>`` invocation.

The tests use monkey-patched ``subprocess.Popen`` — opencode itself is
not required to be installed.
"""
from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.config import AgentConfig
from lingtai.core.daemon import DaemonManager
from lingtai.core.daemon.run_dir import DaemonRunDir


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_agent(tmp_path):
    """Minimal Agent with daemon capability and a mock LLM service."""
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


def _make_run_dir(agent, *, handle="em-test"):
    return DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle=handle,
        task="dummy task",
        tools=[],
        model="opencode",
        max_turns=10,
        timeout_s=60,
        parent_addr=agent._working_dir.name,
        parent_pid=1,
        system_prompt="[stub]",
        backend="opencode",
    )


class _FakeProc:
    """Minimal subprocess.Popen stand-in for opencode."""

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


def test_schema_enum_includes_opencode():
    from lingtai.core.daemon import get_schema
    schema = get_schema("en")
    backend = schema["properties"]["backend"]
    assert "opencode" in backend["enum"]
    assert "opencode" in backend["description"]


def test_schema_backend_options_description_mentions_opencode():
    from lingtai.core.daemon import get_schema
    schema = get_schema("en")
    bo = schema["properties"]["tasks"]["items"]["properties"]["backend_options"]
    assert "opencode" in bo["description"]
    # The discovery hint should now point at opencode's own --help too.
    assert "opencode run --help" in bo["description"]


# ---------------------------------------------------------------------------
# Defensive extractors (pure helpers)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event,expected", [
    ({"session_id": "abc"}, "abc"),
    ({"sessionID": "def"}, "def"),
    ({"sessionId": "ghi"}, "ghi"),
    ({"session": "jkl"}, "jkl"),
    ({"id": "mno"}, None),  # bare event/message id is not a session id
    ({"type": "session.created", "id": "mno"}, "mno"),
    ({"thread_id": "pqr"}, "pqr"),
    ({"threadId": "stu"}, "stu"),
    ({"session": {"id": "nested"}}, "nested"),
    ({"data": {"session_id": "envelope"}}, "envelope"),
    ({"type": "ping"}, None),  # no session field at all
    ({"session_id": ""}, None),  # empty string is rejected
    ({"session_id": None}, None),  # null is rejected
])
def test_session_id_extraction_is_defensive(event, expected):
    assert DaemonManager._opencode_extract_session_id(event) == expected


@pytest.mark.parametrize("event,expected_contains", [
    ({"text": "hello"}, "hello"),
    ({"content": "world"}, "world"),
    ({"message": "msg-scalar"}, "msg-scalar"),
    ({"delta": "streamed"}, "streamed"),
    ({"answer": "the answer"}, "the answer"),
    ({"output": "out"}, "out"),
    ({"message": {"content": "nested-scalar"}}, "nested-scalar"),
    ({"message": {"content": [{"text": "block-1"}, {"text": "block-2"}]}}, "block-1"),
    ({"item": {"text": "codex-style"}}, "codex-style"),
])
def test_text_extraction_handles_common_shapes(event, expected_contains):
    text = DaemonManager._opencode_extract_text(event)
    assert expected_contains in text


def test_text_extraction_returns_empty_for_purely_structural_events():
    # Pure tool-call event — no text fields.
    assert DaemonManager._opencode_extract_text({"type": "tool.started"}) == ""
    assert DaemonManager._opencode_extract_text({"type": "tool.use"}) == ""
    assert DaemonManager._opencode_extract_text({}) == ""


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_opencode_emanate_cmd_includes_run_and_format_json(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _FakeProc()

    run_dir = _make_run_dir(agent, handle="em-oc")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_opencode_emanation(
            "em-oc", run_dir, "Refactor the auth module.",
            cancel, timeout,
            backend_argv=None,
        )

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "opencode"
    assert cmd[1] == "run"
    # --format json must appear before the prompt positional.
    assert "--format" in cmd
    fmt_idx = cmd.index("--format")
    assert cmd[fmt_idx + 1] == "json"
    # The prompt (constructed via _build_opencode_prompt) is the last token,
    # contains the user task verbatim, and carries the daemon contract intro.
    assert cmd[-1].rstrip().endswith("Refactor the auth module.")
    assert "LingTai daemon" in cmd[-1]


def test_opencode_emanate_appends_backend_argv_before_prompt(tmp_path):
    """backend_options tokens must sit between --format json and the prompt."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return _FakeProc()

    run_dir = _make_run_dir(agent, handle="em-oc-opts")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        mgr._run_opencode_emanation(
            "em-oc-opts", run_dir, "Find the bug.",
            cancel, timeout,
            backend_argv=["--model", "openai/gpt-5", "--quiet"],
        )

    cmd = captured[0]
    fmt_idx = cmd.index("--format")
    model_idx = cmd.index("--model")
    quiet_idx = cmd.index("--quiet")
    # backend_argv tokens come after --format json, before the prompt
    assert fmt_idx < model_idx
    assert fmt_idx < quiet_idx
    assert model_idx < len(cmd) - 1  # not the trailing prompt
    assert quiet_idx < len(cmd) - 1
    # The user task is preserved as the trailing positional
    assert cmd[-1].rstrip().endswith("Find the bug.")


def test_opencode_emanate_persists_session_id_from_first_event(tmp_path):
    """The first JSON event carrying a session-id-shaped field is stored
    in daemon.json so daemon(ask) can resume from the moment the
    emanation returns."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    stdout_lines = [
        '{"type":"session.created","session_id":"oc-session-XYZ"}\n',
        '{"type":"text.delta","text":"thinking..."}\n',
        '{"type":"text.delta","text":"final answer is 42"}\n',
        '{"type":"session.completed","text":"final answer is 42"}\n',
    ]

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=stdout_lines)

    run_dir = _make_run_dir(agent, handle="em-oc-sid")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr._run_opencode_emanation(
            "em-oc-sid", run_dir, "What is the answer?",
            cancel, timeout,
        )

    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["opencode_session_id"] == "oc-session-XYZ"
    # The terminal `session.completed` event's text won — final answer
    # is preserved.
    assert "final answer is 42" in result


def test_opencode_emanate_handles_non_json_lines_gracefully(tmp_path):
    """Banners / progress lines that aren't JSON should be recorded as
    cli_output, not crash the parser. The terminal text from a later
    JSON event still wins."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    stdout_lines = [
        "opencode v0.5.1\n",  # banner — not JSON
        "warming up...\n",     # progress — not JSON
        '{"type":"session.created","session_id":"oc-mixed"}\n',
        '{"type":"final","text":"all good"}\n',
    ]

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=stdout_lines)

    run_dir = _make_run_dir(agent, handle="em-oc-mixed")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr._run_opencode_emanation(
            "em-oc-mixed", run_dir, "task",
            cancel, timeout,
        )

    state = json.loads(run_dir.daemon_json_path.read_text())
    assert state["opencode_session_id"] == "oc-mixed"
    assert "all good" in result


def test_opencode_emanate_uses_no_output_sentinel_when_silent(tmp_path):
    """A process that exits 0 with no JSON events and no stderr is
    surfaced as the explicit [no output] sentinel — not as an empty
    string that the agent has to interpret."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(stdout_lines=[], stderr_lines=[], returncode=0)

    run_dir = _make_run_dir(agent, handle="em-oc-silent")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr._run_opencode_emanation(
            "em-oc-silent", run_dir, "task",
            cancel, timeout,
        )

    assert result == "[no output]"


def test_opencode_emanate_raises_when_returncode_nonzero(tmp_path):
    """Non-zero exit → mark_failed + raise, with the stderr tail in the
    error message so the parent can see what went wrong."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    stdout_lines = ['{"type":"text","text":"partial"}\n']
    stderr_lines = ['auth: invalid token\n', 'exit 1\n']

    def fake_popen(cmd, *args, **kwargs):
        return _FakeProc(
            stdout_lines=stdout_lines, stderr_lines=stderr_lines, returncode=1,
        )

    run_dir = _make_run_dir(agent, handle="em-oc-fail")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(RuntimeError, match="opencode CLI exited with code 1"):
            mgr._run_opencode_emanation(
                "em-oc-fail", run_dir, "task",
                cancel, timeout,
            )


def test_opencode_emanate_missing_cli_raises_runtime_error(tmp_path):
    """FileNotFoundError → mark_failed + RuntimeError naming opencode."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    def fake_popen(cmd, *args, **kwargs):
        raise FileNotFoundError("opencode")

    run_dir = _make_run_dir(agent, handle="em-oc-missing")
    cancel = threading.Event()
    timeout = threading.Event()

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        with pytest.raises(RuntimeError, match="'opencode' CLI not found on PATH"):
            mgr._run_opencode_emanation(
                "em-oc-missing", run_dir, "task",
                cancel, timeout,
            )


# ---------------------------------------------------------------------------
# _handle_emanate dispatch routing
# ---------------------------------------------------------------------------


def test_emanate_opencode_routes_to_cli_handler(tmp_path):
    """`emanate` with backend='opencode' must go through
    `_handle_emanate_cli` (which skips preset resolution) and ultimately
    submit `_run_opencode_emanation` to the pool."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured: dict = {}

    def fake_run(em_id, run_dir, task, cancel_event, timeout_event,
                 backend_argv=None):
        captured["em_id"] = em_id
        captured["task"] = task
        captured["backend_argv"] = list(backend_argv or [])
        captured["state"] = json.loads(run_dir.daemon_json_path.read_text())
        run_dir.mark_done("[fake opencode done]")
        return "[fake opencode done]"

    with patch.object(mgr, "_run_opencode_emanation", side_effect=fake_run):
        result = mgr.handle({
            "action": "emanate",
            "backend": "opencode",
            "tasks": [{
                "task": "Summarise the changelog.",
                "tools": [],
                "backend_options": {"model": "openai/gpt-5"},
            }],
        })
        assert result["status"] == "dispatched"
        assert result["backend"] == "opencode"

        em_id = result["ids"][0]
        mgr._emanations[em_id]["future"].result(timeout=5)

    assert captured["task"] == "Summarise the changelog."
    assert captured["backend_argv"] == ["--model", "openai/gpt-5"]
    assert captured["state"]["backend"] == "opencode"
    assert captured["state"]["backend_options"] == {"model": "openai/gpt-5"}


# ---------------------------------------------------------------------------
# ask routing
# ---------------------------------------------------------------------------


def test_ask_opencode_errors_when_no_session_id(tmp_path):
    """ask before the emanation captured a session id → clear error
    pointing the agent at the (still-initializing) state, not a hang."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    # Register a fake opencode emanation entry with a run_dir but no
    # opencode_session_id yet.
    from concurrent.futures import Future
    run_dir = _make_run_dir(agent, handle="em-oc-noresume")
    fut = Future()
    fut.set_result("[fake done]")
    mgr._emanations["em-oc-noresume"] = {
        "future": fut,
        "task": "x",
        "start_time": 0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": "opencode",
        "ask_in_flight": False,
        "ask_future": None,
    }

    result = mgr.handle({
        "action": "ask",
        "id": "em-oc-noresume",
        "message": "any update?",
    })
    assert result["status"] == "error"
    assert "opencode session ID" in result["message"]
    assert "em-oc-noresume" in result["message"]


def test_ask_opencode_resumes_with_captured_session_id(tmp_path):
    """When opencode_session_id is present, ask spawns
    `opencode run --session <id> --format json <message>` and returns
    {"status":"sent","async":true} synchronously."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    captured_cmd: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        captured_cmd.append(list(cmd))
        # Return an empty-stream proc — the ask worker will see EOF and
        # complete cleanly via the deadline branch.
        return _FakeProc()

    from concurrent.futures import Future
    run_dir = _make_run_dir(agent, handle="em-oc-resume")
    # Pre-seed the session id as if a prior emanation captured it.
    run_dir._state["opencode_session_id"] = "oc-resumable-123"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)

    fut = Future()
    fut.set_result("[fake done]")
    mgr._emanations["em-oc-resume"] = {
        "future": fut,
        "task": "x",
        "start_time": 0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": "opencode",
        "ask_in_flight": False,
        "ask_future": None,
    }

    with patch("lingtai.core.daemon.subprocess.Popen", side_effect=fake_popen):
        result = mgr.handle({
            "action": "ask",
            "id": "em-oc-resume",
            "message": "how is it going?",
        })

    assert result["status"] == "sent"
    assert result.get("async") is True
    assert result["id"] == "em-oc-resume"

    # The ask worker is async — wait for it to finish before asserting on
    # the captured command (it spawns the subprocess synchronously on the
    # calling thread, but we don't want to leave the pool busy for
    # subsequent tests).
    ask_future = mgr._emanations["em-oc-resume"]["ask_future"]
    if ask_future is not None:
        ask_future.result(timeout=5)

    assert len(captured_cmd) == 1
    cmd = captured_cmd[0]
    assert cmd[0] == "opencode"
    assert cmd[1] == "run"
    assert "--session" in cmd
    assert cmd[cmd.index("--session") + 1] == "oc-resumable-123"
    assert "--format" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"
    # The follow-up message is the trailing positional
    assert cmd[-1] == "how is it going?"


def test_ask_opencode_concurrent_returns_busy(tmp_path):
    """A second ask against the same opencode em_id while the first is
    still streaming must return ``status='busy'`` (the resumed CLI
    serializes per session and a second spawn would interleave or
    error)."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")

    from concurrent.futures import Future
    run_dir = _make_run_dir(agent, handle="em-oc-busy")
    run_dir._state["opencode_session_id"] = "oc-busy-1"
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)

    fut = Future()
    fut.set_result("[fake done]")
    mgr._emanations["em-oc-busy"] = {
        "future": fut,
        "task": "x",
        "start_time": 0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": "opencode",
        "ask_in_flight": True,  # pretend an ask is already in flight
        "ask_future": None,
    }

    result = mgr._handle_ask("em-oc-busy", "second concurrent ask")
    assert result["status"] == "busy"
    assert "em-oc-busy" in result["message"]
