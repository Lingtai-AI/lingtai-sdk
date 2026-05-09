"""Tests for pre-molt task persistence (issue #55).

Covers:
    - _extract_task_snapshot: extracts recent conversation text
    - _persist_task_snapshot: writes snapshot to pad.md
    - context_forget integration: auto-saves before forced molt
    - _context_molt pad-empty warning: warns when pad.md is empty
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_service():
    """Create a mocked LLMService whose create_session returns a ChatInterface."""
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"

    def fake_create_session(**kwargs):
        mock_chat = MagicMock()
        iface = ChatInterface()
        iface.add_system("You are helpful.")
        mock_chat.interface = iface
        mock_chat.context_window.return_value = 100_000
        return mock_chat

    svc.create_session.side_effect = fake_create_session
    return svc


def _make_agent(tmp_path):
    """Create an Agent with psyche capability and a working mock session."""
    from lingtai.agent import Agent

    svc = _make_mock_service()
    agent = Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )
    return agent


def _ensure_session(agent):
    """Ensure the agent has a live chat session."""
    agent._session.ensure_session()


def _populate_conversation(agent, messages: list[tuple[str, str]]):
    """Add user/assistant text entries to the live interface.

    messages is a list of (role, text) tuples.
    """
    iface = agent._chat.interface
    for role, text in messages:
        if role == "user":
            iface.add_user_message(text)
        elif role == "assistant":
            iface.add_assistant_message([TextBlock(text=text)])


# ---------------------------------------------------------------------------
# _extract_task_snapshot tests
# ---------------------------------------------------------------------------


class TestExtractTaskSnapshot:
    """Unit tests for _extract_task_snapshot."""

    def test_empty_interface(self, tmp_path):
        """Returns empty string when there is no conversation (only system)."""
        from lingtai_kernel.intrinsics.psyche._molt import _extract_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            result = _extract_task_snapshot(agent)
            assert result == ""
        finally:
            agent.stop()

    def test_no_chat_session(self, tmp_path):
        """Returns empty string when _chat is None."""
        from lingtai_kernel.intrinsics.psyche._molt import _extract_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            # _chat is None before ensure_session
            result = _extract_task_snapshot(agent)
            assert result == ""
        finally:
            agent.stop()

    def test_extracts_recent_messages(self, tmp_path):
        """Extracts user and assistant text in chronological order."""
        from lingtai_kernel.intrinsics.psyche._molt import _extract_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Please create a PR for the auth fix"),
                ("assistant", "I'll create the PR now"),
                ("user", "Make sure to include the test changes"),
                ("assistant", "Done, PR #42 is ready"),
            ])
            result = _extract_task_snapshot(agent)
            assert "[human] Please create a PR" in result
            assert "[agent] I'll create the PR" in result
            assert "[human] Make sure to include" in result
            assert "[agent] Done, PR #42" in result
            # Chronological order
            human_pos = result.index("[human] Please create")
            agent_pos = result.index("[agent] Done, PR")
            assert human_pos < agent_pos
        finally:
            agent.stop()

    def test_respects_max_chars(self, tmp_path):
        """Truncates to max_chars limit."""
        from lingtai_kernel.intrinsics.psyche._molt import _extract_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "A" * 500),
                ("assistant", "B" * 500),
                ("user", "C" * 500),
                ("assistant", "D" * 500),
            ])
            result = _extract_task_snapshot(agent, max_chars=200)
            # Result should be bounded — some lines may be truncated
            assert len(result) <= 300  # generous overhead for prefixes + truncation
            # The most recent messages should be present (collected from tail)
            assert "[agent]" in result or "[human]" in result
        finally:
            agent.stop()

    def test_skips_system_entries(self, tmp_path):
        """System entries are not included in the snapshot."""
        from lingtai_kernel.intrinsics.psyche._molt import _extract_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Do the thing"),
                ("assistant", "Doing it"),
            ])
            result = _extract_task_snapshot(agent)
            assert "[human] Do the thing" in result
            assert "[agent] Doing it" in result
            # No system prompt text leaked through
            assert "You are helpful" not in result
        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# _persist_task_snapshot tests
# ---------------------------------------------------------------------------


class TestPersistTaskSnapshot:
    """Unit tests for _persist_task_snapshot."""

    def test_creates_pad_with_snapshot(self, tmp_path):
        """Writes task snapshot to pad.md when pad doesn't exist."""
        from lingtai_kernel.intrinsics.psyche._molt import _persist_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Create a PR for the auth fix"),
                ("assistant", "Working on it"),
            ])
            result = _persist_task_snapshot(agent, source="admin")
            assert result is True

            pad_path = agent._working_dir / "system" / "pad.md"
            assert pad_path.is_file()
            content = pad_path.read_text()
            assert "[auto-saved task context" in content
            assert "[human] Create a PR" in content
            assert "[agent] Working on it" in content
        finally:
            agent.stop()

    def test_prepends_to_existing_pad(self, tmp_path):
        """Prepends snapshot before existing pad content."""
        from lingtai_kernel.intrinsics.psyche._molt import _persist_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            # Write existing pad content
            system_dir = agent._working_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            pad_path = system_dir / "pad.md"
            pad_path.write_text("## Existing notes\n\nSome important context here.")

            _populate_conversation(agent, [
                ("user", "Now fix the login bug"),
            ])
            result = _persist_task_snapshot(agent, source="warning_ladder")
            assert result is True

            content = pad_path.read_text()
            # Auto-saved section comes first
            auto_pos = content.index("[auto-saved task context")
            existing_pos = content.index("Existing notes")
            assert auto_pos < existing_pos
            # Separator between auto-saved and existing
            assert "---" in content
        finally:
            agent.stop()

    def test_returns_false_when_no_conversation(self, tmp_path):
        """Returns False when there's nothing to extract."""
        from lingtai_kernel.intrinsics.psyche._molt import _persist_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            result = _persist_task_snapshot(agent, source="admin")
            assert result is False
        finally:
            agent.stop()

    def test_source_appears_in_header(self, tmp_path):
        """The source of the forced molt appears in the snapshot header."""
        from lingtai_kernel.intrinsics.psyche._molt import _persist_task_snapshot

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Hello"),
            ])
            _persist_task_snapshot(agent, source="jason")

            pad_path = agent._working_dir / "system" / "pad.md"
            content = pad_path.read_text()
            assert "jason" in content
        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# context_forget integration tests
