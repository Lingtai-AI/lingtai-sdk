"""Tests for keep_last parameter on molt (issue #55 revision).

Covers:
    - _context_molt with keep_last=None (default, archive all)
    - _context_molt with keep_last=N (preserve last N entries)
    - _context_molt with keep_last > total entries (preserve all)
    - _context_molt with keep_last=0 (same as None)
    - context_forget with keep_last (system-initiated)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lingtai_kernel.llm.interface import ChatInterface, TextBlock, ToolCallBlock


from tests._molt_helpers import write_session_journal as _write_session_journal


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


def _add_molt_call(agent, summary="Test summary"):
    """Add a molt ToolCallBlock to the interface, return its id."""
    tc_id = "toolu_test_molt"
    iface = agent._chat.interface
    molt_block = ToolCallBlock(
        id=tc_id,
        name="psyche",
        args={
            "object": "context",
            "action": "molt",
            "summary": summary,
        },
    )
    iface.add_assistant_message(content=[molt_block])
    return tc_id


def _count_non_system_entries(iface):
    """Count non-system entries in the interface."""
    return sum(1 for e in iface.entries if e.role != "system")


# ---------------------------------------------------------------------------
# _context_molt keep_last tests
# ---------------------------------------------------------------------------


class TestContextMoltKeepLast:
    """Test keep_last parameter on agent-initiated molt."""

    def test_keep_last_default_preserves_20(self, tmp_path):
        """Default (no keep_last): keeps last 20 entries (default)."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Message 1"),
                ("assistant", "Reply 1"),
                ("user", "Message 2"),
                ("assistant", "Reply 2"),
            ])
            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            # Default keeps last 20, but only 4 non-system entries exist
            # (excluding the molt call), so all 4 are kept.
            assert result["kept_last"] == 4

            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]
            # 4 kept + 1 molt call = 5
            assert len(non_system) == 5
        finally:
            agent.stop()

    def test_keep_last_zero_archives_all(self, tmp_path):
        """keep_last=0 explicitly disables keeping, archives all."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Message 1"),
                ("assistant", "Reply 1"),
                ("user", "Message 2"),
                ("assistant", "Reply 2"),
            ])
            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": 0,
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            assert result["kept_last"] == 0

            # Fresh interface should only have system + molt call (no old entries)
            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]
            # Only the replayed molt call
            assert len(non_system) == 1
            assert any(
                isinstance(b, ToolCallBlock) and b.name == "psyche"
                for b in non_system[0].content
            )
        finally:
            agent.stop()

    def test_keep_last_preserves_entries(self, tmp_path):
        """keep_last=2 preserves the last 2 conversation entries."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Old message"),
                ("assistant", "Old reply"),
                ("user", "Recent message"),
                ("assistant", "Recent reply"),
            ])
            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": 2,
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            assert result["kept_last"] == 2

            # Fresh interface: system + 2 kept entries + molt call
            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]
            # 2 kept + 1 molt call
            assert len(non_system) == 3

            # The kept entries should contain the recent messages
            kept_texts = []
            for entry in non_system[:-1]:  # exclude the molt call
                for block in entry.content:
                    if isinstance(block, TextBlock):
                        kept_texts.append(block.text)

            assert "Recent message" in kept_texts
            assert "Recent reply" in kept_texts
            # Old messages should NOT be in the kept entries
            assert "Old message" not in kept_texts
            assert "Old reply" not in kept_texts
        finally:
            agent.stop()

    def test_keep_last_larger_than_total(self, tmp_path):
        """keep_last > total entries: preserves all non-system entries (excluding molt call)."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Only message"),
                ("assistant", "Only reply"),
            ])
            tc_id = _add_molt_call(agent)

            # 3 non-system entries (user, assistant, molt-call), but molt-call
            # is excluded from keep_last (replayed separately), so keep all 2
            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": 100,
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            # Should keep all non-system entries except the molt call itself
            assert result["kept_last"] == 2

            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]
            # 2 kept + 1 molt call replayed = 3
            assert len(non_system) == 3

            # Verify the original messages survived
            all_texts = []
            for entry in non_system:
                for block in entry.content:
                    if isinstance(block, TextBlock):
                        all_texts.append(block.text)
            assert "Only message" in all_texts
            assert "Only reply" in all_texts
        finally:
            agent.stop()

    def test_keep_last_deduplicates_with_keep_tool_calls(self, tmp_path):
        """Entries already in keep_tool_calls are removed from keep_last."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt
        from lingtai_kernel.llm.interface import ToolResultBlock

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            iface = agent._chat.interface

            # Add a user message
            iface.add_user_message("Do something")

            # Add a tool call + result pair with a LingTai id
            tool_tc_id = "toolu_tool_dedup"
            lt_id = "tc_dedup_abc"
            iface.add_assistant_message(content=[
                ToolCallBlock(id=tool_tc_id, name="file_read", args={"path": "x.py"})
            ])
            iface.add_tool_results([
                ToolResultBlock(
                    id=tool_tc_id, name="file_read",
                    content={"text": "file contents", "_tool_call_id": lt_id}
                )
            ])

            # Add more conversation
            iface.add_assistant_message([TextBlock(text="Here's what I found")])
            iface.add_user_message("Thanks")

            tc_id = _add_molt_call(agent)

            # keep_last=100 to keep everything, keep_tool_calls names the
            # same tool pair — the overlapping entries should be deduplicated.
            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": 100,
                "keep_tool_calls": [lt_id],
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            assert result["kept_tool_calls"] == 1

            # Without dedup, keep_last would include the tool_call and
            # tool_result entries AND keep_pairs would replay them again.
            # With dedup, the two overlapping entries are removed from
            # keep_last_entries. Non-system entries before molt:
            #   user("Do something"), assistant(tool_call), user(tool_result),
            #   assistant("Here's what I found"), user("Thanks")
            # = 5 entries. The tool_call entry and tool_result entry overlap
            # with keep_pairs, so keep_last_entries = 5 - 2 = 3.
            assert result["kept_last"] == 3

            # Verify the tool pair appears exactly once in the fresh interface.
            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]
            tool_call_count = sum(
                1 for e in non_system for b in e.content
                if isinstance(b, ToolCallBlock) and b.id == tool_tc_id
            )
            assert tool_call_count == 1, "Tool call should appear exactly once (no duplicates)"
        finally:
            agent.stop()

    def test_keep_last_invalid_type_rejected(self, tmp_path):
        """Non-integer keep_last is rejected."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [("user", "Hi")])
            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": "twenty",
                "session_journal_path": journal_path,
            })

            assert "error" in result
            assert "integer" in result["error"]
        finally:
            agent.stop()

    def test_keep_last_negative_rejected(self, tmp_path):
        """Negative keep_last is rejected."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [("user", "Hi")])
            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": -5,
                "session_journal_path": journal_path,
            })

            assert "error" in result
            assert "non-negative" in result["error"]
        finally:
            agent.stop()

    def test_keep_last_with_keep_tool_calls(self, tmp_path):
        """keep_last and keep_tool_calls can be used together."""
        from lingtai_kernel.intrinsics.psyche._molt import _context_molt
        from lingtai_kernel.llm.interface import ToolResultBlock

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            iface = agent._chat.interface

            # Add a user message
            iface.add_user_message("Do something")

            # Add a tool call + result pair with a LingTai id
            tool_tc_id = "toolu_tool_123"
            lt_id = "tc_12345_abc"
            iface.add_assistant_message(content=[
                ToolCallBlock(id=tool_tc_id, name="file_read", args={"path": "x.py"})
            ])
            iface.add_tool_results([
                ToolResultBlock(
                    id=tool_tc_id, name="file_read",
                    content={"text": "file contents", "_tool_call_id": lt_id}
                )
            ])

            # Add more conversation
            iface.add_assistant_message([TextBlock(text="Here's what I found")])
            iface.add_user_message("Now do another thing")
            iface.add_assistant_message([TextBlock(text="Working on it")])

            tc_id = _add_molt_call(agent)

            journal_path = _write_session_journal(agent)
            result = _context_molt(agent, {
                "summary": "Test summary",
                "_tc_id": tc_id,
                "keep_last": 2,  # keep last 2 entries
                "keep_tool_calls": [lt_id],  # also keep the tool pair
                "session_journal_path": journal_path,
            })

            assert result["status"] == "ok"
            assert result["kept_last"] == 2
            assert result["kept_tool_calls"] == 1
        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# context_forget keep_last tests
