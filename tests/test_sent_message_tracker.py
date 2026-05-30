"""Tests for SentMessageTracker — dedup and poll backoff."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from lingtai_kernel.sent_message_tracker import (
    SentMessageTracker,
    SEND_TOOLS,
    SEND_ACTIONS,
    CHECK_ACTIONS,
    _content_hash,
)


# ---------------------------------------------------------------------------
# Unit tests for SentMessageTracker
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic(self):
        h1 = _content_hash("hello", "alice")
        h2 = _content_hash("hello", "alice")
        assert h1 == h2

    def test_different_content(self):
        h1 = _content_hash("hello", "alice")
        h2 = _content_hash("goodbye", "alice")
        assert h1 != h2

    def test_different_recipient(self):
        h1 = _content_hash("hello", "alice")
        h2 = _content_hash("hello", "bob")
        assert h1 != h2


class TestWasRecentlySent:
    def test_not_sent(self):
        tracker = SentMessageTracker()
        assert not tracker.was_recently_sent("hello", "alice")

    def test_after_record(self):
        tracker = SentMessageTracker()
        tracker.record_sent("hello", "alice", "telegram")
        assert tracker.was_recently_sent("hello", "alice")

    def test_different_message(self):
        tracker = SentMessageTracker()
        tracker.record_sent("hello", "alice", "telegram")
        assert not tracker.was_recently_sent("goodbye", "alice")

    def test_different_recipient(self):
        tracker = SentMessageTracker()
        tracker.record_sent("hello", "alice", "telegram")
        assert not tracker.was_recently_sent("hello", "bob")

    def test_expired_window(self):
        tracker = SentMessageTracker(dedup_window_seconds=0.1)
        tracker.record_sent("hello", "alice", "telegram")
        time.sleep(0.15)
        assert not tracker.was_recently_sent("hello", "alice")

    def test_custom_window(self):
        tracker = SentMessageTracker(dedup_window_seconds=60)
        tracker.record_sent("hello", "alice", "telegram")
        assert tracker.was_recently_sent("hello", "alice", window_seconds=0.001)
        # With default window (60s), still found
        assert tracker.was_recently_sent("hello", "alice")


class TestRecordSent:
    def test_resets_poll_counter(self):
        tracker = SentMessageTracker()
        tracker.record_poll("telegram", found_new=False)
        tracker.record_poll("telegram", found_new=False)
        assert tracker._poll_counts["telegram"] == 2
        tracker.record_sent("hello", "alice", "telegram")
        assert "telegram" not in tracker._poll_counts

    def test_max_entries_cap(self):
        tracker = SentMessageTracker(max_entries=3)
        for i in range(5):
            tracker.record_sent(f"msg_{i}", "alice", "telegram")
        assert len(tracker._entries) == 3

    def test_ttl_cleanup(self):
        tracker = SentMessageTracker(ttl_seconds=0.1)
        tracker.record_sent("old", "alice", "telegram")
        time.sleep(0.15)
        tracker.record_sent("new", "bob", "telegram")
        # Old entry cleaned up
        assert len(tracker._entries) == 1
        assert tracker._entries[0].recipient == "bob"


class TestPollBackoff:
    def test_no_polls(self):
        tracker = SentMessageTracker()
        assert not tracker.should_stop_polling("telegram")

    def test_polls_with_results(self):
        tracker = SentMessageTracker()
        tracker.record_poll("telegram", found_new=True)
        tracker.record_poll("telegram", found_new=True)
        tracker.record_poll("telegram", found_new=True)
        assert not tracker.should_stop_polling("telegram")

    def test_polls_without_results_exhaust(self):
        tracker = SentMessageTracker()
        tracker.record_poll("telegram", found_new=False)
        tracker.record_poll("telegram", found_new=False)
        assert not tracker.should_stop_polling("telegram")
        tracker.record_poll("telegram", found_new=False)
        assert tracker.should_stop_polling("telegram")

    def test_found_new_resets_counter(self):
        tracker = SentMessageTracker()
        tracker.record_poll("telegram", found_new=False)
        tracker.record_poll("telegram", found_new=False)
        tracker.record_poll("telegram", found_new=True)  # reset
        assert not tracker.should_stop_polling("telegram")
        tracker.record_poll("telegram", found_new=False)
        assert not tracker.should_stop_polling("telegram")

    def test_per_channel_isolation(self):
        tracker = SentMessageTracker()
        for _ in range(3):
            tracker.record_poll("telegram", found_new=False)
        assert tracker.should_stop_polling("telegram")
        assert not tracker.should_stop_polling("imap")

    def test_backoff_seconds(self):
        tracker = SentMessageTracker()
        assert tracker.poll_backoff_seconds("telegram") == 0.0
        tracker.record_poll("telegram", found_new=False)
        assert tracker.poll_backoff_seconds("telegram") == 2.0
        tracker.record_poll("telegram", found_new=False)
        assert tracker.poll_backoff_seconds("telegram") == 4.0
        tracker.record_poll("telegram", found_new=False)
        assert tracker.poll_backoff_seconds("telegram") == 8.0
        # Capped at 8
        tracker.record_poll("telegram", found_new=False)
        assert tracker.poll_backoff_seconds("telegram") == 8.0


class TestResetPoll:
    def test_reset_poll(self):
        tracker = SentMessageTracker()
        tracker.record_poll("telegram", found_new=False)
        tracker.record_poll("telegram", found_new=False)
        tracker.reset_poll("telegram")
        assert not tracker.should_stop_polling("telegram")
        assert tracker.poll_backoff_seconds("telegram") == 0.0


# ---------------------------------------------------------------------------
# Integration tests: _check_external_send and _check_poll_backoff
# ---------------------------------------------------------------------------


class TestCheckExternalSend:
    """Test the _check_external_send function from turn.py."""

    def _make_agent(self):
        agent = MagicMock()
        agent._sent_tracker = SentMessageTracker()
        agent._log = MagicMock()
        return agent

    def _make_tc(self, name, args, id="tc-1"):
        tc = MagicMock()
        tc.id = id
        tc.name = name
        tc.args = args
        return tc

    def test_non_send_tool_ignored(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc = self._make_tc("read", {"path": "/tmp"})
        _check_external_send(agent, [tc])
        assert len(agent._sent_tracker._entries) == 0

    def test_send_action_records(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        _check_external_send(agent, [tc])
        assert len(agent._sent_tracker._entries) == 1

    def test_reply_action_records(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc = self._make_tc("imap", {
            "action": "reply",
            "message": "thanks for your email",
            "to": "user@example.com",
        })
        _check_external_send(agent, [tc])
        assert len(agent._sent_tracker._entries) == 1

    def test_dedup_warns_via_tool_result(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        # First send
        tc1 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        _check_external_send(agent, [tc1])

        # Same message again — dedup should detect it
        tc2 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        tc2.id = "call_dedup"

        # Build a tool result matching tc2
        tr = MagicMock()
        tr.id = "call_dedup"
        tr.content = "sent ok"
        tool_results = [tr]

        _check_external_send(agent, [tc2], tool_results)
        # Tool result should contain the warning
        assert "Recently sent similar message" in tr.content
        # Original content preserved
        assert "sent ok" in tr.content
        # Only the first send was recorded (dedup skips recording)
        assert len(agent._sent_tracker._entries) == 1

    def test_dedup_warns_when_tool_result_content_is_dict(self):
        """Regression for lingtai#117: dict content must not raise TypeError.

        ToolResultBlock.content is Any (str or dict). The dedup-warning
        path previously did `(content or "") + "..."` which crashed with
        `unsupported operand type(s) for +: 'dict' and 'str'` whenever an
        MCP tool (e.g. telegram) returned a structured result and the
        send happened to match a recent one.
        """
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc1 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        _check_external_send(agent, [tc1])

        tc2 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        tc2.id = "call_dedup_dict"

        tr = MagicMock()
        tr.id = "call_dedup_dict"
        tr.content = {"status": "sent", "message_id": "tg:1:42"}
        tool_results = [tr]

        _check_external_send(agent, [tc2], tool_results)
        assert isinstance(tr.content, dict)
        assert tr.content.get("_duplicate_warning", "").startswith(
            "Recently sent similar message"
        )
        # Original fields preserved
        assert tr.content["status"] == "sent"
        assert tr.content["message_id"] == "tg:1:42"

    def test_dedup_without_tool_results(self):
        """Dedup still skips recording when tool_results not passed."""
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc1 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        _check_external_send(agent, [tc1])

        tc2 = self._make_tc("telegram", {
            "action": "send",
            "message": "hello human",
            "chat_id": "12345",
        })
        _check_external_send(agent, [tc2])
        # Only first send recorded
        assert len(agent._sent_tracker._entries) == 1

    def test_check_action_not_a_send(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc = self._make_tc("telegram", {"action": "check"})
        _check_external_send(agent, [tc])
        assert len(agent._sent_tracker._entries) == 0

    def test_internal_mail_ignored(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc = self._make_tc("email", {
            "action": "send",
            "address": "agent@network",
            "message": "hello",
        })
        _check_external_send(agent, [tc])
        assert len(agent._sent_tracker._entries) == 0

    def test_multiple_tools_mixed(self):
        from lingtai_kernel.base_agent.turn import _check_external_send
        agent = self._make_agent()
        tc_read = self._make_tc("read", {"path": "/tmp"})
        tc_send = self._make_tc("feishu", {
            "action": "send",
            "message": "hi",
            "to": "user123",
        })
        _check_external_send(agent, [tc_read, tc_send])
        assert len(agent._sent_tracker._entries) == 1


class TestCheckPollBackoff:
    """Test the _check_poll_backoff function from turn.py."""

    def _make_agent(self):
        agent = MagicMock()
        agent._sent_tracker = SentMessageTracker()
        agent._log = MagicMock()
        return agent

    def _make_tc(self, name, args, id="tc-1"):
        tc = MagicMock()
        tc.id = id
        tc.name = name
        tc.args = args
        return tc

    def test_first_check_not_backoff(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        agent = self._make_agent()
        tc = self._make_tc("telegram", {"action": "check"})
        assert not _check_poll_backoff(agent, [tc])

    def test_three_checks_trigger_backoff(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        agent = self._make_agent()
        tc = self._make_tc("telegram", {"action": "check"})
        _check_poll_backoff(agent, [tc])  # 1
        _check_poll_backoff(agent, [tc])  # 2
        assert _check_poll_backoff(agent, [tc])  # 3 — triggers

    def test_send_resets_backoff(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        agent = self._make_agent()
        tc_check = self._make_tc("telegram", {"action": "check"})
        tc_send = self._make_tc("telegram", {
            "action": "send",
            "message": "hi",
            "chat_id": "123",
        })
        _check_poll_backoff(agent, [tc_check])  # 1
        _check_poll_backoff(agent, [tc_check])  # 2
        _check_poll_backoff(agent, [tc_send])   # send resets
        assert not _check_poll_backoff(agent, [tc_check])  # 1 again

    def test_read_result_with_messages_resets_backoff(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        from lingtai_kernel.llm.interface import ToolResultBlock

        agent = self._make_agent()
        tc = self._make_tc("telegram", {"action": "read"}, id="tc-read")

        _check_poll_backoff(agent, [tc])
        _check_poll_backoff(agent, [tc])
        result = ToolResultBlock(
            id="tc-read",
            name="telegram",
            content={"messages": [{"id": "msg-1", "text": "hello"}]},
        )

        assert not _check_poll_backoff(agent, [tc], [result])
        assert agent._sent_tracker._poll_counts.get("telegram", 0) == 0

    def test_check_result_with_emails_or_conversations_resets_backoff(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        from lingtai_kernel.llm.interface import ToolResultBlock

        for tool_name, content in (
            ("imap", '{"emails": [{"subject": "hi"}]}'),
            ("feishu", {"conversations": [{"chat_id": "oc_1"}]}),
        ):
            agent = self._make_agent()
            tc = self._make_tc(tool_name, {"action": "check"}, id=f"{tool_name}-check")
            _check_poll_backoff(agent, [tc])
            _check_poll_backoff(agent, [tc])
            result = ToolResultBlock(id=tc.id, name=tool_name, content=content)

            assert not _check_poll_backoff(agent, [tc], [result])
            assert agent._sent_tracker._poll_counts.get(tool_name, 0) == 0

    def test_non_external_tool_ignored(self):
        from lingtai_kernel.base_agent.turn import _check_poll_backoff
        agent = self._make_agent()
        tc = self._make_tc("email", {"action": "check"})
        for _ in range(5):
            assert not _check_poll_backoff(agent, [tc])


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_send_tools_are_external(self):
        # email is internal (agent-to-agent), should NOT be in SEND_TOOLS
        assert "email" not in SEND_TOOLS
        assert "telegram" in SEND_TOOLS
        assert "imap" in SEND_TOOLS
        assert "whatsapp" in SEND_TOOLS
        assert "wechat" in SEND_TOOLS
        assert "feishu" in SEND_TOOLS

    def test_send_actions(self):
        assert "send" in SEND_ACTIONS
        assert "reply" in SEND_ACTIONS
        assert "check" not in SEND_ACTIONS

    def test_check_actions(self):
        assert "check" in CHECK_ACTIONS
        assert "read" in CHECK_ACTIONS
        assert "send" not in CHECK_ACTIONS