# ---------------------------------------------------------------------------


class TestContextForgetTaskPersistence:
    """Integration: context_forget auto-saves task snapshot to pad.md."""

    def test_context_forget_saves_snapshot_to_pad(self, tmp_path):
        """context_forget writes task context to pad.md before wiping."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Create PRs for all the auth changes"),
                ("assistant", "I'll start with the login module"),
            ])
            result = context_forget(agent, source="admin")

            assert result["status"] == "ok"
            assert result["task_snapshot_saved"] is True

            # pad.md should contain the auto-saved context
            pad_path = agent._working_dir / "system" / "pad.md"
            assert pad_path.is_file()
            content = pad_path.read_text()
            assert "[auto-saved task context" in content
            assert "Create PRs" in content
        finally:
            agent.stop()

    def test_context_forget_preserves_existing_pad(self, tmp_path):
        """context_forget prepends snapshot; existing pad content survives."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            # Write existing pad
            system_dir = agent._working_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            (system_dir / "pad.md").write_text("## My important notes\n\nDon't forget X.")

            _populate_conversation(agent, [
                ("user", "Fix the deploy script"),
            ])
            context_forget(agent, source="warning_ladder")

            content = (system_dir / "pad.md").read_text()
            assert "My important notes" in content
            assert "Don't forget X" in content
            assert "[auto-saved task context" in content
        finally:
            agent.stop()

    def test_context_forget_with_empty_conversation(self, tmp_path):
        """context_forget handles empty conversation gracefully."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            result = context_forget(agent, source="aed", attempts=3)
            assert result["status"] == "ok"
            assert result["task_snapshot_saved"] is False
        finally:
            agent.stop()

    def test_context_forget_result_mentions_snapshot(self, tmp_path):
        """The result dict includes task_snapshot_saved flag."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Do something"),
            ])
            result = context_forget(agent, source="admin")
            assert "task_snapshot_saved" in result
            assert result["task_snapshot_saved"] is True
        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# _context_molt pad-empty warning tests
# ---------------------------------------------------------------------------


class TestContextMoltPadWarning:
    """Deliberate molt warns when pad.md is empty."""

    def test_molt_with_empty_pad_returns_warning(self, tmp_path):
        """Agent-initiated molt includes warning when pad.md is empty."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            # Ensure pad.md doesn't exist or is empty
            pad_path = agent._working_dir / "system" / "pad.md"
            if pad_path.exists():
                pad_path.write_text("")

            # Add a molt call to the interface so _tc_id can be found
            tc_id = "toolu_test_123"
            iface = agent._chat.interface
            molt_block = ToolCallBlock(
                id=tc_id,
                name="psyche",
                args={
                    "object": "context",
                    "action": "molt",
                    "summary": "Test summary for molt",
                },
            )
            iface.add_assistant_message(content=[molt_block])

            result = _context_molt(agent, {
                "summary": "Test summary for molt",
                "_tc_id": tc_id,
            })
            assert result["status"] == "ok"
            assert "warning" in result
            assert "pad.md" in result["warning"]
        finally:
            agent.stop()

    def test_molt_with_nonempty_pad_no_warning(self, tmp_path):
        """Agent-initiated molt has no warning when pad.md has content."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            # Write content to pad.md
            system_dir = agent._working_dir / "system"
            system_dir.mkdir(parents=True, exist_ok=True)
            (system_dir / "pad.md").write_text("## Current task\n\nWorking on auth.")

            tc_id = "toolu_test_456"
            iface = agent._chat.interface
            molt_block = ToolCallBlock(
                id=tc_id,
                name="psyche",
                args={
                    "object": "context",
                    "action": "molt",
                    "summary": "Test summary with pad content",
                },
            )
            iface.add_assistant_message(content=[molt_block])

            result = _context_molt(agent, {
                "summary": "Test summary with pad content",
                "_tc_id": tc_id,
            })
            assert result["status"] == "ok"
            assert "warning" not in result
        finally:
            agent.stop()
