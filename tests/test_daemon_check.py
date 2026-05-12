"""Tests for daemon(action='check') — read-only event-tail surface."""
import threading
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig


def _make_agent(tmp_path, capabilities=None):
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    return Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )


def _make_run_dir(agent, em_id="em-test"):
    from lingtai.core.daemon.run_dir import DaemonRunDir
    return DaemonRunDir(
        parent_working_dir=agent._working_dir,
        handle=em_id,
        task="test task",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=300.0,
        parent_addr=agent._working_dir.name,
        parent_pid=12345,
        system_prompt="You are a daemon.",
    )


def _register(mgr, em_id, run_dir, future=None):
    mgr._emanations[em_id] = {
        "future": future or MagicMock(done=MagicMock(return_value=False)),
        "task": "test task",
        "start_time": 0.0,
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }


def test_check_unknown_id_returns_error(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "check", "id": "em-999"})
    assert out["status"] == "error"
    assert "em-999" in out["message"]


def test_check_running_emanation_returns_state_and_events(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-1")
    _register(mgr, "em-1", rd)

    # Constructor already wrote daemon_start event. Add a couple more.
    rd.set_current_tool("read", {"file_path": "/tmp/x"})
    rd.clear_current_tool("ok")
    rd.bump_turn(turn=1, response_text="working...")

    out = mgr.handle({"action": "check", "id": "em-1"})
    assert out["id"] == "em-1"
    assert out["state"] == "running"
    assert out["turn"] == 1
    assert isinstance(out["events"], list)
    assert out["events_returned"] == len(out["events"])
    assert out["events_total"] >= 3  # daemon_start + tool_call + tool_result
    # Each event must have the expected shape
    event_types = {e.get("event") for e in out["events"]}
    assert "daemon_start" in event_types or "tool_call" in event_types


def test_check_respects_last_parameter(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-2")
    _register(mgr, "em-2", rd)

    # Generate ~10 events
    for i in range(5):
        rd.set_current_tool(f"tool_{i}", {"i": i})
        rd.clear_current_tool("ok")

    out = mgr.handle({"action": "check", "id": "em-2", "last": 3})
    assert out["events_returned"] == 3
    assert out["events_total"] >= 11  # 1 start + 10 tool events
    # The last 3 should include the most recent tool_result
    last_event = out["events"][-1]
    assert last_event.get("event") in ("tool_result", "tool_call")


def test_check_truncate_limits_string_fields(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-3")
    _register(mgr, "em-3", rd)

    # Inject an event with a very long args_preview
    rd.set_current_tool("bash", {"cmd": "x" * 2000})

    out = mgr.handle({"action": "check", "id": "em-3", "truncate": 100})
    # Find the tool_call event we just wrote
    tool_call_events = [e for e in out["events"] if e.get("event") == "tool_call"]
    assert tool_call_events, "expected at least one tool_call event"
    args_preview = tool_call_events[-1].get("args_preview", "")
    # Truncation appends "…[truncated]" so length is 100 + suffix
    assert "[truncated]" in args_preview
    assert len(args_preview) <= 100 + len("…[truncated]")


def test_check_truncate_zero_disables(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-4")
    _register(mgr, "em-4", rd)
    rd.set_current_tool("bash", {"cmd": "x" * 100})

    out = mgr.handle({"action": "check", "id": "em-4", "truncate": 0})
    tool_call_events = [e for e in out["events"] if e.get("event") == "tool_call"]
    assert tool_call_events
    # With truncate=0 the args_preview must NOT carry the truncation marker.
    # (Note: set_current_tool itself caps args_preview at 500 chars before
    # writing — that's a separate, pre-existing cap that lives in run_dir,
    # not in _handle_check. Our truncate=0 means _handle_check applies no
    # additional truncation.)
    assert "[truncated]" not in tool_call_events[-1].get("args_preview", "")


def test_check_includes_terminal_event_for_done_emanation(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-5")
    _register(mgr, "em-5", rd, future=MagicMock(done=MagicMock(return_value=True)))

    rd.mark_done("final report text")

    out = mgr.handle({"action": "check", "id": "em-5"})
    assert out["state"] == "done"
    assert out["backend"] == "lingtai"
    assert out["path"] == str(rd.path)
    assert out["finished_at"] is not None
    assert out["result_preview"] == "final report text"
    assert out["result_path"] == str(rd.result_path)
    assert out["error"] is None
    event_types = {e.get("event") for e in out["events"]}
    assert "daemon_done" in event_types



def test_check_includes_cli_progress_fields(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-cli")
    rd._state["backend"] = "codex"
    rd._atomic_write_json(rd.daemon_json_path, rd._state)
    _register(mgr, "em-cli", rd)

    rd.record_cli_output("phase 1 complete", stream="combined")

    out = mgr.handle({"action": "check", "id": "em-cli"})
    assert out["backend"] == "codex"
    assert out["last_output"] == "phase 1 complete"
    assert out["last_output_at"] is not None
    cli_events = [e for e in out["events"] if e.get("event") == "cli_output"]
    assert cli_events
    assert cli_events[-1]["text"] == "phase 1 complete"


def test_check_includes_failure_error(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-fail")
    _register(mgr, "em-fail", rd, future=MagicMock(done=MagicMock(return_value=True)))

    rd.mark_failed(RuntimeError("boom"))

    out = mgr.handle({"action": "check", "id": "em-fail"})
    assert out["state"] == "failed"
    assert out["error"]["type"] == "RuntimeError"
    assert out["error"]["message"] == "boom"
    assert out["finished_at"] is not None

def test_check_default_last_is_20(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-6")
    _register(mgr, "em-6", rd)

    # Generate 30 events (15 tool_call + tool_result pairs)
    for i in range(15):
        rd.set_current_tool(f"tool_{i}", {"i": i})
        rd.clear_current_tool("ok")

    # Default last=20 → 20 returned, 31 total (1 start + 30 tool)
    out = mgr.handle({"action": "check", "id": "em-6"})
    assert out["events_returned"] == 20
    assert out["events_total"] == 31


def test_check_rejects_non_numeric_last(tmp_path):
    """Non-numeric `last` must return a clean error, not raise."""
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "check", "id": "em-x", "last": "twenty"})
    assert out["status"] == "error"
    assert "last" in out["message"]


def test_check_rejects_non_numeric_truncate(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "check", "id": "em-x", "truncate": "tons"})
    assert out["status"] == "error"
    assert "truncate" in out["message"]


def test_check_rejects_zero_or_negative_last(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "check", "id": "em-x", "last": 0})
    assert out["status"] == "error"
    assert "last" in out["message"]
    out = mgr.handle({"action": "check", "id": "em-x", "last": -1})
    assert out["status"] == "error"


def test_check_rejects_negative_truncate(tmp_path):
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    out = mgr.handle({"action": "check", "id": "em-x", "truncate": -10})
    assert out["status"] == "error"
    assert "truncate" in out["message"]


def test_check_caps_last_at_max(tmp_path):
    """last is capped at _CHECK_LAST_MAX so events.jsonl can't be slurped
    unboundedly (readlines() would otherwise read the whole file)."""
    from lingtai.core.daemon import DaemonManager
    agent = _make_agent(tmp_path)
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, "em-cap")
    _register(mgr, "em-cap", rd)

    # Asking for a huge value still works but only returns up to the cap.
    out = mgr.handle({"action": "check", "id": "em-cap", "last": 10**9})
    # Successful check returns the data shape (no `status` field — that's
    # reserved for error responses). The important property: no exception,
    # no DoS, response shape intact.
    assert "events" in out
    assert out["events_returned"] <= DaemonManager._CHECK_LAST_MAX
