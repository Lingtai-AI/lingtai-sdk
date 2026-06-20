"""Tests for the psyche intrinsic — identity, pad, context, and name."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai_kernel.base_agent import BaseAgent
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _call(agent, args: dict) -> dict:
    """Dispatch a psyche tool call directly through the registered intrinsic."""
    return agent._intrinsics["psyche"](args)


_VALID_JOURNAL = """\
---
name: 2026-06-19-molt-1-test
description: A test session journal entry for the molt gate.
date: 2026-06-19
molt_count: 1
type: session-journal
---

## What this segment was about
Testing.

## Accomplishments
Wrote a valid session journal.
"""


def _write_session_journal(agent, rel="knowledge/session-journal/2026-06-19-molt-1-test/KNOWLEDGE.md"):
    """Write a valid session-journal entry so an agent-initiated molt passes
    the session-journal gate (issue #350). Returns the workdir-relative path."""
    path = agent._working_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_VALID_JOURNAL, encoding="utf-8")
    return rel


# ---------------------------------------------------------------------------
# Setup / registration
# ---------------------------------------------------------------------------


def test_psyche_is_intrinsic(tmp_path):
    """Psyche is now an intrinsic, not a capability — always registered."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    assert "psyche" in agent._intrinsics
    assert "eigen" not in agent._intrinsics
    agent.stop(timeout=1.0)


def test_psyche_capability_silently_dropped(tmp_path):
    """Legacy init.json with capabilities=['psyche'] should be tolerated —
    psyche is filtered out, the intrinsic still provides the tool."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    assert "psyche" not in [name for name, _ in agent._capabilities]
    assert "psyche" in agent._intrinsics
    agent.stop(timeout=1.0)


