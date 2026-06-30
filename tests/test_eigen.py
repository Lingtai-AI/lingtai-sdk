"""Tests for psyche intrinsic — core self-management (pad + context)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from tests._service_helpers import make_gemini_mock_service as make_mock_service
from tests._molt_helpers import write_session_journal as _write_session_journal


# ---------------------------------------------------------------------------
# Pad edit
# ---------------------------------------------------------------------------


def test_psyche_pad_edit(tmp_path):
    """psyche pad edit writes to system/pad.md."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({"object": "pad", "action": "edit", "content": "hello world"})
    assert result["status"] == "ok"
    pad_path = agent._working_dir / "system" / "pad.md"
    assert pad_path.read_text() == "hello world"
    agent.stop(timeout=1.0)


def test_psyche_pad_edit_empty_clears(tmp_path):
    """psyche pad edit with explicit content='' clears the pad file."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    # First write something
    agent._intrinsics["psyche"]({"object": "pad", "action": "edit", "content": "data"})
    # Then clear it (must pass content="" — empty args alone is rejected)
    result = agent._intrinsics["psyche"]({"object": "pad", "action": "edit", "content": ""})
    assert result["status"] == "ok"
    pad_path = agent._working_dir / "system" / "pad.md"
    assert pad_path.read_text() == ""
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Pad load
# ---------------------------------------------------------------------------


def test_psyche_pad_load(tmp_path):
    """psyche pad load injects into system prompt."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    agent.start()
    try:
        # Write pad file first
        system_dir = agent._working_dir / "system"
        system_dir.mkdir(exist_ok=True)
        (system_dir / "pad.md").write_text("loaded content")

        result = agent._intrinsics["psyche"]({"object": "pad", "action": "load"})
        assert result["status"] == "ok"
        section = agent._prompt_manager.read_section("pad")
        assert "loaded content" in section
    finally:
        agent.stop()


def test_psyche_pad_load_empty(tmp_path):
    """psyche pad load with empty file deletes section."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    agent.start()
    try:
        result = agent._intrinsics["psyche"]({"object": "pad", "action": "load"})
        assert result["status"] == "ok"
        section = agent._prompt_manager.read_section("pad")
        assert section is None or section.strip() == ""
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Context molt (agent-callable)
# ---------------------------------------------------------------------------


def test_psyche_molt_uses_summary(tmp_path):
    """molt wipes context and re-injects agent's summary."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = BaseAgent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
    )
    agent.start()
    try:
        from lingtai_kernel.llm.interface import ToolCallBlock

        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message(
            [TextBlock(text="Hi there.")],
        )
        # Simulate the assistant turn that emitted the molt — it must be
        # in the live interface before psyche runs (the wire layer records
        # assistant tool_calls before dispatching). _context_molt locates
        # this block by tc.id and replays it into the fresh session.
        journal_path = _write_session_journal(agent)
        molt_wire_id = "toolu_test_molt_uses_summary"
        molt_summary = "Key finding: X=42. Task: analyze Y."
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_wire_id,
                name="psyche",
                args={
                    "object": "context", "action": "molt", "summary": molt_summary,
                    "session_journal_path": journal_path,
                },
            ),
        ])

        result = agent._intrinsics["psyche"]({
            "object": "context",
            "action": "molt",
            "summary": molt_summary,
            "_tc_id": molt_wire_id,
            "session_journal_path": journal_path,
        })
        assert result["status"] == "ok"
        # The summary now lives in the replayed ToolCallBlock's args, not
        # in a user message — the agent will read it from the assistant
        # turn the same way it reads any past tool_use it emitted.
        iface = agent._session._chat.interface
        assistant_entries = [e for e in iface.entries if e.role == "assistant"]
        assert assistant_entries
        last_calls = [b for b in assistant_entries[-1].content if isinstance(b, ToolCallBlock)]
        assert last_calls and last_calls[0].id == molt_wire_id
        assert "X=42" in last_calls[0].args.get("summary", "")
    finally:
        agent.stop()


def test_psyche_molt_rejects_empty_summary(tmp_path):
    """molt with empty summary returns error — agent must write a real briefing."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({
        "object": "context", "action": "molt", "summary": "",
    })
    assert "error" in result
    assert "empty" in result["error"].lower()
    agent.stop(timeout=1.0)


def test_psyche_molt_rejects_missing_summary(tmp_path):
    """molt without summary arg returns error."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({"object": "context", "action": "molt"})
    assert "error" in result
    assert "required" in result["error"].lower()
    agent.stop(timeout=1.0)


