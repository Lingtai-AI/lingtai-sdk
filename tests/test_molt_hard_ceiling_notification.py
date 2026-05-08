"""Tests for the two-phase hard ceiling molt behavior.

When context pressure jumps straight to the hard ceiling (>= 0.95) without
prior graduated warnings, the agent should receive a notification FIRST and
only be force-wiped on a subsequent turn where pressure is still at the
hard ceiling.

See: fix/molt-hard-ceiling-notification branch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHardCeilingTwoPhase:
    """Two-phase hard ceiling: warn first, wipe second."""

    def test_first_hard_ceiling_hit_publishes_notification_no_wipe(self, tmp_path):
        """When pressure jumps to hard ceiling with 0 prior warnings,
        a notification is published but context_forget is NOT called."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            agent._config.context_limit = 100_000
            agent._session.get_context_pressure = lambda: 0.96
            agent._session._compaction_warnings = 0

            sent_content = []
            agent._session.send = lambda content: (
                sent_content.append(content), _make_mock_response()
            )[-1]

            with patch(
                "lingtai_kernel.intrinsics.psyche.context_forget"
            ) as mock_forget:
                _send_request(agent)

                # context_forget must NOT have been called
                mock_forget.assert_not_called()

            # Warning counter incremented
            assert agent._session._compaction_warnings == 1

            # Notification file written with critical priority
            notif_path = agent._working_dir / ".notification" / "molt.json"
            assert notif_path.is_file(), (
                "first hard-ceiling hit should publish a notification"
            )
            notif = json.loads(notif_path.read_text())
            assert notif["priority"] == "high"
            assert "CRITICAL" in notif["header"]

            # User content should NOT contain the wipe notice
            assert len(sent_content) > 0
            for c in sent_content:
                assert "molt_wiped" not in str(c).lower()

        finally:
            agent.stop()

    def test_second_hard_ceiling_hit_no_force_wipe(self, tmp_path):
        """When pressure is at hard ceiling AND warnings > 0,
        context_forget is NOT called (force-wipe removed).
        Warnings accumulate instead."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            agent._config.context_limit = 100_000
            agent._session.get_context_pressure = lambda: 0.96
            # Simulate: agent was already warned on a prior turn
            agent._session._compaction_warnings = 1

            sent_content = []
            agent._session.send = lambda content: (
                sent_content.append(content), _make_mock_response()
            )[-1]

            with patch(
                "lingtai_kernel.intrinsics.psyche.context_forget"
            ) as mock_forget:
                _send_request(agent)

                # Force-wipe must NOT fire (removed in PR #32)
                mock_forget.assert_not_called()

            # Counter increments (warnings accumulate, not reset)
            assert agent._session._compaction_warnings == 2

        finally:
            agent.stop()

    def test_pressure_drop_between_turns_preserves_counter(self, tmp_path):
        """If pressure drops below hard ceiling after the first warning,
        the counter stays non-zero. When pressure climbs back up,
        warnings continue to accumulate (no force-wipe)."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            agent._config.context_limit = 100_000
            agent._session.send = lambda content: _make_mock_response()

            # Turn 1: pressure at hard ceiling, no prior warnings → warn only
            agent._session.get_context_pressure = lambda: 0.96
            agent._session._compaction_warnings = 0

            with patch(
                "lingtai_kernel.intrinsics.psyche.context_forget"
            ) as mock_forget:
                _send_request(agent, "turn 1")
                mock_forget.assert_not_called()
            assert agent._session._compaction_warnings == 1

            # Turn 2: pressure drops to graduated warning band
            # (below hard ceiling but above molt_pressure).
            # The elif branch fires: counter increments to 2.
            agent._session.get_context_pressure = lambda: 0.85
            _send_request(agent, "turn 2")
            assert agent._session._compaction_warnings == 2

            # Turn 3: pressure spikes back to hard ceiling.
            # Counter is > 0, but force-wipe is removed (PR #32).
            # Warnings accumulate instead.
            agent._session.get_context_pressure = lambda: 0.97

            with patch(
                "lingtai_kernel.intrinsics.psyche.context_forget"
            ) as mock_forget:
                _send_request(agent, "turn 3")
                mock_forget.assert_not_called()
            assert agent._session._compaction_warnings == 3

        finally:
            agent.stop()

    def test_pressure_drop_below_threshold_clears_notification(self, tmp_path):
        """If pressure drops below molt_pressure, the notification file
        is cleared — but the counter stays so a future spike wipes."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            agent._config.context_limit = 100_000
            agent._session.send = lambda content: _make_mock_response()

            # Turn 1: hard ceiling, first hit → notification published
            agent._session.get_context_pressure = lambda: 0.96
            agent._session._compaction_warnings = 0
            _send_request(agent, "turn 1")
            notif_path = agent._working_dir / ".notification" / "molt.json"
            assert notif_path.is_file()

            # Turn 2: pressure drops well below threshold
            agent._session.get_context_pressure = lambda: 0.50
            _send_request(agent, "turn 2")
            # Notification cleared by the else branch
            assert not notif_path.is_file()
            # Counter stays at 1 (not cleared by pressure drop)
            assert agent._session._compaction_warnings == 1

        finally:
            agent.stop()

    def test_hard_ceiling_notification_data_shape(self, tmp_path):
        """The notification published on first hard-ceiling hit should
        carry the expected data fields."""
        agent = _make_agent_with_psyche(tmp_path)
        agent.start()
        try:
            agent._config.context_limit = 100_000
            agent._session.get_context_pressure = lambda: 0.97
            agent._session._compaction_warnings = 0
            agent._session.send = lambda content: _make_mock_response()

            _send_request(agent)

            notif_path = agent._working_dir / ".notification" / "molt.json"
            notif = json.loads(notif_path.read_text())
            data = notif["data"]

            assert data["pressure"] == pytest.approx(0.97)
            assert data["level"] == 3
            assert data["warnings"] == 1
            assert "CRITICAL" in data["status"]

        finally:
            agent.stop()