# ---------------------------------------------------------------------------


class TestContextForgetKeepLast:
    """Test keep_last parameter on system-initiated forced molt."""

    def test_context_forget_default_no_keep(self, tmp_path):
        """Default context_forget archives everything (no keep_last)."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Message 1"),
                ("assistant", "Reply 1"),
            ])
            result = context_forget(agent, source="admin")

            assert result["status"] == "ok"
            assert result["kept_last"] == 0
        finally:
            agent.stop()

    def test_context_forget_with_keep_last(self, tmp_path):
        """context_forget with keep_last preserves recent entries."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Old question"),
                ("assistant", "Old answer"),
                ("user", "Recent question"),
                ("assistant", "Recent answer"),
            ])

            result = context_forget(agent, source="admin", keep_last=2)

            assert result["status"] == "ok"
            assert result["kept_last"] == 2

            # Check the fresh interface has the kept entries
            iface = agent._chat.interface
            non_system = [e for e in iface.entries if e.role != "system"]

            # Find text content in the kept entries
            all_texts = []
            for entry in non_system:
                for block in entry.content:
                    if isinstance(block, TextBlock):
                        all_texts.append(block.text)

            assert "Recent question" in all_texts
            assert "Recent answer" in all_texts
            assert "Old question" not in all_texts
            assert "Old answer" not in all_texts
        finally:
            agent.stop()

    def test_context_forget_no_task_snapshot_saved_field(self, tmp_path):
        """context_forget no longer includes task_snapshot_saved in result."""
        from lingtai_kernel.intrinsics.psyche._molt import context_forget

        agent = _make_agent(tmp_path)
        agent.start()
        try:
            _ensure_session(agent)
            _populate_conversation(agent, [
                ("user", "Something"),
            ])
            result = context_forget(agent, source="admin")
            assert "task_snapshot_saved" not in result
        finally:
            agent.stop()