def test_eigen_schema_has_context_molt(tmp_path):
    """Schema exposes context/summary without strict-incompatible combinators."""
    from lingtai_kernel.intrinsics.psyche import get_schema
    s = get_schema("en")
    assert "context" in s["properties"]["object"]["enum"]
    assert "summary" in s["properties"]
    assert {"object", "action"}.issubset(set(s["required"]))
    for keyword in ("allOf", "oneOf", "anyOf", "not"):
        assert keyword not in s


def test_psyche_rejects_invalid_object_action_pair(tmp_path):
    """Runtime validation still enforces per-object actions without schema allOf."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({"object": "context", "action": "load"})
    assert "error" in result
    assert "Invalid action" in result["error"]
    assert "molt" in result["error"]
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Context forget (internal only)
# ---------------------------------------------------------------------------


def test_eigen_forget_wipes_context(tmp_path):
    """context_forget nuclear wipes the session."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock
    from lingtai_kernel.intrinsics.psyche import context_forget

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = BaseAgent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
    )
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("test")
        agent._session._chat.interface.add_assistant_message(
            [TextBlock(text="response")],
        )

        result = context_forget(agent)
        assert result["status"] == "ok"
        assert result["tokens_before"] > 0
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_eigen_unknown_object(tmp_path):
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({"object": "bogus", "action": "edit"})
    assert "error" in result
    agent.stop(timeout=1.0)


def test_eigen_unknown_action(tmp_path):
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = agent._intrinsics["psyche"]({"object": "pad", "action": "bogus"})
    assert "error" in result
    agent.stop(timeout=1.0)


def test_eigen_is_intrinsic_not_pad(tmp_path):
    """psyche replaces pad in intrinsics."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    assert "psyche" in agent._intrinsics
    assert "pad" not in agent._intrinsics
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Name action (true name)
# ---------------------------------------------------------------------------

def test_eigen_name_sets_agent_name(tmp_path):
    """psyche name action sets agent true name."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    assert agent.agent_name is None
    result = agent._intrinsics["psyche"]({"object": "name", "action": "set", "content": "悟空"})
    assert result["status"] == "ok"
    assert result["name"] == "悟空"
    assert agent.agent_name == "悟空"
    agent.stop(timeout=1.0)


