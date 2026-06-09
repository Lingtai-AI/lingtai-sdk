# tests/test_daemon.py
"""Tests for the daemon (神識) capability — subagent system."""
import json
import queue
import re
import threading
import time
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.llm.base import ToolCall


def _make_agent(tmp_path, capabilities=None):
    """Create a minimal Agent with mock LLM service."""
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=capabilities or ["daemon"],
        config=AgentConfig(),
    )
    return agent


def _make_run_dir(agent, em_id="em-test"):
    """Helper: build a DaemonRunDir matching the new _run_emanation signature."""
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


def test_daemon_registers_tool(tmp_path):
    agent = _make_agent(tmp_path, ["daemon"])
    tool_names = {s.name for s in agent._tool_schemas}
    assert "daemon" in tool_names


def test_daemon_default_max_emanations_is_100(tmp_path):
    """Default concurrency ceiling is 100 when init.json gives no override."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    assert mgr._max_emanations == 100
    assert mgr._handle_list()["max_emanations"] == 100


def test_daemon_max_emanations_override_reaches_manager(tmp_path):
    """A kwargs override on the daemon capability reaches DaemonManager."""
    agent = _make_agent(tmp_path, {"daemon": {"max_emanations": 30}})
    mgr = agent.get_capability("daemon")
    assert mgr._max_emanations == 30
    assert mgr._handle_list()["max_emanations"] == 30


def test_build_tool_surface_expands_groups(tmp_path):
    """'file' group expands to read/write/edit/glob/grep."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface(["file"])
    names = {s.name for s in schemas}
    assert "read" in names
    assert "write" in names
    assert "edit" in names
    assert "glob" in names
    assert "grep" in names


def test_build_tool_surface_blacklist(tmp_path):
    """Blacklisted tools are silently excluded."""
    agent = _make_agent(tmp_path, ["file", "daemon", "avatar"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface([
        "file",
        "avatar",
        "avatar_spawn",
        "avatar_rules",
        "daemon",
    ])
    names = {s.name for s in schemas}
    assert "daemon" not in names
    assert "avatar" not in names
    assert "avatar_spawn" not in names
    assert "avatar_rules" not in names
    assert "avatar_spawn" not in dispatch
    assert "avatar_rules" not in dispatch
    assert "read" in names


def test_build_tool_surface_unknown_tool(tmp_path):
    """Unknown tool name raises ValueError."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    try:
        mgr._build_tool_surface(["nonexistent"])
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "nonexistent" in str(e)


def test_build_tool_surface_inherits_mcp_tools(tmp_path):
    """MCP tools are automatically inherited without being requested."""
    agent = _make_agent(tmp_path, ["daemon"])
    # Simulate an MCP tool registered via connect_mcp
    agent._sealed = False
    agent.add_tool("my_mcp_tool", schema={"type": "object", "properties": {}},
                   handler=lambda args: {}, description="MCP tool")
    agent._sealed = True
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface([])  # no explicit tools
    names = {s.name for s in schemas}
    assert "my_mcp_tool" in names


def test_build_emanation_prompt_includes_task(tmp_path):
    """System prompt includes the task description."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, _ = mgr._build_tool_surface(["file"])
    prompt = mgr._build_emanation_prompt("Find all TODOs", schemas)
    assert "Find all TODOs" in prompt
    assert "daemon emanation" in prompt.lower() or "分神" in prompt


def test_run_emanation_returns_text(tmp_path):
    """Emanation runs a tool loop and returns final text."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "Task done. Found 3 files."
    mock_response.tool_calls = []
    mock_response.usage = MagicMock(input_tokens=0, output_tokens=0,
                                    thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_response)
    agent.service.create_session = MagicMock(return_value=mock_session)

    cancel = threading.Event()
    em_id = "em-test"
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id=em_id)
    mgr._emanations[em_id] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation(em_id, run_dir, schemas, dispatch,
                                "find stuff", cancel)
    assert "Found 3 files" in result


def test_run_emanation_dispatches_tools(tmp_path):
    """Emanation dispatches tool calls and feeds results back."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_handler = MagicMock(return_value={"content": "file text"})
    agent._tool_handlers["read"] = mock_handler

    tc = ToolCall(name="read", args={"file_path": "/tmp/x"}, id="tc-1")
    resp1 = MagicMock()
    resp1.text = ""
    resp1.tool_calls = [tc]
    resp1.usage = MagicMock(input_tokens=0, output_tokens=0,
                            thinking_tokens=0, cached_tokens=0)
    resp2 = MagicMock()
    resp2.text = "Task done. Read the file."
    resp2.tool_calls = []
    resp2.usage = MagicMock(input_tokens=0, output_tokens=0,
                            thinking_tokens=0, cached_tokens=0)

    mock_session = MagicMock()
    mock_session.send = MagicMock(side_effect=[resp1, resp2])
    agent.service.create_session = MagicMock(return_value=mock_session)
    agent.service.make_tool_result = MagicMock(return_value="mock_result")

    cancel = threading.Event()
    em_id = "em-test"
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id=em_id)
    mgr._emanations[em_id] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation(em_id, run_dir, schemas, dispatch,
                                "read a file", cancel)
    assert "Read the file" in result
    assert mock_handler.called


def test_run_emanation_respects_cancel_before_first_send(tmp_path):
    """Emanation exits immediately if pre-cancelled (before first LLM call)."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    agent.service.create_session = MagicMock(return_value=mock_session)

    cancel = threading.Event()
    cancel.set()
    em_id = "em-test"
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id=em_id)
    mgr._emanations[em_id] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation(em_id, run_dir, schemas, dispatch,
                                "do stuff", cancel)
    assert result == "[cancelled]"
    mock_session.send.assert_not_called()


def test_handle_emanate_dispatches_and_returns_ids(tmp_path):
    """emanate dispatches tasks and returns sequential IDs."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "task done — finished successfully"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "task A", "tools": ["file"]},
        {"task": "task B", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"
    assert result["count"] == 2
    assert result["ids"] == ["em-1", "em-2"]

    time.sleep(1)

    from lingtai_kernel.notifications import collect_notifications

    assert agent.inbox.empty()
    events = collect_notifications(agent._working_dir)["system"]["data"]["events"]
    assert len(events) == 2
    assert {e["source"] for e in events} == {"daemon"}
    assert {e["ref_id"] for e in events} == {"em-1", "em-2"}
    assert all("[daemon:em-" not in e["body"] for e in events)


def test_handle_emanate_allows_concurrent(tmp_path):
    """emanate succeeds even with existing emanations (no limit)."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    mgr._emanations["em-0"] = {"future": MagicMock(done=MagicMock(return_value=False)), "run_dir": None}
    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "x", "tools": ["file"]},
    ]})
    # No limit enforced — should succeed
    assert result["status"] == "dispatched"
    assert len(result["ids"]) == 1


def test_handle_list_shows_status(tmp_path):
    """list returns emanation statuses."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")

    done_future = MagicMock()
    done_future.done.return_value = True
    done_future.exception.return_value = None
    running_future = MagicMock()
    running_future.done.return_value = False

    mgr._emanations = {
        "em-1": {"future": done_future, "task": "task A", "start_time": time.time() - 10, "cancel_event": threading.Event(), "run_dir": None},
        "em-2": {"future": running_future, "task": "task B", "start_time": time.time() - 5, "cancel_event": threading.Event(), "run_dir": None},
    }
    result = mgr._handle_list()
    assert len(result["emanations"]) == 2
    statuses = {e["id"]: e["status"] for e in result["emanations"]}
    assert statuses["em-1"] == "done"
    assert statuses["em-2"] == "running"


def test_handle_ask_sends_followup(tmp_path):
    """ask buffers a follow-up for a running emanation."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    mgr._emanations["em-1"] = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "x",
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": None,
    }
    result = mgr._handle_ask("em-1", "also check tests/")
    assert result["status"] == "sent"
    assert mgr._emanations["em-1"]["followup_buffer"] == "also check tests/"


def test_handle_ask_collapses_multiple(tmp_path):
    """Multiple asks collapse into one buffer."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    mgr._emanations["em-1"] = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "x",
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": None,
    }
    mgr._handle_ask("em-1", "first")
    mgr._handle_ask("em-1", "second")
    assert mgr._emanations["em-1"]["followup_buffer"] == "first\n\nsecond"


