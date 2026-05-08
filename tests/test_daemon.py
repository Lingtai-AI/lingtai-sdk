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
    schemas, dispatch = mgr._build_tool_surface(["file", "avatar", "daemon"])
    names = {s.name for s in schemas}
    assert "daemon" not in names
    assert "avatar" not in names
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

    messages = []
    while not agent.inbox.empty():
        messages.append(agent.inbox.get_nowait())
    assert len(messages) == 2


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

    messages = []
    while not agent.inbox.empty():
        messages.append(agent.inbox.get_nowait())
    assert len(messages) >= 1
    texts = [m.content for m in messages]
    assert any("Task done" in t for t in texts)

    reclaim_result = mgr._handle_reclaim()
    assert reclaim_result["status"] == "reclaimed"


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
    mock_session.send = MagicMock(return_value=mock_resp)
    agent.service.create_session = MagicMock(return_value=mock_session)

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
    from unittest.mock import patch
    import lingtai_kernel.preset_connectivity as preset_connectivity

    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    preset_file = _write_preset_file(presets_dir, "deepseek")

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
    preset_file = _write_preset_file(presets_dir, "deepseek", api_key_env="DEEPSEEK_API_KEY")

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
    from lingtai.llm.service import LLMService as ConcreteLLMService
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