def test_anima_alias_removed(tmp_path):
    """'anima' alias was removed — agent skips it (unknown capabilities are logged, not raised)."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        capabilities=["anima"],
    )
    assert "anima" not in [name for name, _ in agent._capabilities]
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Lingtai (identity) actions
# ---------------------------------------------------------------------------


def test_lingtai_update_writes_lingtai_md(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        covenant="You are helpful",
    )
    result = _call(agent, {"object": "lingtai", "action": "update", "content": "I am a PDF specialist"})
    assert result["status"] == "ok"
    character = (agent.working_dir / "system" / "lingtai.md").read_text()
    assert character == "I am a PDF specialist"
    agent.stop(timeout=1.0)


def test_lingtai_update_empty_clears(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    _call(agent, {"object": "lingtai", "action": "update", "content": "something"})
    _call(agent, {"object": "lingtai", "action": "update", "content": ""})
    character = (agent.working_dir / "system" / "lingtai.md").read_text()
    assert character == ""
    agent.stop(timeout=1.0)


def test_lingtai_load_writes_character_section(tmp_path):
    """lingtai.md populates the standalone `character` section, NOT covenant.

    The two are semantically distinct: `covenant` is the operator-supplied
    contract (covenant.md alone); `character` is the agent's self-authored
    identity (lingtai.md alone). The character text must never leak into the
    covenant section.
    """
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
        covenant="You are helpful",
    )
    agent.start()
    try:
        _call(agent, {"object": "lingtai", "action": "update", "content": "I specialize in PDFs"})
        _call(agent, {"object": "lingtai", "action": "load"})

        # character section carries lingtai.md alone
        character = agent._prompt_manager.read_section("character")
        assert character is not None
        assert "I specialize in PDFs" in character

        # covenant section carries covenant.md alone — character text not folded in
        covenant = agent._prompt_manager.read_section("covenant") or ""
        assert "You are helpful" in covenant
        assert "I specialize in PDFs" not in covenant
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Pad edit (with optional files=)
# ---------------------------------------------------------------------------


def test_pad_edit_content_only(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"object": "pad", "action": "edit", "content": "my notes"})
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "my notes" in md
    agent.stop(timeout=1.0)


def test_pad_edit_with_files(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    (agent.working_dir / "export1.txt").write_text("knowledge from export 1")
    (agent.working_dir / "export2.txt").write_text("knowledge from export 2")

    result = _call(agent, {
        "object": "pad", "action": "edit",
        "content": "My working notes.",
        "files": ["export1.txt", "export2.txt"],
    })
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "My working notes." in md
    assert "[file-1]" in md
    assert "knowledge from export 1" in md
    assert "[file-2]" in md
    assert "knowledge from export 2" in md
    agent.stop(timeout=1.0)


def test_pad_edit_files_only(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    (agent.working_dir / "data.txt").write_text("file data")

    result = _call(agent, {
        "object": "pad", "action": "edit",
        "files": ["data.txt"],
    })
    assert result["status"] == "ok"
    md = (agent.working_dir / "system" / "pad.md").read_text()
    assert "[file-1]" in md
    assert "file data" in md
    agent.stop(timeout=1.0)


def test_pad_edit_missing_file_errors(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {
        "object": "pad", "action": "edit",
        "content": "notes",
        "files": ["nonexistent.txt"],
    })
    assert "error" in result
    assert "nonexistent.txt" in result["error"]
    agent.stop(timeout=1.0)


def test_pad_edit_empty_errors(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"object": "pad", "action": "edit"})
    assert "error" in result
    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Pad load
# ---------------------------------------------------------------------------


def test_pad_load(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    agent.start()
    try:
        system_dir = agent._working_dir / "system"
        system_dir.mkdir(exist_ok=True)
        (system_dir / "pad.md").write_text("loaded from disk")

        result = _call(agent, {"object": "pad", "action": "load"})
        assert result["status"] == "ok"
        section = agent._prompt_manager.read_section("pad")
        assert "loaded from disk" in section
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Molt (agent-initiated)
# ---------------------------------------------------------------------------


def test_molt_returns_faint_memory(tmp_path):
    """psyche(context, molt, summary) replays the molt's own ToolCallBlock as
    the opening assistant entry of the fresh session, and returns a faint-
    memory result dict."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi there.")])

        journal_path = _write_session_journal(agent)
        molt_wire_id = "toolu_test_molt_001"
        molt_summary = "Key findings: X=42. Current task: analyze dataset Z."
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

        result = _call(agent, {
            "object": "context",
            "action": "molt",
            "summary": molt_summary,
            "_tc_id": molt_wire_id,
            "session_journal_path": journal_path,
        })

        assert result["status"] == "ok"
        iface = agent._session._chat.interface
        assistant_entries = [e for e in iface.entries if e.role == "assistant"]
        assert assistant_entries, "fresh session should contain the replayed molt tool_call"
        last = assistant_entries[-1]
        molt_calls = [b for b in last.content if isinstance(b, ToolCallBlock)]
        assert molt_calls, "last assistant entry should carry the molt ToolCallBlock"
        assert molt_calls[0].id == molt_wire_id
        assert molt_calls[0].args.get("summary") == molt_summary
    finally:
        agent.stop()


def test_context_forget_still_works(tmp_path):
    """System-initiated molt (base_agent calls this when the warning ladder
    is exhausted) uses the localized default summary and succeeds without
    any agent-provided summary."""
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

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi there.")])

        result = context_forget(agent)
        assert result.get("status") == "ok"
    finally:
        agent.stop()


# ---------------------------------------------------------------------------
# Schema checks
# ---------------------------------------------------------------------------


def test_psyche_schema_has_correct_objects():
    from lingtai_kernel.intrinsics.psyche import get_schema
    SCHEMA = get_schema("en")
    objects = SCHEMA["properties"]["object"]["enum"]
    assert set(objects) == {"lingtai", "pad", "context", "name"}


def test_psyche_schema_has_correct_actions():
    # Schema is intentionally flat (no allOf) for strict-mode provider
    # compatibility — see #114. Per-(object, action) constraints live in
    # the runtime _VALID_ACTIONS table.
    from lingtai_kernel.intrinsics.psyche import _VALID_ACTIONS, get_schema
    SCHEMA = get_schema("en")
    assert "enum" not in SCHEMA["properties"]["action"]
    assert "allOf" not in SCHEMA
    assert _VALID_ACTIONS == {
        "lingtai": {"update", "load"},
        "pad": {"edit", "load", "append"},
        "context": {"molt"},
        "name": {"set", "nickname"},
    }