def test_handle_reclaim_cancels_all(tmp_path):
    """reclaim sets cancel events and clears registry."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    cancel = threading.Event()
    pool = MagicMock()
    mgr._pools = [(pool, cancel)]
    mgr._emanations = {
        "em-1": {"future": MagicMock(done=MagicMock(return_value=False)), "cancel_event": cancel, "run_dir": None},
    }
    result = mgr._handle_reclaim()
    assert result["status"] == "reclaimed"
    assert result["cancelled"] == 1
    assert cancel.is_set()
    assert len(mgr._emanations) == 0
    pool.shutdown.assert_called_once()


def test_run_emanation_respects_cancel_mid_loop(tmp_path):
    """Emanation exits on cancel event between tool-call rounds."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")

    tc = ToolCall(name="read", args={}, id="tc-1")
    resp = MagicMock()
    resp.text = ""
    resp.tool_calls = [tc]
    resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                           thinking_tokens=0, cached_tokens=0)

    mock_session = MagicMock()
    agent.service.create_session = MagicMock(return_value=mock_session)
    agent.service.make_tool_result = MagicMock(return_value="mock_result")
    agent._tool_handlers["read"] = MagicMock(return_value={})

    cancel = threading.Event()
    call_count = [0]
    def send_and_cancel(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] >= 2:
            cancel.set()
        return resp
    mock_session.send = send_and_cancel

    em_id = "em-test"
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id=em_id)
    mgr._emanations[em_id] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation(em_id, run_dir, schemas, dispatch,
                                "do stuff", cancel)
    assert result == "[cancelled]"


