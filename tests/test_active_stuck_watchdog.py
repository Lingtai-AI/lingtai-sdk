"""Tests for the issue #164 ACTIVE-without-progress watchdog and the
runtime diagnostics it exposes through ``.status.json``.

The watchdog protects against two real-world failure modes documented in
``reports/dual-agent-stuck-forensics-2026-05-26.md``:

1. After a refresh, an agent transitions to ACTIVE but never emits
   ``wake`` / ``llm_call``, then sits there indefinitely while
   ``notification_deferred_active`` accumulates.
2. A long synchronous tool keeps the agent ACTIVE with no LLM/turn
   progress events.

Both cases used to be invisible to the heartbeat-only liveness check.
"""
import json
import time
from unittest.mock import MagicMock


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


class TestProgressBookkeeping:
    """``_log`` taps known progress events to bump ``_last_progress_at``
    and refine the active-turn block surfaced in ``.status.json``."""

    def test_state_change_bumps_progress_and_state_change_clocks(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        before = agent._last_progress_at
        time.sleep(0.01)
        agent._set_state(AgentState.ACTIVE, reason="test")
        assert agent._last_progress_at > before
        assert agent._state_changed_at > before

    def test_active_seeds_pending_turn_kind(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        assert agent._active_turn_kind == "pending"
        assert agent._active_turn_started_at is not None

    def test_llm_call_event_refines_turn_kind(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._log("llm_call")
        assert agent._active_turn_kind == "llm_call"

    def test_tool_call_event_refines_turn_kind_and_records_id(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._log("tool_call", tool_call_id="call_abc123")
        assert agent._active_turn_kind == "tool_call"
        assert agent._active_turn_id == "call_abc123"

    def test_leaving_active_clears_turn_block_and_latch(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._log("tool_call", tool_call_id="call_abc")
        agent._active_stuck_logged = True  # simulate watchdog fired
        agent._set_state(AgentState.IDLE)
        assert agent._active_turn_kind is None
        assert agent._active_turn_id is None
        assert agent._active_stuck_logged is False


class TestDeferredNotificationsCounter:
    """``notification_deferred_active`` events accumulate into the
    runtime status block so an ops grep on ``.status.json`` sees the
    storm without scanning ``events.jsonl``."""

    def test_counter_increments(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        assert agent._deferred_notifications_count == 0
        agent._log("notification_deferred_active", sources=["telegram"])
        agent._log("notification_deferred_active", sources=["telegram"])
        agent._log("notification_deferred_active", sources=["telegram"])
        assert agent._deferred_notifications_count == 3
        assert agent._deferred_notifications_oldest_at is not None

    def test_state_change_resets_counter(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._log("notification_deferred_active", sources=["telegram"])
        agent._log("notification_deferred_active", sources=["telegram"])
        agent._set_state(AgentState.ACTIVE, reason="test")
        assert agent._deferred_notifications_count == 0
        assert agent._deferred_notifications_oldest_at is None


class TestStatusJsonExposesActiveTurn:
    """``.status.json`` (``agent.status()``) carries the new diagnostic
    fields so external observers can distinguish "actually computing"
    from "wedged ACTIVE."""

    def test_status_has_state_changed_at_and_last_progress_at(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        status = agent.status()
        runtime = status["runtime"]
        assert "state_changed_at" in runtime
        assert "last_progress_at" in runtime
        assert "no_progress_seconds" in runtime
        # no_progress_seconds is a non-negative float
        assert isinstance(runtime["no_progress_seconds"], (int, float))
        assert runtime["no_progress_seconds"] >= 0

    def test_status_active_turn_block_present_only_in_active(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        # IDLE — no active_turn block
        assert "active_turn" not in agent.status()
        # ACTIVE — block appears
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._log("llm_call")
        status = agent.status()
        assert "active_turn" in status
        assert status["active_turn"]["kind"] == "llm_call"
        # Back to IDLE — block clears
        agent._set_state(AgentState.IDLE)
        assert "active_turn" not in agent.status()

    def test_status_deferred_notifications_block(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        assert "deferred_notifications" not in agent.status()
        agent._log("notification_deferred_active", sources=["telegram"])
        agent._log("notification_deferred_active", sources=["telegram"])
        status = agent.status()
        assert "deferred_notifications" in status
        assert status["deferred_notifications"]["count"] == 2
        assert status["deferred_notifications"]["reason"] == "active"


class TestWatchdogFires:
    """Heartbeat watchdog logs ``active_without_progress`` once per stuck
    episode."""

    def test_watchdog_log_fires_when_active_with_no_progress(self, tmp_path, monkeypatch):
        # Force a 0-second threshold so the watchdog trips immediately.
        monkeypatch.setenv("LINGTAI_ACTIVE_STUCK_THRESHOLD_S", "30")

        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        # Simulate: agent is ACTIVE, no progress for >> threshold.
        # Use a low env threshold via clamp (min 30) and rewind the
        # progress clock instead of sleeping for a test-friendly run.
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._last_progress_at = time.time() - 120  # 2 minutes ago

        logged: list[dict] = []
        original_log = agent._log_service.log if agent._log_service else None

        def capture_log(entry):
            logged.append(entry)
            if original_log:
                original_log(entry)

        if agent._log_service:
            agent._log_service.log = capture_log

        agent._start_heartbeat()
        # Heartbeat ticks once per second; give it 2.5s to be sure.
        time.sleep(2.5)
        agent._stop_heartbeat()

        kinds = [e.get("type") for e in logged]
        assert "active_without_progress" in kinds, (
            f"watchdog did not fire; saw events: {kinds}"
        )
        # Check structured fields are present on the event.
        evt = next(e for e in logged if e.get("type") == "active_without_progress")
        assert "no_progress_seconds" in evt
        assert "threshold_seconds" in evt
        assert evt["no_progress_seconds"] >= 30

    def test_watchdog_writes_fresh_status_json_when_active_stuck(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LINGTAI_ACTIVE_STUCK_THRESHOLD_S", "30")

        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._write_status_snapshot()
        stale_status = json.loads((agent._working_dir / ".status.json").read_text())
        assert stale_status["runtime"]["state"] == "idle"

        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._log("tool_call", tool_call_id="call_active")
        agent._last_progress_at = time.time() - 120

        try:
            agent._start_heartbeat()
            time.sleep(2.5)
        finally:
            agent._stop_heartbeat()

        status = json.loads((agent._working_dir / ".status.json").read_text())
        assert status["runtime"]["state"] == "active"
        assert status["runtime"]["no_progress_seconds"] >= 30
        assert status["active_turn"]["kind"] == "tool_call"
        assert status["active_turn"]["id"] == "call_active"

    def test_watchdog_only_fires_once_per_stuck_episode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LINGTAI_ACTIVE_STUCK_THRESHOLD_S", "30")

        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._last_progress_at = time.time() - 120

        logged: list[dict] = []
        original_log = agent._log_service.log if agent._log_service else None

        def capture_log(entry):
            logged.append(entry)
            if original_log:
                original_log(entry)

        if agent._log_service:
            agent._log_service.log = capture_log

        agent._start_heartbeat()
        # Let the heartbeat tick several times.
        time.sleep(3.5)
        agent._stop_heartbeat()

        active_logs = [e for e in logged if e.get("type") == "active_without_progress"]
        assert len(active_logs) == 1, (
            f"watchdog fired {len(active_logs)} times; want exactly 1"
        )

    def test_watchdog_does_not_fire_when_idle(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LINGTAI_ACTIVE_STUCK_THRESHOLD_S", "30")

        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._last_progress_at = time.time() - 120  # rewind

        logged: list[dict] = []
        original_log = agent._log_service.log if agent._log_service else None

        def capture_log(entry):
            logged.append(entry)
            if original_log:
                original_log(entry)

        if agent._log_service:
            agent._log_service.log = capture_log

        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()

        kinds = [e.get("type") for e in logged]
        assert "active_without_progress" not in kinds