def test_psyche_schema_has_files_field():
    from lingtai_kernel.intrinsics.psyche import get_schema
    SCHEMA = get_schema("en")
    assert "files" in SCHEMA["properties"]


def test_psyche_schema_has_session_journal_path_field():
    """Issue #350: molt requires a structured session_journal_path arg."""
    from lingtai_kernel.intrinsics.psyche import get_schema
    SCHEMA = get_schema("en")
    prop = SCHEMA["properties"].get("session_journal_path")
    assert prop is not None
    assert prop["type"] == "string"
    assert prop["description"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_invalid_object(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"object": "bogus", "action": "diff"})
    assert "error" in result
    agent.stop(timeout=1.0)


def test_invalid_action_for_object(tmp_path):
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    result = _call(agent, {"object": "lingtai", "action": "submit"})
    assert "error" in result
    assert "update" in result["error"]
    agent.stop(timeout=1.0)


def test_stop_does_not_overwrite_pad_md(tmp_path):
    """Pad is disk-authoritative — stop() must not clobber existing pad.md."""
    agent = Agent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test",
    )
    pad_file = agent.working_dir / "system" / "pad.md"
    pad_file.parent.mkdir(exist_ok=True)
    pad_file.write_text("previous session pad")
    agent.stop()
    assert pad_file.read_text() == "previous session pad"


# ---------------------------------------------------------------------------
# Molt summary persistence (system/summaries/)
# ---------------------------------------------------------------------------


def test_molt_writes_summary_file_for_agent_path(tmp_path):
    """Agent-initiated molt persists summary to system/summaries/ with source=agent."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        journal_path = _write_session_journal(agent)
        molt_id = "toolu_test_summary_001"
        molt_summary = "Worked on dataset Z analysis. Found anomaly in column foo."
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_id,
                name="psyche",
                args={
                    "object": "context", "action": "molt", "summary": molt_summary,
                    "session_journal_path": journal_path,
                },
            ),
        ])

        result = _call(agent, {
            "object": "context",
            "action": "molt",
            "summary": molt_summary,
            "_tc_id": molt_id,
            "session_journal_path": journal_path,
        })

        assert result["status"] == "ok"
        assert result.get("summary_path") is not None

        summary_file = agent._working_dir / result["summary_path"]
        assert summary_file.is_file()
        content = summary_file.read_text()
        # Frontmatter present
        assert content.startswith("---\n")
        assert "molt_count: 1" in content
        assert "source: agent" in content
        assert "tokens_shed:" in content
        # Summary body present after frontmatter
        assert molt_summary in content
    finally:
        agent.stop()


def test_context_forget_writes_summary_file_for_system_path(tmp_path):
    """System-initiated molt also persists summary; source field reflects trigger."""
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

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        result = context_forget(agent, source="warning_ladder")
        assert result.get("status") == "ok"
        assert result.get("summary_path") is not None

        summary_file = agent._working_dir / result["summary_path"]
        assert summary_file.is_file()
        content = summary_file.read_text()
        assert "source: warning_ladder" in content
        assert "molt_count: 1" in content
    finally:
        agent.stop()


def test_summary_write_failure_does_not_block_molt(tmp_path, monkeypatch):
    """If summary write fails, molt still completes; summary_path is None."""
    from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock
    from lingtai_kernel.intrinsics import psyche as psyche_mod

    monkeypatch.setattr(psyche_mod, "_write_molt_summary", lambda *a, **kw: None)

    svc = make_mock_service()

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session

    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")
    agent.start()
    try:
        agent._session.ensure_session()
        agent._session._chat.interface.add_user_message("Hello")
        agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])

        journal_path = _write_session_journal(agent)
        molt_id = "toolu_test_failguard_001"
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=molt_id, name="psyche",
                args={
                    "object": "context", "action": "molt", "summary": "test",
                    "session_journal_path": journal_path,
                },
            ),
        ])

        result = _call(agent, {
            "object": "context",
            "action": "molt",
            "summary": "test",
            "_tc_id": molt_id,
            "session_journal_path": journal_path,
        })

        # Molt succeeded
        assert result["status"] == "ok"
        # But summary_path is None (write was forced to fail)
        assert result.get("summary_path") is None
    finally:
        agent.stop()