def test_end_to_end_emanate_list_ask_reclaim(tmp_path):
    """Full lifecycle: emanate → list → ask → results arrive → reclaim."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    tc = ToolCall(name="read", args={"file_path": "/tmp/x"}, id="tc-1")
    resp1 = MagicMock()
    resp1.text = "Checking files..."
    resp1.tool_calls = [tc]
    resp1.usage = MagicMock(input_tokens=0, output_tokens=0,
                            thinking_tokens=0, cached_tokens=0)
    resp2 = MagicMock()
    resp2.text = "Task done. Summarized architecture."
    resp2.tool_calls = []
    resp2.usage = MagicMock(input_tokens=0, output_tokens=0,
                            thinking_tokens=0, cached_tokens=0)

    mock_session = MagicMock()
    mock_session.send = MagicMock(side_effect=[resp1, resp2])
    agent.service.create_session = MagicMock(return_value=mock_session)
    agent.service.make_tool_result = MagicMock(return_value="mock_result")

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "summarize architecture", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"
    assert result["ids"] == ["em-1"]

    time.sleep(0.1)
    time.sleep(2)

    list_result = mgr._handle_list()
    statuses = {e["id"]: e["status"] for e in list_result["emanations"]}
    assert statuses.get("em-1") == "done"

    from lingtai_kernel.notifications import collect_notifications

    assert agent.inbox.empty()
    events = collect_notifications(agent._working_dir)["system"]["data"]["events"]
    assert any(e["source"] == "daemon" and e["ref_id"] == "em-1" for e in events)
    assert any("Task done" in e["body"] for e in events)
    assert any("daemon(action=\"check\", id=\"em-1\")" in e["body"] for e in events)
    assert all("[daemon:em-" not in e["body"] for e in events)

    reclaim_result = mgr._handle_reclaim()
    assert reclaim_result["status"] == "reclaimed"



def test_on_emanation_done_publishes_system_notification_not_request(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    agent = _make_agent(tmp_path, ["daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, em_id="em-9")

    future = MagicMock()
    future.result.return_value = "final report long enough to notify"
    mgr._emanations["em-9"] = {
        "future": future,
        "task": "test task",
        "start_time": time.time(),
        "run_dir": rd,
    }

    mgr._on_emanation_done("em-9", "test task", future)

    assert agent.inbox.empty()
    notifications = collect_notifications(agent._working_dir)
    events = notifications["system"]["data"]["events"]
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "daemon"
    assert event["ref_id"] == "em-9"
    assert "Daemon em-9 done" in event["body"]
    assert "daemon(action=\"check\", id=\"em-9\")" in event["body"]
    assert "final report" in event["body"]
    assert "[daemon:em-" not in event["body"]


def test_on_emanation_done_failure_always_notifies(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    agent = _make_agent(tmp_path, ["daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, em_id="em-fail")

    future = MagicMock()
    future.result.side_effect = RuntimeError("boom")
    mgr._emanations["em-fail"] = {
        "future": future,
        "task": "test task",
        "start_time": time.time(),
        "run_dir": rd,
    }

    mgr._on_emanation_done("em-fail", "test task", future)

    assert agent.inbox.empty()
    events = collect_notifications(agent._working_dir)["system"]["data"]["events"]
    assert len(events) == 1
    assert events[0]["source"] == "daemon"
    assert events[0]["ref_id"] == "em-fail"
    assert "failed" in events[0]["body"]
    assert "boom" in events[0]["body"]
    assert "[daemon:em-" not in events[0]["body"]


def test_on_emanation_done_short_success_still_suppressed(tmp_path):
    from lingtai_kernel.notifications import collect_notifications

    agent = _make_agent(tmp_path, ["daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")
    rd = _make_run_dir(agent, em_id="em-short")

    future = MagicMock()
    future.result.return_value = "ok"
    mgr._emanations["em-short"] = {
        "future": future,
        "task": "test task",
        "start_time": time.time(),
        "run_dir": rd,
    }

    mgr._on_emanation_done("em-short", "test task", future)

    assert agent.inbox.empty()
    assert collect_notifications(agent._working_dir) == {}


def test_sequential_emanate_increments_ids(tmp_path):
    """Multiple emanate calls produce sequential IDs."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    r1 = mgr.handle({"action": "emanate", "tasks": [{"task": "a", "tools": ["file"]}]})
    time.sleep(0.5)
    r2 = mgr.handle({"action": "emanate", "tasks": [{"task": "b", "tools": ["file"]}]})

    assert r1["ids"] == ["em-1"]
    assert r2["ids"] == ["em-2"]


def test_emanate_creates_folder_on_disk(tmp_path):
    """_handle_emanate creates daemons/<run_id>/ before the future starts."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    release_send = threading.Event()

    def _blocking_send(*args, **kwargs):
        release_send.wait(timeout=5.0)
        return mock_resp

    mock_session.send = MagicMock(side_effect=_blocking_send)
    agent.service.create_session = MagicMock(return_value=mock_session)

    try:
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "find todos", "tools": ["file"]},
        ]})
        assert result["status"] == "dispatched"

        daemons_dir = agent._working_dir / "daemons"
        assert daemons_dir.is_dir()
        children = list(daemons_dir.iterdir())
        assert len(children) == 1
        folder = children[0]
        # Folder name matches em-1-<YYYYMMDD-HHMMSS>-<6 hex>
        assert re.fullmatch(r"em-1-\d{8}-\d{6}-[0-9a-f]{6}", folder.name)
        # daemon.json exists with state=running and identity fields
        data = json.loads((folder / "daemon.json").read_text())
        assert data["handle"] == "em-1"
        assert data["task"] == "find todos"
        assert data["tools"] == ["file"]
        assert data["state"] == "running"
    finally:
        release_send.set()


def test_reclaim_resets_next_id_to_1(tmp_path):
    """After reclaim, the next emanate gets em-1 again. Folder timestamps disambiguate."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    r1 = mgr.handle({"action": "emanate", "tasks": [{"task": "a", "tools": ["file"]}]})
    assert r1["ids"] == ["em-1"]
    time.sleep(0.5)
    mgr.handle({"action": "reclaim"})
    r2 = mgr.handle({"action": "emanate", "tasks": [{"task": "b", "tools": ["file"]}]})
    assert r2["ids"] == ["em-1"]  # handle reused after reclaim


def test_reclaim_preserves_folders(tmp_path):
    """reclaim stops processes but leaves daemon folders on disk."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "done"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    mgr.handle({"action": "emanate", "tasks": [{"task": "a", "tools": ["file"]}]})
    time.sleep(0.5)
    daemons_dir = agent._working_dir / "daemons"
    folders_before = list(daemons_dir.iterdir())
    assert len(folders_before) == 1

    mgr.handle({"action": "reclaim"})
    folders_after = list(daemons_dir.iterdir())
    assert folders_after == folders_before  # same folder still there


def test_handle_list_includes_run_id_and_path(tmp_path):
    """list output exposes run_id and path so inspectors know where to read."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "running"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                 thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    mgr.handle({"action": "emanate", "tasks": [{"task": "x", "tools": ["file"]}]})
    time.sleep(0.5)
    listing = mgr._handle_list()
    assert len(listing["emanations"]) >= 1
    em = listing["emanations"][0]
    assert "run_id" in em
    assert "path" in em
    assert em["run_id"].startswith("em-1-")
    assert em["path"].endswith(em["run_id"])


