"""Tests for lingtai_kernel.intrinsics.soul.

After the past-self-consultation refactor, this file covers only:
- The agent-callable surface (``handle``): inquiry action, flow rejection,
  unknown-action error.
- The wall-clock soul timer (``_start_soul_timer`` / ``_cancel_soul_timer``)
  that drives consultation cadence.

The legacy diary+mirror-session machinery (``soul_flow``,
``_collect_new_diary``, ``_ensure_soul_session``, ``_save_soul_session``,
``_trim_soul_session``, ``reset_soul_session``, ``enqueue_flow_voice``,
``_soul_history_path``, ``_soul_cursor_path``) has been removed; tests for
it are gone with it. The new mechanism is covered in
``tests/test_soul_consultation.py``.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from lingtai_kernel.config import AgentConfig
from lingtai_kernel.intrinsics import soul


def _make_mock_agent():
    """Tiny mock for direct ``handle`` calls — no real LLM, no real chat."""
    agent = MagicMock()
    agent._soul_delay = 120.0
    return agent


def _make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


# ---------------------------------------------------------------------------
# soul.handle — agent-callable surface
# ---------------------------------------------------------------------------


class TestSoulHandle:

    def test_inquiry_returns_voice(self):
        agent = _make_mock_agent()
        agent._config.retry_timeout = 30.0
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "What am I missing?"})
        assert result["status"] == "ok"
        assert "voice" in result

    def test_inquiry_requires_text(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry"})
        assert "error" in result

    def test_inquiry_rejects_empty(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "   "})
        assert "error" in result

    def test_flow_action_voluntary_succeeds_when_lock_free(self):
        """Voluntary flow returns ok when no fire is in flight; the real
        consultation runs on a daemon thread and lands later via tc_inbox."""
        agent = _make_mock_agent()
        agent._soul_fire_lock = threading.Lock()
        result = soul.handle(agent, {"action": "flow"})
        assert result.get("status") == "ok"
        assert "soul flow triggered" in result.get("message", "").lower()

    def test_flow_action_rejected_when_fire_in_flight(self):
        """Voluntary flow refuses if another fire (timer or prior voluntary)
        already holds the fire lock."""
        agent = _make_mock_agent()
        lock = threading.Lock()
        lock.acquire()
        agent._soul_fire_lock = lock
        try:
            result = soul.handle(agent, {"action": "flow"})
        finally:
            lock.release()
        assert "error" in result
        assert "ongoing" in result["error"]

    def test_flow_voluntary_waits_for_idle(self, monkeypatch):
        """Voluntary flow daemon thread waits for _idle event before
        calling _run_consultation_fire (race condition fix)."""
        agent = _make_mock_agent()
        agent._soul_fire_lock = threading.Lock()
        agent._soul_delay = 5.0
        idle_event = threading.Event()
        # Simulate ACTIVE — _idle is cleared
        idle_event.clear()
        agent._idle = idle_event

        fire_called = threading.Event()
        original_fire = soul._run_consultation_fire

        def tracking_fire(a):
            fire_called.set()

        monkeypatch.setattr(
            "lingtai_kernel.intrinsics.soul.flow._run_consultation_fire",
            tracking_fire,
        )

        result = soul.handle(agent, {"action": "flow"})
        assert result.get("status") == "ok"

        # Fire should NOT have been called yet — still ACTIVE
        assert not fire_called.wait(timeout=0.2)

        # Simulate transition to IDLE
        idle_event.set()
        assert fire_called.wait(timeout=2.0)

    def test_flow_voluntary_timeout_when_never_idle(self, monkeypatch):
        """Voluntary flow gives up if IDLE never arrives within timeout."""
        agent = _make_mock_agent()
        agent._soul_fire_lock = threading.Lock()
        agent._soul_delay = 0.3  # Short timeout for test speed
        idle_event = threading.Event()
        idle_event.clear()
        agent._idle = idle_event

        fire_called = threading.Event()

        def tracking_fire(a):
            fire_called.set()

        monkeypatch.setattr(
            "lingtai_kernel.intrinsics.soul.flow._run_consultation_fire",
            tracking_fire,
        )

        result = soul.handle(agent, {"action": "flow"})
        assert result.get("status") == "ok"

        # Wait longer than the timeout — fire should never be called
        time.sleep(0.6)
        assert not fire_called.is_set()

    def test_unknown_action_returns_error(self):
        agent = _make_mock_agent()
        result = soul.handle(agent, {"action": "on"})
        assert "error" in result

    def test_inquiry_works_with_large_delay(self):
        """Inquiry is independent of soul_delay value — no timer interaction."""
        agent = _make_mock_agent()
        agent._soul_delay = 999999.0
        agent._config.retry_timeout = 30.0
        result = soul.handle(agent, {"action": "inquiry", "inquiry": "Am I stuck?"})
        assert result["status"] == "ok"
        assert "voice" in result


# ---------------------------------------------------------------------------
# soul.get_schema — public schema shape
# ---------------------------------------------------------------------------


class TestSoulSchema:

    def test_schema_exposes_five_actions(self):
        schema = soul.get_schema("en")
        # Five actions are agent-visible: inquiry (manual self-Q&A),
        # flow (mechanical, fires only on the wall clock / turn counter —
        # agent cannot invoke), config (agent adjusts cadence + K
        # at runtime), voice (agent picks/customizes own soul-flow
        # prompt — read or set), and dismiss (clear soul notification).
        assert schema["properties"]["action"]["enum"] == [
            "inquiry", "flow", "config", "voice", "dismiss",
        ]

    def test_schema_inquiry_property_present(self):
        schema = soul.get_schema("en")
        assert "inquiry" in schema["properties"]

    def test_schema_config_properties_present(self):
        # config parameters — delay_seconds (number, min 30s),
        # consultation_past_count (integer, [0, 5]).
        schema = soul.get_schema("en")
        assert "delay_seconds" in schema["properties"]
        assert schema["properties"]["delay_seconds"]["type"] == "number"
        assert schema["properties"]["delay_seconds"]["minimum"] == 30.0
        assert "consultation_interval" not in schema["properties"]
        assert "consultation_past_count" in schema["properties"]
        assert schema["properties"]["consultation_past_count"]["type"] == "integer"

    def test_schema_required_is_action(self):
        assert soul.get_schema("en")["required"] == ["action"]


# ---------------------------------------------------------------------------
# Soul timer — wall-clock cadence that drives _run_consultation_fire
# ---------------------------------------------------------------------------


class TestSoulTimer:

    def test_soul_attributes_initialized_default(self, tmp_path):
        """BaseAgent with default config has soul_delay=999999999."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 999999999.0
        assert agent._soul_timer is None

    def test_soul_timer_lifecycle_follows_idle_state(self, tmp_path):
        """Timer starts on IDLE entry and cancels on IDLE exit.

        _set_state starts a soul timer when entering IDLE and cancels it
        when leaving IDLE (to ACTIVE, STUCK, etc.).
        """
        from lingtai_kernel import AgentState, BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 300.0

        # Going ACTIVE from initial IDLE cancels the timer (if any).
        agent._set_state(AgentState.ACTIVE, reason="test")
        assert agent._soul_timer is None

        # Entering IDLE starts a fresh timer.
        agent._set_state(AgentState.IDLE, reason="done")
        assert agent._soul_timer is not None
        assert agent._soul_timer.is_alive()

        # Leaving IDLE cancels it.
        agent._set_state(AgentState.ACTIVE, reason="new mail")
        assert agent._soul_timer is None

    def test_soul_timer_not_started_when_shutdown(self, tmp_path):
        """_start_soul_timer is a no-op when _shutdown is set."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 1.0
        agent._shutdown.set()
        agent._start_soul_timer()
        assert agent._soul_timer is None

    def test_soul_delay_from_config(self, tmp_path):
        """soul_delay in config sets initial _soul_delay."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            config=AgentConfig(soul_delay=60.0),
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 60.0

    def test_soul_delay_clamped_to_min(self, tmp_path):
        """soul_delay below 1 is clamped to 1."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            config=AgentConfig(soul_delay=-10.0),
            working_dir=tmp_path / "test_agent",
        )
        assert agent._soul_delay == 1.0

    def test_stop_cancels_soul_timer(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._soul_delay = 300.0
        agent._start_soul_timer()
        assert agent._soul_timer is not None
        agent.stop()
        assert agent._soul_timer is None


class TestSoulFireAllowed:
    """_soul_fire_allowed compares by string value, not enum identity."""

    def test_allows_idle_state(self):
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed
        from lingtai_kernel.state import AgentState
        agent = MagicMock()
        agent._state = AgentState.IDLE
        assert _soul_fire_allowed(agent) is True

    def test_rejects_active_state(self):
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed
        from lingtai_kernel.state import AgentState
        agent = MagicMock()
        agent._state = AgentState.ACTIVE
        assert _soul_fire_allowed(agent) is False

    def test_allows_foreign_enum_with_idle_value(self):
        """Simulates stale-enum mismatch: a different Enum class whose
        .value is 'idle' should still be accepted."""
        import enum
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed

        class ForeignState(enum.Enum):
            IDLE = "idle"

        agent = MagicMock()
        agent._state = ForeignState.IDLE
        assert _soul_fire_allowed(agent) is True

    def test_rejects_foreign_enum_with_active_value(self):
        import enum
        from lingtai_kernel.intrinsics.soul.flow import _soul_fire_allowed

        class ForeignState(enum.Enum):
            ACTIVE = "active"

        agent = MagicMock()
        agent._state = ForeignState.ACTIVE
        assert _soul_fire_allowed(agent) is False


def test_consultation_fire_discards_late_result_after_state_change(monkeypatch):
    """If the agent becomes STUCK while consultation is running, the late
    result must not enqueue a TC wake into an unsafe interface window.

    The fire starts while IDLE (passes the gate), but the batch callback
    transitions the agent to STUCK mid-flight — the post-batch state
    check must discard the result.
    """
    from lingtai_kernel.intrinsics.soul import flow
    from lingtai_kernel.llm.interface import TextBlock
    from lingtai_kernel.state import AgentState

    agent = MagicMock()
    agent._state = AgentState.IDLE  # Must start IDLE to pass the gate
    agent._logs = []
    agent._tc_inbox.enqueue = MagicMock()

    def log(event_type, **fields):
        agent._logs.append((event_type, fields))
    agent._log.side_effect = log

    monkeypatch.setattr(flow, "_append_soul_flow_record", MagicMock())

    def fake_batch(_agent):
        _agent._state = AgentState.STUCK
        return [{"source": "insights", "blocks": [TextBlock(text="late")]}]

    monkeypatch.setattr(
        "lingtai_kernel.intrinsics.soul.consultation._render_current_diary",
        lambda _agent: "diary",
    )
    monkeypatch.setattr(
        "lingtai_kernel.intrinsics.soul.consultation._run_consultation_batch",
        fake_batch,
    )
    monkeypatch.setattr(
        "lingtai_kernel.intrinsics.soul.consultation.build_consultation_pair",
        MagicMock(),
    )

    flow._run_consultation_fire(agent)

    # Soul flow no longer enqueues a TC wake directly; filesystem
    # notification + heartbeat sync owns injection/wake-up. A mid-flight state
    # change therefore must not touch tc_inbox, and the fire is allowed to
    # publish/log through the notification path without the old
    # consultation_discarded_state event.
    agent._tc_inbox.enqueue.assert_not_called()
    assert any(name == "consultation_fire" for name, _ in agent._logs)