def test_eigen_name_rejects_second_set(tmp_path):
    """psyche name action fails if already named."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test", agent_name="alice")
    result = agent._intrinsics["psyche"]({"object": "name", "action": "set", "content": "bob"})
    assert "error" in result
    assert agent.agent_name == "alice"  # unchanged
    agent.stop(timeout=1.0)


def test_eigen_name_rejects_empty(tmp_path):
    """psyche name action fails with empty name."""
    agent = BaseAgent(service=make_mock_service(), working_dir=tmp_path / "test")
    result = agent._intrinsics["psyche"]({"object": "name", "action": "set", "content": ""})
    assert "error" in result
    assert agent.agent_name is None  # still unnamed
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Molt snapshots — discrete pre-molt interface dumps for past-self consultation
# ---------------------------------------------------------------------------


def _agent_with_session(tmp_path):
    """Build a BaseAgent with a mock session that uses a real ChatInterface
    so molt code can actually walk and serialize entries. Returns the agent;
    caller is responsible for stop()."""
    from lingtai_kernel.llm.interface import ChatInterface

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = BaseAgent(
        service=svc, agent_name="snapshot-test", working_dir=tmp_path / "snapshot-test",
    )
    agent.start()
    return agent


def test_snapshot_written_on_agent_molt(tmp_path):
    """Agent-initiated molt drops a discrete snapshot file under history/snapshots/."""
    import json
    from lingtai_kernel.llm.interface import TextBlock, ToolCallBlock

    agent = _agent_with_session(tmp_path)
    try:
        agent._session.ensure_session()
        iface = agent._session._chat.interface
        iface.add_user_message("Hello")
        iface.add_assistant_message([TextBlock(text="Hi.")])

        # The molt's own tool_call lives in the tail entry pre-molt.
        journal_path = _write_session_journal(agent)
        molt_id = "toolu_test_snapshot_agent"
        molt_summary = "Briefing: completed task X, next is Y."
        iface.add_assistant_message([
            ToolCallBlock(
                id=molt_id, name="psyche",
                args={
                    "object": "context", "action": "molt", "summary": molt_summary,
                    "session_journal_path": journal_path,
                },
            ),
        ])

        result = agent._intrinsics["psyche"]({
            "object": "context", "action": "molt",
            "summary": molt_summary, "_tc_id": molt_id,
            "session_journal_path": journal_path,
        })
        assert result["status"] == "ok"

        snapshots_dir = agent._working_dir / "history" / "snapshots"
        files = sorted(snapshots_dir.glob("snapshot_*_*.json"))
        assert len(files) == 1, f"expected 1 snapshot, found {files}"
        # Filename uses molt_count (1 — first molt) as the leading number.
        assert files[0].name.startswith("snapshot_1_"), files[0].name

        payload = json.loads(files[0].read_text())
        assert payload["schema_version"] == 1
        assert payload["molt_count"] == 1
        assert payload["molt_summary"] == molt_summary
        assert payload["molt_source"] == "agent"
        assert payload["before_tokens"] > 0
        assert payload["agent_name"] == "snapshot-test"
        assert isinstance(payload["interface"], list)
        # The molt's own tool_call IS preserved (history fidelity), but
        # must be closed with a synthetic tool_result so the snapshot is
        # self-contained and sendable.
        molt_call_found = False
        molt_result_found = False
        for entry in payload["interface"]:
            for block in entry.get("content", []):
                if block.get("type") == "tool_call" and block.get("id") == molt_id:
                    molt_call_found = True
                if (block.get("type") == "tool_result"
                        and block.get("id") == molt_id):
                    content = block.get("content", {})
                    if isinstance(content, str) and "[kernel notice" in content:
                        molt_result_found = True
                    elif isinstance(content, dict) and "error" in content:
                        molt_result_found = True
        assert molt_call_found, "molt tool_call must be preserved in snapshot"
        assert molt_result_found, "molt tool_call must be closed with a synthetic result"
    finally:
        agent.stop()


def test_snapshot_written_on_system_forget(tmp_path):
    """System-initiated context_forget also writes a snapshot, source != 'agent'."""
    import json
    from lingtai_kernel.llm.interface import TextBlock
    from lingtai_kernel.intrinsics.psyche import context_forget

    agent = _agent_with_session(tmp_path)
    try:
        agent._session.ensure_session()
        iface = agent._session._chat.interface
        iface.add_user_message("test")
        iface.add_assistant_message([TextBlock(text="response")])

        result = context_forget(agent, source="warning_ladder")
        assert result["status"] == "ok"

        snapshots_dir = agent._working_dir / "history" / "snapshots"
        files = sorted(snapshots_dir.glob("snapshot_*_*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["molt_source"] == "warning_ladder"
        assert payload["molt_count"] == 1
        # System-initiated path: no agent tool_call exists pre-molt, so the
        # whole interface is captured. No exclusion needed.
        assert isinstance(payload["interface"], list)
        assert any(
            entry.get("role") == "user" for entry in payload["interface"]
        )
    finally:
        agent.stop()


def test_snapshot_filename_uses_molt_count(tmp_path):
    """Successive molts produce successive molt_count values in filenames."""
    from lingtai_kernel.intrinsics.psyche import context_forget

    agent = _agent_with_session(tmp_path)
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("turn 1")
        context_forget(agent, source="warning_ladder")

        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("turn 2")
        context_forget(agent, source="aed", attempts=3)

        snapshots_dir = agent._working_dir / "history" / "snapshots"
        files = sorted(snapshots_dir.glob("snapshot_*_*.json"))
        assert len(files) == 2
        # First has molt_count=1, second has molt_count=2.
        assert files[0].name.startswith("snapshot_1_")
        assert files[1].name.startswith("snapshot_2_")
    finally:
        agent.stop()


def test_snapshot_helper_swallows_failures(tmp_path):
    """_write_molt_snapshot is best-effort — it returns None on any failure
    rather than propagating, so a broken disk can't block a molt."""
    from lingtai_kernel.intrinsics import psyche

    # Block the snapshots dir by planting a file where its parent should be.
    (tmp_path / "history").write_text("blocker — not a directory")

    broken_agent = MagicMock()
    broken_agent._working_dir = tmp_path
    broken_agent._agent_id = "x"
    broken_agent._agent_name = "x"

    result = psyche._write_molt_snapshot(
        broken_agent, MagicMock(),
        before_tokens=100, summary="x", source="agent", molt_count=1,
    )
    assert result is None  # swallowed, returned None
    # And a log call was attempted (best-effort — agent._log is a MagicMock
    # so it accepts anything).
    broken_agent._log.assert_called()