def test_e2e_emanate_writes_full_fs_artifact(tmp_path):
    """Full lifecycle: emanate → tool dispatch → completion → forensic folder."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    # Two LLM rounds: first emits a tool call, second completes.
    tc = ToolCall(name="read", args={"file_path": "/tmp/x"}, id="tc-1")
    resp1 = MagicMock()
    resp1.text = "Checking..."
    resp1.tool_calls = [tc]
    resp1.usage = MagicMock(input_tokens=100, output_tokens=20,
                             thinking_tokens=5, cached_tokens=10)
    resp2 = MagicMock()
    resp2.text = "Task done. Found 3 TODOs."
    resp2.tool_calls = []
    resp2.usage = MagicMock(input_tokens=80, output_tokens=15,
                             thinking_tokens=3, cached_tokens=5)

    mock_session = MagicMock()
    mock_session.send = MagicMock(side_effect=[resp1, resp2])
    agent.service.create_session = MagicMock(return_value=mock_session)
    agent.service.make_tool_result = MagicMock(return_value="mock_result")
    agent.service._base_url = "https://mock.example.com"
    agent._tool_handlers["read"] = MagicMock(return_value={"content": "file text"})

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "find TODOs", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"
    em_id = result["ids"][0]

    # Wait for completion
    time.sleep(2.0)

    # Find the folder
    daemons_dir = agent._working_dir / "daemons"
    folders = list(daemons_dir.iterdir())
    assert len(folders) == 1
    folder = folders[0]

    # daemon.json shows terminal state with full info
    data = json.loads((folder / "daemon.json").read_text())
    assert data["state"] == "done"
    assert data["finished_at"] is not None
    assert data["task"] == "find TODOs"
    assert data["tool_call_count"] == 1
    assert data["result_preview"] == "Task done. Found 3 TODOs."
    assert data["tokens"]["input"] == 180
    assert data["tokens"]["output"] == 35

    # chat_history.jsonl has user+assistant entries across both rounds
    chat_lines = (folder / "history" / "chat_history.jsonl").read_text().splitlines()
    assert len(chat_lines) >= 4  # task + assistant1 + tool_results + assistant2
    chat_entries = [json.loads(line) for line in chat_lines]
    assert any(e["role"] == "user" and e["kind"] == "task" for e in chat_entries)
    assert any(e["role"] == "assistant" and "Found 3 TODOs" in e["text"] for e in chat_entries)

    # events.jsonl has daemon_start, tool_call, tool_result, daemon_done
    events = [json.loads(line) for line in (folder / "logs" / "events.jsonl").read_text().splitlines()]
    event_types = [e["event"] for e in events]
    assert "daemon_start" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "daemon_done" in event_types

    # Daemon's own token ledger has 2 entries
    daemon_ledger = (folder / "logs" / "token_ledger.jsonl").read_text().splitlines()
    assert len(daemon_ledger) == 2

    # Parent's ledger has the same 2 entries, tagged
    parent_ledger_path = agent._working_dir / "logs" / "token_ledger.jsonl"
    parent_lines = parent_ledger_path.read_text().splitlines()
    daemon_tagged = [json.loads(line) for line in parent_lines
                     if json.loads(line).get("source") == "daemon"]
    assert len(daemon_tagged) == 2
    assert all(e["em_id"] == em_id for e in daemon_tagged)

    # Reclaim does not touch folder
    mgr.handle({"action": "reclaim"})
    assert folder.is_dir()
    # daemon.json still readable, still state=done (reclaim doesn't rewrite completed daemons)
    data_after = json.loads((folder / "daemon.json").read_text())
    assert data_after["state"] == "done"


def test_run_emanation_timeout_calls_mark_timeout(tmp_path):
    """When timeout_event is set alongside cancel_event, the run loop calls
    mark_timeout (state=timeout) instead of mark_cancelled (state=cancelled)."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id="em-test")

    cancel = threading.Event()
    timeout_event = threading.Event()
    # Watchdog-style: set both, with timeout_event marking the cause
    timeout_event.set()
    cancel.set()

    mgr._emanations["em-test"] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation("em-test", run_dir, schemas, dispatch,
                                 "task", cancel, timeout_event)
    assert result == "[cancelled]"
    data = json.loads(run_dir.daemon_json_path.read_text())
    assert data["state"] == "timeout"
    last_event = json.loads(run_dir.events_path.read_text().splitlines()[-1])
    assert last_event["event"] == "daemon_timeout"


def test_run_emanation_manual_reclaim_calls_mark_cancelled(tmp_path):
    """When cancel_event is set WITHOUT timeout_event, the run loop calls
    mark_cancelled (the manual-reclaim semantic)."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    mgr = agent.get_capability("daemon")
    schemas, dispatch = mgr._build_tool_surface(["file"])
    run_dir = _make_run_dir(agent, em_id="em-test")

    cancel = threading.Event()
    timeout_event = threading.Event()
    # Reclaim-style: only cancel_event set
    cancel.set()

    mgr._emanations["em-test"] = {
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
    }
    result = mgr._run_emanation("em-test", run_dir, schemas, dispatch,
                                 "task", cancel, timeout_event)
    assert result == "[cancelled]"
    data = json.loads(run_dir.daemon_json_path.read_text())
    assert data["state"] == "cancelled"
    last_event = json.loads(run_dir.events_path.read_text().splitlines()[-1])
    assert last_event["event"] == "daemon_cancelled"


def test_watchdog_sets_both_events(tmp_path):
    """Watchdog must set timeout_event before cancel_event so the run loop
    can observe the cause when it next checks."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    cancel = threading.Event()
    timeout_event = threading.Event()
    # Use a tiny timeout so the watchdog fires almost immediately
    mgr._watchdog(cancel, timeout_event, timeout=0.01)
    assert timeout_event.is_set()
    assert cancel.is_set()


