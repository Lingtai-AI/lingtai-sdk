"""Tests for heartbeat — always-on agent health monitor with AED timeout."""
import time
from unittest.mock import MagicMock


def make_mock_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


class TestHeartbeatInit:

    def test_heartbeat_counter_initialized(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        assert agent._heartbeat == 0.0
        assert agent._heartbeat_thread is None
        assert agent._aed_start is None

    def test_heartbeat_attribute_present(self, tmp_path):
        """The agent carries a ``_heartbeat`` float attribute. The
        live-runtime ``status()`` no longer surfaces it directly — the
        canonical liveness signal is the ``.agent.heartbeat`` file on
        disk (consumed by ``handshake.is_alive``)."""
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._heartbeat = 1234567890.123
        assert isinstance(agent._heartbeat, float)
        assert agent._heartbeat == 1234567890.123


class TestHeartbeatBeating:

    def test_heartbeat_increments(self, tmp_path):
        from lingtai_kernel import BaseAgent
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._start_heartbeat()
        time.sleep(2.5)
        agent._stop_heartbeat()
        assert agent._heartbeat > 0
        assert time.time() - agent._heartbeat < 2.0

    def test_no_aed_on_idle(self, tmp_path):
        """Heartbeat does NOT set _aed_start when agent is IDLE."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE)

        time.sleep(2.0)
        agent._stop_heartbeat()
        assert agent._aed_start is None


class TestHeartbeatFile:

    def test_heartbeat_writes_file(self, tmp_path):
        """Heartbeat file exists while running, deleted after stop."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        time.sleep(1.5)
        assert hb_file.exists()
        agent._stop_heartbeat()
        assert not hb_file.exists()

    def test_heartbeat_file_written_while_running(self, tmp_path):
        """While ACTIVE, heartbeat file exists with a fresh timestamp."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)

        assert hb_file.exists()
        ts = float(hb_file.read_text())
        assert time.time() - ts < 2.0

        agent._stop_heartbeat()

    def test_heartbeat_file_alive_when_asleep(self, tmp_path):
        """ASLEEP is a living sleep — heartbeat keeps ticking."""
        from lingtai_kernel import BaseAgent, AgentState
        from lingtai_kernel.config import AgentConfig
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
            config=AgentConfig(aed_timeout=1.0),  # very short timeout
        )
        hb_file = agent._working_dir / ".agent.heartbeat"
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        time.sleep(1.5)
        assert hb_file.exists()

        # Simulate STUCK — heartbeat will enforce aed_timeout → ASLEEP
        agent._set_state(AgentState.STUCK)
        time.sleep(3.0)  # wait for aed_timeout (1s) to elapse

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        # Heartbeat keeps ticking in ASLEEP (living sleep) — file is fresh
        if hb_file.exists():
            ts = float(hb_file.read_text())
            assert time.time() - ts < 2.0  # still fresh
        agent._stop_heartbeat()


class TestHeartbeatAEDTimeout:
    """Heartbeat enforces aed_timeout as a safety net — forces ASLEEP if STUCK too long."""

    def test_aed_timeout_triggers_asleep(self, tmp_path):
        """After aed_timeout in STUCK, agent goes ASLEEP."""
        from lingtai_kernel import BaseAgent, AgentState
        from lingtai_kernel.config import AgentConfig
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
            config=AgentConfig(aed_timeout=1.0),  # 1 second timeout
        )
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.STUCK)

        agent._start_heartbeat()
        time.sleep(3.0)  # wait for aed_timeout to elapse
        agent._stop_heartbeat()

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()

    def test_aed_start_resets_on_recovery(self, tmp_path):
        """When agent recovers from STUCK, _aed_start resets."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._aed_start = time.monotonic()

        # Simulate recovery
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")
        agent._set_state(AgentState.IDLE)

        time.sleep(1.5)
        agent._stop_heartbeat()

        assert agent._aed_start is None

    def test_asleep_state_in_status(self, tmp_path):
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._state = AgentState.ASLEEP
        status = agent.status()
        # State now lives under the "runtime" sub-dict (status() was reshaped
        # to group identity / runtime / tokens cleanly for the TUI).
        assert status["runtime"]["state"] == "asleep"


class TestSleepFile:

    def test_sleep_file_triggers_asleep_not_shutdown(self, tmp_path):
        """When .sleep is detected, agent goes ASLEEP and _asleep is set, _shutdown is NOT set."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Write .sleep file for heartbeat to detect
        (agent._working_dir / ".sleep").write_text("")
        time.sleep(2.0)
        agent._stop_heartbeat()

        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()


class TestSuspendFile:

    def test_suspend_file_triggers_shutdown(self, tmp_path):
        """When .suspend is detected, agent goes SUSPENDED and _shutdown IS set."""
        from lingtai_kernel import BaseAgent, AgentState
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._start_heartbeat()
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Write .suspend file for heartbeat to detect
        (agent._working_dir / ".suspend").write_text("")
        time.sleep(2.0)
        agent._stop_heartbeat()

        assert agent._state == AgentState.SUSPENDED
        assert agent._shutdown.is_set()


class TestSelfSleep:

    def test_self_sleep_no_karma_required(self, tmp_path):
        """Any agent can self-sleep to ASLEEP without admin.karma."""
        from lingtai_kernel import BaseAgent, AgentState
        from lingtai_kernel.intrinsics.system import handle
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")

        # Self-sleep: action=sleep with no address
        result = handle(agent, {"action": "sleep"})

        assert result["status"] == "ok"
        assert agent._state == AgentState.ASLEEP
        assert agent._asleep.is_set()
        assert not agent._shutdown.is_set()
