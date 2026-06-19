"""Tests for notification persistence across molt.

After a molt (agent-initiated or system-initiated), .notification/ files
on disk should survive — they are system state, not conversation memory.
In-memory tracking is reset so the next sync re-reads from disk cleanly.

See: fix/molt-notification-persistence branch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_with_psyche(tmp_path):
    """Create an Agent with psyche capability and mocked LLM service."""
    from lingtai.agent import Agent

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return Agent(
        service=svc, agent_name="test", working_dir=tmp_path / "test",
        capabilities=["psyche"],
    )


def _make_mock_response():
    resp = MagicMock()
    resp.text = "ok"
    resp.tool_calls = []
    resp.thoughts = []
    resp.usage = None
    return resp


def _send_request(agent, content="do something"):
    from lingtai_kernel.message import _make_message, MSG_REQUEST
    msg = _make_message(MSG_REQUEST, sender="test", content=content)
    agent._handle_request(msg)


def _create_notification_file(agent, name="email.json", data=None):
    """Create a .notification/ file with test data."""
    notif_dir = agent._working_dir / ".notification"
    notif_dir.mkdir(parents=True, exist_ok=True)
    if data is None:
        data = {
            "header": "test notification",
            "icon": "📧",
            "priority": "normal",
            "data": {"test": True},
        }
    path = notif_dir / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _setup_mock_chat(agent):
    """Set up a mock chat interface that survives ensure_session calls."""
    mock_interface = MagicMock()
    mock_interface.entries = []
    mock_interface.estimate_context_tokens.return_value = 50000

    mock_chat = MagicMock()
    mock_chat.interface = mock_interface

    # Make ensure_session create a proper mock chat when _chat is None
    original_ensure = agent._session.ensure_session

    def patched_ensure():
        if agent._session._chat is None:
            new_interface = MagicMock()
            new_interface.entries = []
            new_interface.estimate_context_tokens.return_value = 5000
            new_chat = MagicMock()
            new_chat.interface = new_interface
            agent._session._chat = new_chat
        return agent._session._chat

    agent._session.ensure_session = patched_ensure
    # Set the initial chat
    agent._session._chat = mock_chat
    agent._chat = mock_chat

    # Ensure .agent.json exists so write_manifest's os.replace doesn't fail
    manifest_path = agent._working_dir / ".agent.json"
    if not manifest_path.exists():
        manifest_path.write_text("{}")

    return mock_interface


# ---------------------------------------------------------------------------
# Tests: notification files survive molt
# ---------------------------------------------------------------------------


class TestNotificationPersistenceAgentMolt:
    """Notification files survive agent-initiated _context_molt."""

    def test_notification_files_survive_agent_molt(self, tmp_path):
        """After an agent-initiated molt, .notification/ directory and its
        files should still exist on disk."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            # Create a notification file before molt
            notif_path = _create_notification_file(agent, "email.json")
            assert notif_path.is_file()

            # Set up mock chat
            mock_interface = _setup_mock_chat(agent)

            # Build a fake ToolCallBlock for the molt
            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            from lingtai_kernel.llm.interface import ToolCallBlock
            tc_id = "toolu_test_123"
            tc_block = ToolCallBlock(
                id=tc_id, name="psyche",
                args={"object": "context", "action": "molt",
                      "summary": "test summary for molt"},
            )
            # Place the ToolCallBlock in a fake assistant entry
            mock_entry = MagicMock()
            mock_entry.role = "assistant"
            mock_entry.content = [tc_block]
            mock_interface.entries = [mock_entry]

            result = _context_molt(agent, {
                "summary": "test summary for molt",
                "_tc_id": tc_id,
            })

            # Molt should succeed
            assert result.get("status") == "ok"

            # .notification/ directory and file should still exist
            notif_dir = agent._working_dir / ".notification"
            assert notif_dir.is_dir(), (
                ".notification/ directory should survive agent-initiated molt"
            )
            assert notif_path.is_file(), (
                "notification files should survive agent-initiated molt"
            )

            # Verify content is unchanged
            data = json.loads(notif_path.read_text())
            assert data["header"] == "test notification"

        finally:
            agent.stop()

    def test_multiple_notification_files_survive_agent_molt(self, tmp_path):
        """Multiple notification files should all survive molt."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            # Create multiple notification files
            email_path = _create_notification_file(agent, "email.json", {
                "header": "new email", "icon": "📧",
                "priority": "normal", "data": {},
            })
            molt_path = _create_notification_file(agent, "molt.json", {
                "header": "pressure warning", "icon": "⚠️",
                "priority": "high", "data": {"pressure": 0.85},
            })

            assert email_path.is_file()
            assert molt_path.is_file()

            # Set up mock chat
            mock_interface = _setup_mock_chat(agent)

            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            from lingtai_kernel.llm.interface import ToolCallBlock

            tc_id = "toolu_test_456"
            tc_block = ToolCallBlock(
                id=tc_id, name="psyche",
                args={"object": "context", "action": "molt",
                      "summary": "multi-file test"},
            )
            mock_entry = MagicMock()
            mock_entry.role = "assistant"
            mock_entry.content = [tc_block]
            mock_interface.entries = [mock_entry]

            result = _context_molt(agent, {
                "summary": "multi-file test",
                "_tc_id": tc_id,
            })

            assert result.get("status") == "ok"
            assert email_path.is_file(), "email.json should survive molt"
            assert molt_path.is_file(), "molt.json should survive molt"

        finally:
            agent.stop()


class TestNotificationPersistenceForceWipe:
    """Notification files survive system-initiated context_forget."""

    def test_notification_files_survive_context_forget(self, tmp_path):
        """After a system-initiated force molt, .notification/ directory
        and its files should still exist on disk."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            # Create a notification file before force molt
            notif_path = _create_notification_file(agent, "email.json")
            assert notif_path.is_file()

            # Set up mock chat
            _setup_mock_chat(agent)

            # Perform system-initiated force molt
            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="warning_ladder")

            # Force molt should succeed
            assert result.get("status") == "ok"

            # .notification/ directory and file should still exist
            notif_dir = agent._working_dir / ".notification"
            assert notif_dir.is_dir(), (
                ".notification/ directory should survive context_forget"
            )
            assert notif_path.is_file(), (
                "notification files should survive context_forget"
            )

            # Verify content is unchanged
            data = json.loads(notif_path.read_text())
            assert data["header"] == "test notification"

        finally:
            agent.stop()

    def test_notification_files_survive_aed_forget(self, tmp_path):
        """Notification files survive context_forget triggered by AED."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            notif_path = _create_notification_file(agent, "email.json")
            assert notif_path.is_file()

            _setup_mock_chat(agent)

            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="aed", attempts=3)

            assert result.get("status") == "ok"
            assert notif_path.is_file(), (
                "notification files should survive AED-triggered context_forget"
            )

        finally:
            agent.stop()


# ---------------------------------------------------------------------------
# Tests: in-memory tracking state after molt
# ---------------------------------------------------------------------------


class TestNotificationTrackingStateAfterMolt:
    """In-memory tracking is reset so notification files rehydrate post-molt."""

    def test_tracking_state_reset_after_agent_molt(self, tmp_path):
        """After agent-initiated molt, all in-memory notification
        tracking should reset while files stay on disk."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            # Create a notification file so the fingerprint is stable
            # (background _sync_notifications won't overwrite our fp)
            notif_path = _create_notification_file(agent, "email.json")
            # Read the actual fingerprint from disk
            stat = notif_path.stat()
            real_fp = ("email.json", stat.st_mtime_ns, stat.st_size)

            # Set tracking state to verify reset behavior
            agent._notification_block_id = "block_123"
            agent._pending_notification_meta = {"test": "meta"}
            agent._pending_notification_fp = real_fp
            agent._notification_fp = real_fp

            # Set up mock chat
            mock_interface = _setup_mock_chat(agent)

            from lingtai_kernel.intrinsics.psyche._molt import _context_molt
            from lingtai_kernel.llm.interface import ToolCallBlock

            tc_id = "toolu_test_789"
            tc_block = ToolCallBlock(
                id=tc_id, name="psyche",
                args={"object": "context", "action": "molt",
                      "summary": "tracking test"},
            )
            mock_entry = MagicMock()
            mock_entry.role = "assistant"
            mock_entry.content = [tc_block]
            mock_interface.entries = [mock_entry]

            result = _context_molt(agent, {
                "summary": "tracking test",
                "_tc_id": tc_id,
            })

            assert result.get("status") == "ok"

            # _notification_block_id should be reset to None
            assert agent._notification_block_id is None, (
                "_notification_block_id should be None after molt"
            )
            # _pending_notification_meta should be reset to None
            assert agent._pending_notification_meta is None, (
                "_pending_notification_meta should be None after molt"
            )
            # _notification_fp and pending fp reset so the surviving file
            # re-injects into the fresh wire on the next sync.
            assert agent._notification_fp == (), (
                "_notification_fp should reset after molt for rehydration"
            )
            assert agent._pending_notification_fp is None, (
                "_pending_notification_fp should be None after molt"
            )

        finally:
            agent.stop()

    def test_tracking_state_reset_after_context_forget(self, tmp_path):
        """After system-initiated context_forget, all in-memory
        notification tracking should reset while files stay on disk."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            # Create a notification file so the fingerprint is stable
            notif_path = _create_notification_file(agent, "molt.json", {
                "header": "pressure", "icon": "⚠️",
                "priority": "high", "data": {"pressure": 0.9},
            })
            stat = notif_path.stat()
            real_fp = ("molt.json", stat.st_mtime_ns, stat.st_size)

            # Set tracking state
            agent._notification_block_id = "block_456"
            agent._pending_notification_meta = {"source": "email"}
            agent._pending_notification_fp = real_fp
            agent._notification_fp = real_fp

            # Set up mock chat
            _setup_mock_chat(agent)

            from lingtai_kernel.intrinsics.psyche._molt import context_forget
            result = context_forget(agent, source="warning_ladder")

            assert result.get("status") == "ok"

            # _notification_block_id should be reset to None
            assert agent._notification_block_id is None, (
                "_notification_block_id should be None after context_forget"
            )
            # _pending_notification_meta should be reset to None
            assert agent._pending_notification_meta is None, (
                "_pending_notification_meta should be None after context_forget"
            )
            # _notification_fp and pending fp reset so the surviving file
            # re-injects into the fresh wire on the next sync.
            assert agent._notification_fp == (), (
                "_notification_fp should reset after context_forget"
            )
            assert agent._pending_notification_fp is None, (
                "_pending_notification_fp should be None after context_forget"
            )

        finally:
            agent.stop()