def test_watchdog_returns_when_already_cancelled(tmp_path):
    """Watchdog must NOT set timeout_event when cancel_event was set first
    (manual reclaim path — timeout_event must remain unset)."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    cancel = threading.Event()
    timeout_event = threading.Event()
    cancel.set()  # simulate manual reclaim before watchdog deadline
    # Long timeout so we'd notice if it fired
    mgr._watchdog(cancel, timeout_event, timeout=60.0)
    assert cancel.is_set()
    assert not timeout_event.is_set()


# ---------------------------------------------------------------------------
# Per-emanation preset tests
# ---------------------------------------------------------------------------

def _write_preset_file(presets_dir, name, provider="deepseek", model="deepseek-v3",
                        api_key_env="DEEPSEEK_API_KEY", base_url=None):
    """Write a minimal preset JSON file to the presets directory."""
    import json
    preset = {
        "name": name,
        "description": f"{name} preset",
        "manifest": {
            "llm": {
                "provider": provider,
                "model": model,
                "api_key": None,
                "api_key_env": api_key_env,
                **({"base_url": base_url} if base_url else {}),
            },
            "capabilities": {"file": {}},
        },
    }
    (presets_dir / f"{name}.json").write_text(json.dumps(preset))
    return f"{name}.json"


def _make_agent_with_presets(tmp_path, presets_dir):
    """Create an agent whose init.json references a preset library."""
    from lingtai.agent import Agent
    svc = MagicMock()
    svc.provider = "mock"
    svc.model = "mock-model"
    svc.create_session = MagicMock()
    svc.make_tool_result = MagicMock()
    agent = Agent(
        svc,
        working_dir=tmp_path / "daemon-agent",
        capabilities=["file", "daemon"],
        config=AgentConfig(),
    )
    # Patch _read_init to return a manifest with a preset.path pointing to our dir
    agent._read_init = lambda: {
        "manifest": {
            "preset": {
                "active": "mock",
                "default": "mock",
                "path": str(presets_dir),
            },
            "llm": {"provider": "mock", "model": "mock-model"},
        }
    }
    return agent


def test_emanate_with_preset_validates_preset_exists(tmp_path, monkeypatch):
    """If a per-task preset is specified but doesn't exist in the library,
    refuse THE WHOLE BATCH (no partial emanations)."""
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset_file(presets_dir, "deepseek")

    agent = _make_agent_with_presets(tmp_path, presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    ghost_path = str(presets_dir / "ghost.json")
    # 'ghost' doesn't exist in the library
    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "task A", "tools": ["file"], "preset": ghost_path},
        {"task": "task B", "tools": ["file"]},  # valid task, but should be refused too
    ]})
    assert result["status"] == "error"
    assert "ghost" in result["message"]
    # No daemons spawned — whole batch refused
    daemons_dir = agent._working_dir / "daemons"
    assert not daemons_dir.exists() or not list(daemons_dir.iterdir())


def test_emanate_with_preset_unreachable_refuses(tmp_path, monkeypatch):
    """If the requested preset has connectivity 'unreachable', refuse the emanation."""
    from unittest.mock import patch
    import lingtai_kernel.preset_connectivity as preset_connectivity

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset_file(presets_dir, "deepseek", api_key_env="DEEPSEEK_API_KEY")

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    agent = _make_agent_with_presets(tmp_path, presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "deepseek.json")
    with patch.object(preset_connectivity, "_probe_host",
                      side_effect=OSError("connection refused")):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "task A", "tools": ["file"], "preset": preset_path},
        ]})
    assert result["status"] == "error"
    assert "unreachable" in result["message"]
    assert "deepseek" in result["message"]


def test_emanate_with_preset_no_credentials_refuses(tmp_path, monkeypatch):
    """If the requested preset has 'no_credentials', refuse the emanation."""
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset_file(presets_dir, "deepseek", api_key_env="DEEPSEEK_API_KEY_MISSING_XYZ")

    # Ensure the env var is NOT set
    monkeypatch.delenv("DEEPSEEK_API_KEY_MISSING_XYZ", raising=False)

    agent = _make_agent_with_presets(tmp_path, presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    preset_path = str(presets_dir / "deepseek.json")
    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "task A", "tools": ["file"], "preset": preset_path},
    ]})
    assert result["status"] == "error"
    assert "no_credentials" in result["message"]
    assert "deepseek" in result["message"]


def test_emanate_with_preset_passes_through(tmp_path, monkeypatch):
    """When preset is valid and reachable, emanation is scheduled and
    daemon.json records the preset name + provider + model."""
    from unittest.mock import patch
    import lingtai_kernel.preset_connectivity as preset_connectivity

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    _write_preset_file(presets_dir, "deepseek", provider="deepseek",
                       model="deepseek-v3", api_key_env="DEEPSEEK_API_KEY_TEST")

    monkeypatch.setenv("DEEPSEEK_API_KEY_TEST", "sk-test-key")

    agent = _make_agent_with_presets(tmp_path, presets_dir)
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "task done — finished successfully"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)

    # The preset's LLMService will call create_session — mock at the class level
    preset_svc = MagicMock()
    preset_svc.create_session = MagicMock(return_value=mock_session)
    preset_svc.make_tool_result = MagicMock(return_value="mock_result")
    preset_svc._base_url = "https://mock.deepseek.com"

    preset_path = str(presets_dir / "deepseek.json")
    with patch.object(preset_connectivity, "_probe_host", return_value=42), \
         patch("lingtai.llm.service.LLMService", return_value=preset_svc):
        result = mgr.handle({"action": "emanate", "tasks": [
            {"task": "find todos", "tools": ["file"], "preset": preset_path},
        ]})

    assert result["status"] == "dispatched"
    assert result["count"] == 1

    # Wait for completion
    time.sleep(1.5)

    # Check daemon.json records preset metadata
    daemons_dir = agent._working_dir / "daemons"
    folders = list(daemons_dir.iterdir())
    assert len(folders) == 1
    data = json.loads((folders[0] / "daemon.json").read_text())
    assert data.get("preset_name") == preset_path
    assert data.get("preset_provider") == "deepseek"
    assert data.get("preset_model") == "deepseek-v3"


def test_emanate_without_preset_inherits_parent(tmp_path, monkeypatch):
    """Existing behavior: omitting preset means daemon uses parent's
    currently-active LLM (no new LLMService created)."""
    agent = _make_agent(tmp_path, ["file", "daemon"])
    agent.inbox = queue.Queue()
    mgr = agent.get_capability("daemon")

    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = "task done — finished successfully"
    mock_resp.tool_calls = []
    mock_resp.usage = MagicMock(input_tokens=0, output_tokens=0,
                                thinking_tokens=0, cached_tokens=0)
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

    result = mgr.handle({"action": "emanate", "tasks": [
        {"task": "task A", "tools": ["file"]},
    ]})
    assert result["status"] == "dispatched"
    # Parent's service was used (create_session called on agent.service)
    time.sleep(1.0)
    assert agent.service.create_session.called

    # daemon.json has no preset_name (None)
    daemons_dir = agent._working_dir / "daemons"
    folders = list(daemons_dir.iterdir())
    assert len(folders) == 1
    data = json.loads((folders[0] / "daemon.json").read_text())
    assert data.get("preset_name") is None


def test_claude_code_env_strips_auth_overrides(monkeypatch):
    """Spawned claude-code processes must not inherit auth overrides.

    ANTHROPIC_* force the CLI off the user's Claude Code subscription onto
    API billing (GH #107); a stale CLAUDE_CODE_OAUTH_TOKEN can override a
    refreshed credentials.json and look like a false weekly limit (GH #189).
    """
    from lingtai.core.daemon import _claude_code_env, _CLAUDE_CODE_STRIP_ENV

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-leaked")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale-claude-code-oauth")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # sentinel non-stripped var
    monkeypatch.setenv("HOME", "/tmp/home")

    env = _claude_code_env()

    for key in _CLAUDE_CODE_STRIP_ENV:
        assert key not in env, f"{key} should be stripped from claude-code env"
    # Non-auth vars must pass through unchanged so claude can still find HOME,
    # PATH, CLAUDE_CONFIG_DIR, etc.
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/tmp/home"


def test_claude_code_env_noop_when_unset(monkeypatch):
    """When no Claude auth override vars are set, sanitized env equals os.environ."""
    import os
    from lingtai.core.daemon import _claude_code_env, _CLAUDE_CODE_STRIP_ENV

    for key in _CLAUDE_CODE_STRIP_ENV:
        monkeypatch.delenv(key, raising=False)

    env = _claude_code_env()
    assert env == os.environ


# ---------------------------------------------------------------------------
# CLI-backend ask: non-blocking dispatch + concurrent-ask guard (GH issue:
# daemon(ask) hanging the parent agent's tool turn). The handlers must
# return promptly even when the resumed `claude --resume` / `codex exec
# resume` process is slow/hangs, and a second ask while one is in flight
# must be refused with a clear busy error.
# ---------------------------------------------------------------------------


class _FakeStream:
    """A line-iterable stream that the test can append to live."""

    def __init__(self):
        import threading as _t
        self._lock = _t.Lock()
        self._lines: list[str] = []
        self._closed = False
        self._cond = _t.Condition(self._lock)

    def feed(self, line: str) -> None:
        with self._cond:
            self._lines.append(line)
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def __iter__(self):
        return self

    def __next__(self):
        with self._cond:
            while not self._lines and not self._closed:
                self._cond.wait()
            if self._lines:
                return self._lines.pop(0)
            raise StopIteration


class _FakeProc:
    """Subprocess.Popen stand-in with controllable stdout/stderr."""

    def __init__(self):
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode: int | None = None
        self.pid = 0  # _kill_process_group uses pid as pgid, but we override it
        self._wait_evt = threading.Event()

    def finish(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout.close()
        self.stderr.close()
        self._wait_evt.set()

    def wait(self, timeout=None):
        if not self._wait_evt.wait(timeout=timeout):
            import subprocess as _sp
            raise _sp.TimeoutExpired(cmd=["fake"], timeout=timeout)
        return self.returncode


def _install_fake_popen(monkeypatch, proc: _FakeProc):
    """Replace subprocess.Popen inside the daemon module with one that
    returns `proc` on next call, and neutralize _kill_process_group so it
    doesn't try to signal pid 0.
    """
    from lingtai.core import daemon as daemon_mod

    def fake_popen(cmd, **kwargs):
        return proc

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)
    # Don't actually os.killpg(0) — just mark the proc finished as if killed.
    monkeypatch.setattr(daemon_mod, "_kill_process_group",
                        lambda p: p.finish(returncode=-15))


def _cli_entry(mgr, agent, em_id: str, backend: str, session_id: str) -> dict:
    """Register an em-N entry as if it had been spawned by _handle_emanate_cli,
    with a real run_dir whose <backend>_session_id is already populated.
    """
    run_dir = _make_run_dir(agent, em_id=em_id)
    # Simulate the streaming handler having captured the session id.
    if backend == "claude-code":
        run_dir._state["claude_session_id"] = session_id
    elif backend == "codex":
        run_dir._state["codex_session_id"] = session_id
    run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
    entry = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "primary task",
        "start_time": time.time(),
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": backend,
        "ask_in_flight": False,
        "ask_future": None,
    }
    mgr._emanations[em_id] = entry
    return entry


def test_ask_claude_code_returns_immediately_when_subprocess_hangs(tmp_path, monkeypatch):
    """`daemon(ask)` against a claude-code emanation must not block the
    parent's tool turn even when the resumed subprocess is slow."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    _cli_entry(mgr, agent, "em-1", "claude-code", "claude-sess-abc")

    t0 = time.monotonic()
    result = mgr._handle_ask("em-1", "follow-up please")
    elapsed = time.monotonic() - t0

    # The handler must return synchronously and quickly — the subprocess
    # is still hanging (no stdout fed, no finish() called). Generous bound
    # so this passes on a loaded CI box but still fails the regression
    # (which blocked for up to self._timeout seconds, default 3600).
    assert elapsed < 1.0, f"ask blocked for {elapsed:.2f}s"
    assert result["status"] == "sent"
    assert result.get("async") is True
    assert result["id"] == "em-1"

    # The ask is tracked as in-flight until the worker observes EOF.
    assert mgr._emanations["em-1"]["ask_in_flight"] is True
    ask_future = mgr._emanations["em-1"]["ask_future"]
    assert ask_future is not None and not ask_future.done()

    # Drive the fake subprocess to completion and let the worker drain.
    proc.stdout.feed(
        '{"type":"result","result":"all done","is_error":false}\n'
    )
    proc.finish(returncode=0)
    ask_future.result(timeout=5)

    # Worker cleared the in-flight flag and persisted progress to the run_dir.
    assert mgr._emanations["em-1"]["ask_in_flight"] is False
    run_dir = mgr._emanations["em-1"]["run_dir"]
    assert run_dir._state.get("last_output") is not None
    # The dispatched marker + the assistant/result text should both have
    # landed as cli_output events.
    events_text = run_dir.events_path.read_text()
    assert "ask dispatched" in events_text


def test_ask_claude_code_second_ask_is_busy(tmp_path, monkeypatch):
    """While an ask is in flight, a second concurrent ask returns busy."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    _cli_entry(mgr, agent, "em-1", "claude-code", "claude-sess-abc")

    first = mgr._handle_ask("em-1", "first follow-up")
    assert first["status"] == "sent"

    second = mgr._handle_ask("em-1", "second follow-up")
    assert second["status"] == "busy"
    assert "still" in second["message"].lower()
    assert second["id"] == "em-1"

    # Let the first one finish so the test teardown is clean.
    proc.finish(returncode=0)
    mgr._emanations["em-1"]["ask_future"].result(timeout=5)

    # After it clears, another ask should succeed.
    proc2 = _FakeProc()
    _install_fake_popen(monkeypatch, proc2)
    third = mgr._handle_ask("em-1", "third follow-up")
    assert third["status"] == "sent"
    proc2.finish(returncode=0)
    mgr._emanations["em-1"]["ask_future"].result(timeout=5)


def test_ask_codex_returns_immediately_when_subprocess_hangs(tmp_path, monkeypatch):
    """Codex ask must also be non-blocking."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    _cli_entry(mgr, agent, "em-1", "codex", "codex-thread-xyz")

    t0 = time.monotonic()
    result = mgr._handle_ask("em-1", "what next?")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"codex ask blocked for {elapsed:.2f}s"
    assert result["status"] == "sent"
    assert result.get("async") is True

    assert mgr._emanations["em-1"]["ask_in_flight"] is True
    ask_future = mgr._emanations["em-1"]["ask_future"]

    # Drive to completion with a synthetic codex JSONL stream.
    proc.stdout.feed(
        '{"type":"item.completed","item":{"type":"agent_message","text":"reply text"}}\n'
    )
    proc.stdout.feed('{"type":"turn.completed"}\n')
    proc.finish(returncode=0)
    ask_future.result(timeout=5)

    assert mgr._emanations["em-1"]["ask_in_flight"] is False


def test_ask_codex_second_ask_is_busy(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    _cli_entry(mgr, agent, "em-1", "codex", "codex-thread-xyz")

    first = mgr._handle_ask("em-1", "first follow-up")
    assert first["status"] == "sent"

    second = mgr._handle_ask("em-1", "second follow-up")
    assert second["status"] == "busy"

    proc.finish(returncode=0)
    mgr._emanations["em-1"]["ask_future"].result(timeout=5)


def test_ask_claude_code_missing_session_id_still_synchronous(tmp_path):
    """If the session id hasn't been captured yet, the pre-flight check
    must still respond immediately (no subprocess spawn, no busy flag)."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    # Build an entry whose run_dir has NO claude_session_id.
    run_dir = _make_run_dir(agent, em_id="em-1")
    mgr._emanations["em-1"] = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "x",
        "start_time": time.time(),
        "cancel_event": threading.Event(),
        "timeout_event": threading.Event(),
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": run_dir,
        "backend": "claude-code",
        "ask_in_flight": False,
        "ask_future": None,
    }
    result = mgr._handle_ask("em-1", "anything")
    assert result["status"] == "error"
    assert "session" in result["message"].lower()
    # Must not have flipped the in-flight flag on a pre-flight failure.
    assert mgr._emanations["em-1"]["ask_in_flight"] is False


def test_ask_lingtai_backend_unchanged(tmp_path):
    """The builtin lingtai backend ask still buffers into followup_buffer
    and is unaffected by the CLI-ask refactor."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    mgr._emanations["em-1"] = {
        "future": MagicMock(done=MagicMock(return_value=False)),
        "task": "x",
        "followup_buffer": "",
        "followup_lock": threading.Lock(),
        "run_dir": None,
        # No 'backend' key → routes to the in-process followup buffer path.
    }
    result = mgr._handle_ask("em-1", "buffered follow-up")
    assert result["status"] == "sent"
    assert mgr._emanations["em-1"]["followup_buffer"] == "buffered follow-up"


def test_ask_claude_code_reclaim_suppresses_followup_notification(tmp_path, monkeypatch):
    """If the emanation is reclaimed while a CLI ask is mid-flight, the
    worker must NOT publish a follow-up notification when it eventually
    exits — the parent has already torn the entry down."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    entry = _cli_entry(mgr, agent, "em-1", "claude-code", "claude-sess-abc")
    # Register a fake pool entry so _handle_reclaim's pool.shutdown loop
    # doesn't hit an empty list (the cli entry helper doesn't add one).
    mgr._pools = [(MagicMock(), threading.Event())]
    mgr._cli_procs.append(proc)  # so reclaim's kill loop targets it too

    # Count published notifications + log events.
    published: list[tuple] = []
    monkeypatch.setattr(
        mgr, "_publish_daemon_notification",
        lambda em_id, *, status, text, run_dir=None: published.append((em_id, status)),
    )
    logged: list[tuple] = []
    real_log = mgr._log
    def _log_capture(event, **fields):
        logged.append((event, fields))
        return real_log(event, **fields)
    monkeypatch.setattr(mgr, "_log", _log_capture)

    # Dispatch the ask — returns immediately, worker is blocked on stdout.
    result = mgr._handle_ask("em-1", "anything")
    assert result["status"] == "sent"
    ask_future = entry["ask_future"]
    assert not ask_future.done()

    # Reclaim races the in-flight ask. _handle_reclaim kills _cli_procs
    # (which marks the fake proc finished via the patched _kill_process_group).
    mgr._handle_reclaim()
    assert "em-1" not in mgr._emanations

    # Worker finishes its drain after the kill — it would otherwise have
    # published a "follow-up failed" notification on the non-zero returncode.
    ask_future.result(timeout=5)

    assert published == [], (
        f"reclaimed ask must not publish follow-up notifications, got {published}"
    )
    post_reclaim_logs = [e for e, _ in logged if e == "daemon_ask_post_reclaim"]
    assert post_reclaim_logs, (
        "expected a daemon_ask_post_reclaim log event when worker tried to "
        f"publish after reclaim; got {[e for e, _ in logged]}"
    )


def _run_silent_subprocess_ask_test(tmp_path, monkeypatch, backend: str):
    """Shared body for the claude-code + codex silent-subprocess tests.

    Models the regression: the resumed CLI is spawned but never writes a
    single byte to stdout and never exits. Before this fix, the worker's
    `for raw_line in proc.stdout` blocked the worker thread forever; the
    `if time.monotonic() > deadline` check inside the loop never ran.
    The fix routes stdout through a daemon reader thread + `queue.get`
    with a deadline, so the worker observes the timeout regardless.
    """
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    # Make the test fast — without this the default 3600s timeout would
    # itself be the bug we'd be reproducing.
    mgr._timeout = 0.5

    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    session_id = ("claude-sess-silent" if backend == "claude-code"
                  else "codex-thread-silent")
    entry = _cli_entry(mgr, agent, "em-1", backend, session_id)

    published: list[tuple] = []
    monkeypatch.setattr(
        mgr, "_publish_daemon_notification",
        lambda em_id, *, status, text, run_dir=None:
            published.append((em_id, status, text)),
    )
    logged: list[tuple] = []
    real_log = mgr._log
    def _log_capture(event, **fields):
        logged.append((event, fields))
        return real_log(event, **fields)
    monkeypatch.setattr(mgr, "_log", _log_capture)

    t0 = time.monotonic()
    result = mgr._handle_ask("em-1", "anything")
    dispatch_elapsed = time.monotonic() - t0

    # Dispatcher itself must still return promptly even with a silent
    # subprocess (this part already worked before the fix).
    assert dispatch_elapsed < 1.0
    assert result["status"] == "sent"
    assert result["async"] is True

    ask_future = entry["ask_future"]
    # The worker should observe the deadline (mgr._timeout=0.5s) and
    # finish — without the fix this future.result(timeout=3) would itself
    # time out because the worker is blocked on proc.stdout iteration.
    worker_result = ask_future.result(timeout=3.0)

    total_elapsed = time.monotonic() - t0
    assert total_elapsed < 2.5, (
        f"worker took {total_elapsed:.2f}s — likely still blocked on stdout. "
        "Did _iter_stdout_with_deadline get bypassed?"
    )

    # Worker must report timeout error and clean up everything.
    assert worker_result["status"] == "error"
    assert "timed out" in worker_result["message"]
    assert entry["ask_in_flight"] is False, (
        "ask_in_flight must clear so a subsequent ask isn't permanently busy"
    )
    assert proc not in mgr._cli_procs, (
        "proc must be removed from _cli_procs so reclaim/list see clean state"
    )

    # The fake _kill_process_group (installed by _install_fake_popen)
    # marks the proc finished with returncode=-15 (simulating SIGTERM).
    # The real implementation actually SIGTERMs the process group.
    assert proc.returncode is not None, "subprocess must have been killed"

    # Publish a "follow-up failed" notification so the parent agent sees
    # the timeout instead of nothing.
    failures = [p for p in published if p[1] == "follow-up failed"]
    assert failures, f"expected a follow-up failed notification, got {published}"
    assert "timed out" in failures[0][2]

    # No post-reclaim log — reclaim didn't happen, the entry is still live.
    post_reclaim_logs = [e for e, _ in logged if e == "daemon_ask_post_reclaim"]
    assert not post_reclaim_logs


def test_ask_claude_code_silent_subprocess_enforces_timeout(tmp_path, monkeypatch):
    """REGRESSION: a silent `claude --resume` subprocess (no stdout, never
    exits) must NOT hang the ask worker indefinitely. Worker must observe
    self._timeout, kill the proc group, clear ask_in_flight, and publish
    a follow-up failed notification."""
    _run_silent_subprocess_ask_test(tmp_path, monkeypatch, "claude-code")


def test_ask_codex_silent_subprocess_enforces_timeout(tmp_path, monkeypatch):
    """REGRESSION: same as the claude-code case but for
    `codex exec resume`. Symmetric worker, same fix path."""
    _run_silent_subprocess_ask_test(tmp_path, monkeypatch, "codex")


def test_ask_worker_exception_is_logged(tmp_path, monkeypatch):
    """An unexpected exception in the ask worker must be logged via
    daemon_ask_worker_error and recorded into the run_dir as a cli_output
    line so daemon(check) shows what happened."""
    agent = _make_agent(tmp_path, ["daemon"])
    mgr = agent.get_capability("daemon")
    proc = _FakeProc()
    _install_fake_popen(monkeypatch, proc)
    entry = _cli_entry(mgr, agent, "em-1", "claude-code", "claude-sess-abc")

    # Replace the worker with one that raises immediately. _on_ask_done
    # runs as the future's done-callback so the exception is surfaced
    # rather than swallowed.
    def boom(em_id, entry, proc, run_dir):
        proc.finish(returncode=0)  # drain so any background reader doesn't hang
        raise RuntimeError("simulated worker crash")
    monkeypatch.setattr(mgr, "_run_ask_claude_code_stream", boom)

    logged: list[tuple] = []
    real_log = mgr._log
    def _log_capture(event, **fields):
        logged.append((event, fields))
        return real_log(event, **fields)
    monkeypatch.setattr(mgr, "_log", _log_capture)

    mgr._handle_ask("em-1", "anything")
    ask_future = entry["ask_future"]

    # Wait for the worker future AND its done-callback to run.
    try:
        ask_future.result(timeout=5)
    except RuntimeError:
        pass  # expected
    # add_done_callback runs synchronously after .result returns/raises,
    # but be tolerant of scheduling.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(e == "daemon_ask_worker_error" for e, _ in logged):
            break
        time.sleep(0.05)

    err_logs = [f for e, f in logged if e == "daemon_ask_worker_error"]
    assert err_logs, f"expected daemon_ask_worker_error, got {[e for e, _ in logged]}"
    assert err_logs[0]["em_id"] == "em-1"
    assert err_logs[0]["exception"] == "RuntimeError"
    assert "simulated worker crash" in err_logs[0]["message"]

    # Run_dir should have a stderr cli_output line marking the worker error.
    events_text = entry["run_dir"].events_path.read_text()
    assert "[ask worker error]" in events_text
    assert "RuntimeError" in events_text

    # ask_in_flight cleared even though worker raised before its finally
    # would have run.
    assert entry["ask_in_flight"] is False
