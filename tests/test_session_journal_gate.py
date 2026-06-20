"""Tests for the molt session-journal gate (issue #350).

The gate makes ``psyche(object="context", action="molt", ...)`` require a
machine-checkable pointer to the session journal the agent wrote before
shedding context. Validation runs BEFORE any state mutation — a rejected
journal must leave molt_count and history untouched.

Two layers are tested:
  1. The pure validator ``validate_session_journal_path`` (unit).
  2. The end-to-end gate via ``_context_molt`` (integration), including the
     fail-closed guarantee (no partial shed) and metadata recording.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai_kernel.intrinsics.psyche._session_journal import (
    validate_session_journal_path,
)
from tests._service_helpers import make_gemini_mock_service as make_mock_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------




VALID_JOURNAL = """\
---
name: 2026-06-19-molt-3-session-journal-gate
description: Implemented the molt session-journal gate for issue #350.
date: 2026-06-19
molt_count: 3
type: session-journal
---

## What this segment was about
Implementing the molt gate.

## Accomplishments
Wrote the validator and wired it into _context_molt.
"""


def _write_journal(workdir, rel, content=VALID_JOURNAL):
    path = workdir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _agent_workdir(agent):
    return agent._working_dir


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


def test_validate_missing_path_rejects(tmp_path):
    ok, err, resolved = validate_session_journal_path(tmp_path, None)
    assert ok is False
    assert resolved is None
    assert "session_journal_path" in err


def test_validate_empty_path_rejects(tmp_path):
    ok, err, resolved = validate_session_journal_path(tmp_path, "   ")
    assert ok is False
    assert "session_journal_path" in err


def test_validate_nonexistent_path_rejects(tmp_path):
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
    )
    assert ok is False
    assert "does not exist" in err.lower() or "not found" in err.lower()


def test_validate_outside_workdir_rejects(tmp_path):
    # Absolute path pointing entirely outside the workdir.
    outside = tmp_path.parent / "outside.md"
    outside.write_text(VALID_JOURNAL, encoding="utf-8")
    ok, err, resolved = validate_session_journal_path(tmp_path, str(outside))
    assert ok is False
    assert "workdir" in err.lower() or "inside" in err.lower()


def test_validate_path_traversal_rejects(tmp_path):
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/../../../etc/passwd"
    )
    assert ok is False


def test_validate_wrong_knowledge_area_rejects(tmp_path):
    # A real, well-formed knowledge file but NOT under session-journal.
    _write_journal(tmp_path, "knowledge/some-topic/KNOWLEDGE.md")
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/some-topic/KNOWLEDGE.md"
    )
    assert ok is False
    assert "session-journal" in err


def test_validate_parent_index_rejects(tmp_path):
    # The parent index is routing-only, not a journal entry.
    _write_journal(tmp_path, "knowledge/session-journal/KNOWLEDGE.md")
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/KNOWLEDGE.md"
    )
    assert ok is False
    assert "index" in err.lower() or "entry" in err.lower()


def test_validate_scratch_file_rejects(tmp_path):
    _write_journal(tmp_path, "tmp/scratch.md")
    ok, err, resolved = validate_session_journal_path(tmp_path, "tmp/scratch.md")
    assert ok is False


def test_validate_empty_file_rejects(tmp_path):
    _write_journal(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md", content=""
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
    )
    assert ok is False
    assert "empty" in err.lower()


def test_validate_no_frontmatter_rejects(tmp_path):
    _write_journal(
        tmp_path,
        "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md",
        content="Just some prose, no frontmatter at all.\n",
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
    )
    assert ok is False
    assert "frontmatter" in err.lower()


def test_validate_invalid_yaml_rejects(tmp_path):
    bad = "---\nname: [unterminated\n: : :\n---\n\nbody\n"
    _write_journal(
        tmp_path,
        "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md",
        content=bad,
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
    )
    assert ok is False
    assert "yaml" in err.lower() or "frontmatter" in err.lower()


def test_validate_no_marker_rejects(tmp_path):
    # Valid frontmatter with name+description but NO session-journal marker.
    no_marker = (
        "---\n"
        "name: 2026-06-19-molt-1-x\n"
        "description: A generic knowledge file without the marker.\n"
        "---\n\n"
        "Body content here.\n"
    )
    _write_journal(
        tmp_path,
        "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md",
        content=no_marker,
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
    )
    assert ok is False
    assert "marker" in err.lower() or "session-journal" in err.lower() or "session_journal" in err.lower()


def test_validate_accepts_type_marker(tmp_path):
    _write_journal(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"
    )
    assert ok is True, err
    assert err is None
    assert resolved == "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"


def test_validate_accepts_session_journal_bool_marker(tmp_path):
    content = (
        "---\n"
        "name: 2026-06-19-molt-4-y\n"
        "description: Uses the boolean marker.\n"
        "session_journal: true\n"
        "---\n\n"
        "Body.\n"
    )
    _write_journal(
        tmp_path,
        "knowledge/session-journal/2026-06-19-molt-4-y/KNOWLEDGE.md",
        content=content,
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-4-y/KNOWLEDGE.md"
    )
    assert ok is True, err


def test_validate_normalizes_absolute_inside_workdir(tmp_path):
    p = _write_journal(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"
    )
    ok, err, resolved = validate_session_journal_path(tmp_path, str(p))
    assert ok is True, err
    # Recorded path is normalized to be relative to the workdir.
    assert resolved == "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"


def test_validate_rejects_non_knowledge_md_filename(tmp_path):
    # Right directory tree but file is not KNOWLEDGE.md.
    _write_journal(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-3-x/notes.md"
    )
    ok, err, resolved = validate_session_journal_path(
        tmp_path, "knowledge/session-journal/2026-06-19-molt-3-x/notes.md"
    )
    assert ok is False
    assert "KNOWLEDGE.md" in err


# ---------------------------------------------------------------------------
# Integration: molt gate via _context_molt
# ---------------------------------------------------------------------------


def _molt_setup(tmp_path):
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
    agent._session.ensure_session()
    agent._session._chat.interface.add_user_message("Hello")
    agent._session._chat.interface.add_assistant_message([TextBlock(text="Hi.")])
    return agent, ToolCallBlock


def _emit_molt_call(agent, ToolCallBlock, wire_id, summary, journal_path=None):
    args = {"object": "context", "action": "molt", "summary": summary}
    if journal_path is not None:
        args["session_journal_path"] = journal_path
    agent._session._chat.interface.add_assistant_message([
        ToolCallBlock(id=wire_id, name="psyche", args=args),
    ])


def _call(agent, args):
    return agent._intrinsics["psyche"](args)


def test_molt_rejects_missing_journal_path(tmp_path):
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        wire = "toolu_gate_001"
        _emit_molt_call(agent, ToolCallBlock, wire, "summary text")
        before_count = agent._molt_count
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "summary text", "_tc_id": wire,
        })
        assert "error" in result
        assert "session_journal_path" in result["error"]
        # Fail closed: nothing shed.
        assert agent._molt_count == before_count
        assert agent._session._chat is not None
    finally:
        agent.stop()


def test_molt_rejects_nonexistent_journal(tmp_path):
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        wire = "toolu_gate_002"
        bad = "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
        _emit_molt_call(agent, ToolCallBlock, wire, "summary text", bad)
        before_count = agent._molt_count
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "summary text", "_tc_id": wire,
            "session_journal_path": bad,
        })
        assert "error" in result
        assert agent._molt_count == before_count
    finally:
        agent.stop()


def test_molt_rejects_outside_area(tmp_path):
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        # well-formed knowledge file, wrong area
        wd = _agent_workdir(agent)
        _write_journal(wd, "knowledge/other/KNOWLEDGE.md")
        wire = "toolu_gate_003"
        path = "knowledge/other/KNOWLEDGE.md"
        _emit_molt_call(agent, ToolCallBlock, wire, "summary text", path)
        before_count = agent._molt_count
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "summary text", "_tc_id": wire,
            "session_journal_path": path,
        })
        assert "error" in result
        assert agent._molt_count == before_count
    finally:
        agent.stop()


def test_molt_rejects_invalid_frontmatter(tmp_path):
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        wd = _agent_workdir(agent)
        _write_journal(
            wd,
            "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md",
            content="no frontmatter here\n",
        )
        wire = "toolu_gate_004"
        path = "knowledge/session-journal/2026-06-19-molt-1-x/KNOWLEDGE.md"
        _emit_molt_call(agent, ToolCallBlock, wire, "summary text", path)
        before_count = agent._molt_count
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "summary text", "_tc_id": wire,
            "session_journal_path": path,
        })
        assert "error" in result
        assert agent._molt_count == before_count
    finally:
        agent.stop()


def test_molt_accepts_valid_journal_and_records_path(tmp_path):
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        wd = _agent_workdir(agent)
        path = "knowledge/session-journal/2026-06-19-molt-3-x/KNOWLEDGE.md"
        _write_journal(wd, path)
        wire = "toolu_gate_005"
        _emit_molt_call(agent, ToolCallBlock, wire, "summary text", path)
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "summary text", "_tc_id": wire,
            "session_journal_path": path,
        })
        assert result.get("status") == "ok", result
        # Path recorded in molt result metadata.
        assert result.get("session_journal_path") == path
        # And recorded in the persisted summary frontmatter.
        summary_file = wd / result["summary_path"]
        content = summary_file.read_text()
        assert "session_journal_path:" in content
        assert path in content
    finally:
        agent.stop()


def test_molt_initiator_system_arg_cannot_bypass_gate(tmp_path):
    """A model-provided ``_initiator: "system"`` arg must NOT bypass the
    session-journal gate (issue #350). ``_initiator`` is a tool arg that can
    reach intrinsic dispatch from model-controlled args, not trusted kernel
    state — so a molt carrying it but no valid journal must still be refused
    on the missing journal, and must not shed any context."""
    agent, ToolCallBlock = _molt_setup(tmp_path)
    try:
        wire = "toolu_gate_bypass_001"
        # Emit the molt call exactly as a model would, smuggling _initiator.
        agent._session._chat.interface.add_assistant_message([
            ToolCallBlock(
                id=wire,
                name="psyche",
                args={
                    "object": "context",
                    "action": "molt",
                    "summary": "briefing",
                    "_initiator": "system",
                },
            ),
        ])
        before_count = agent._molt_count
        before_chat = agent._session._chat
        result = _call(agent, {
            "object": "context", "action": "molt",
            "summary": "briefing", "_tc_id": wire,
            "_initiator": "system",
        })
        # Rejected FIRST on the missing journal — the gate ran despite
        # _initiator == "system".
        assert "error" in result
        assert "session_journal_path" in result["error"]
        # Fail closed: nothing shed, no molt consumed, chat untouched.
        assert agent._molt_count == before_count
        assert agent._session._chat is before_chat
    finally:
        agent.stop()


def test_system_forget_does_not_require_journal(tmp_path):
    """System-initiated molt (context_forget) must NOT require a journal —
    there is no agent turn to write one."""
    from lingtai_kernel.intrinsics.psyche import context_forget

    agent, _ = _molt_setup(tmp_path)
    try:
        result = context_forget(agent, source="warning_ladder")
        assert result.get("status") == "ok"
    finally:
        agent.stop()
