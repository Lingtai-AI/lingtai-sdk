"""Tests for karma/nirvana lifecycle control via system intrinsic."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.state import AgentState


def _make_agent(tmp_path, **kwargs):
    """Create a minimal BaseAgent for testing."""
    svc = MagicMock()
    svc.create_session.return_value = MagicMock()
    kwargs.setdefault("working_dir", str(tmp_path / "test000000ab"))
    agent = BaseAgent(svc, **kwargs)
    return agent


class TestSignalFiles:
    """Signal file detection in heartbeat loop."""

    def test_interrupt_signal_sets_cancel_event(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.start()
        try:
            # Write .interrupt signal file
            (agent.working_dir / ".interrupt").write_text("")
            # Wait for heartbeat to detect it
            time.sleep(2.0)
            assert agent._cancel_event.is_set()
            assert not (agent.working_dir / ".interrupt").exists(), "signal file should be deleted"
        finally:
            agent.stop()

    def test_sleep_signal_sets_asleep(self, tmp_path):
        agent = _make_agent(tmp_path)
        agent.start()
        # Write .sleep signal file
        (agent.working_dir / ".sleep").write_text("")
        # Wait for agent to detect it
        time.sleep(3.0)
        assert agent._asleep.is_set()
        assert agent.state == AgentState.ASLEEP
        assert not (agent.working_dir / ".sleep").exists(), "signal file should be deleted"


class TestSystemIntrinsicKarma:
    """Karma actions in system intrinsic."""

    def test_interrupt_requires_karma_admin(self, tmp_path):
        agent = _make_agent(tmp_path, admin={})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "interrupt", "address": "/some/path"})
        assert "error" in result

    def test_interrupt_with_karma_admin(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / ".agent.json").write_text('{"agent_id": "t1"}')
        (target_dir / ".agent.heartbeat").write_text(str(time.time()))

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "interrupt", "address": str(target_dir)})
        assert result["status"] == "interrupted"
        assert (target_dir / ".interrupt").is_file()

    def test_lull_writes_signal_file(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / ".agent.json").write_text('{"agent_id": "t1"}')
        (target_dir / ".agent.heartbeat").write_text(str(time.time()))

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "lull", "address": str(target_dir)})
        assert result["status"] == "asleep"
        assert (target_dir / ".sleep").is_file()

    def test_lull_rejects_asleep_target(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        # admin must be non-null — without it, is_human() returns True and
        # is_alive() short-circuits to always-alive, defeating the
        # not-running rejection path.
        (target_dir / ".agent.json").write_text('{"agent_id": "t1", "admin": {}}')

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "lull", "address": str(target_dir)})
        assert "error" in result

    def test_interrupt_self_rejected(self, tmp_path):
        agent = _make_agent(tmp_path, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "interrupt", "address": str(agent.working_dir)})
        assert "error" in result

    def test_nirvana_requires_nirvana_admin(self, tmp_path):
        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "nirvana", "address": "/some/path"})
        assert "error" in result

    def test_nirvana_with_nirvana_admin(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        # admin={} so is_human returns False; no heartbeat file → is_alive returns False
        (target_dir / ".agent.json").write_text('{"agent_id": "t1", "admin": {}}')

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True, "nirvana": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "nirvana", "address": str(target_dir)})
        assert result["status"] == "nirvana"
        assert not target_dir.exists()

    def test_nirvana_self_rejected(self, tmp_path):
        agent = _make_agent(tmp_path, admin={"karma": True, "nirvana": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "nirvana", "address": str(agent.working_dir)})
        assert "error" in result

    def test_cpr_rejects_alive_target(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        (target_dir / ".agent.json").write_text('{"agent_id": "t1"}')
        (target_dir / ".agent.heartbeat").write_text(str(time.time()))

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "cpr", "address": str(target_dir)})
        assert "error" in result
        assert "already running" in result["message"]

    def test_cpr_without_handler_returns_error(self, tmp_path):
        target_dir = tmp_path / "target"
        target_dir.mkdir()
        # admin={} so is_human returns False (admin is not None)
        (target_dir / ".agent.json").write_text('{"agent_id": "t1", "admin": {}}')

        sender_base = tmp_path / "sender"
        sender_base.mkdir()
        agent = _make_agent(sender_base, admin={"karma": True})
        from lingtai_kernel.intrinsics.system import handle
        result = handle(agent, {"action": "cpr", "address": str(target_dir)})
        assert "error" in result
        assert "not supported" in result["message"].lower()


class TestCPRLingtai:
    """CPR via lingtai Agent (full reconstruction)."""

    def test_cpr_reconstructs_agent(self, tmp_path):
        from lingtai.agent import Agent

        svc = MagicMock()
        svc.create_session.return_value = MagicMock()
        svc.provider = "mock"
        svc.model = "test-model"
        svc._base_url = None

        # Create an agent — this should persist LLM config
        agent = Agent(svc, working_dir=tmp_path / "alice000001",
                      agent_name="alice", admin={"karma": True})

        # Verify LLM config was persisted to working dir
        import json
        llm_config_path = agent.working_dir / "system" / "llm.json"
        assert llm_config_path.is_file()
        llm_config = json.loads(llm_config_path.read_text())
        assert llm_config["provider"] == "mock"
        assert llm_config["model"] == "test-model"

    def test_cpr_agent_hook_returns_truthy(self, tmp_path):
        """_cpr_agent now launches the target as a detached subprocess and
        returns a truthy sentinel (not a reconstructed Agent). The test
        validates the hook fires and yields a non-None signal — actual
        process spawning is mocked out so no real lingtai child is forked."""
        import json as _json
        from lingtai.agent import Agent
        from unittest.mock import patch

        svc = MagicMock()
        svc.create_session.return_value = MagicMock()
        svc.provider = "mock"
        svc.model = "test-model"
        svc._base_url = None

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        target = Agent(svc, working_dir=agents_dir / "bobbob000001",
                       agent_name="bob")
        target_dir = str(target.working_dir)

        # _cpr_agent reads init.json before launching; Agent.__init__ doesn't
        # persist it, so the test stages a minimal one.
        (target.working_dir / "init.json").write_text(_json.dumps({
            "manifest": {
                "agent_name": "bob",
                "llm": {"provider": "mock", "model": "test-model"},
            },
        }))

        reviver_dir = tmp_path / "reviver"
        reviver_dir.mkdir()
        reviver = Agent(svc, working_dir=reviver_dir / "admin000001",
                        agent_name="admin", admin={"karma": True})

        target._workdir.release_lock()

        # Stub Popen so no real subprocess is forked. Also short-circuit
        # the venv resolver, which otherwise probes the runtime via
        # subprocess.run (no real venv available in the test env).
        # _cpr_agent imports these inside the function body, so the patch
        # targets the source module rather than lingtai.agent.
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        from pathlib import Path as _Path
        with patch("subprocess.Popen", return_value=fake_proc), \
             patch("lingtai.venv_resolve.resolve_venv",
                   return_value=_Path("/fake/venv")), \
             patch("lingtai.venv_resolve.venv_python",
                   return_value="/fake/venv/bin/python"):
            resuscitated = reviver._cpr_agent(target_dir)

        assert resuscitated is not None
        assert resuscitated  # truthy sentinel


class TestSelfSleepPendingNotificationsGuard:
    """Regression: system(sleep) must not transition to ASLEEP while
    `.notification/` has unprocessed payloads on disk.

    Reported as lingtai-kernel#112 by @TZZheng: mail arriving during an
    ACTIVE turn that already decided to sleep was deferred (correct),
    but `system(sleep)` then transitioned the agent to ASLEEP without
    re-checking the queue, leaving the first email unprocessed until a
    second email arrived to wake the agent.
    """

    def _write_notification(self, agent, name, payload):
        import json
        notif_dir = agent.working_dir / ".notification"
        notif_dir.mkdir(parents=True, exist_ok=True)
        path = notif_dir / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_sleep_refused_when_notification_pending(self, tmp_path):
        from lingtai_kernel.intrinsics.system import handle
        agent = _make_agent(tmp_path)
        # Simulate the pre-turn baseline: no notifications observed yet.
        agent._notification_fp = ()

        self._write_notification(agent, "email.json", {
            "header": "human mail",
            "icon": "📧",
            "priority": "normal",
            "data": {"count": 1},
        })

        result = handle(agent, {"action": "sleep", "reason": "test"})

        assert result.get("status") == "ok"
        # Refusal message, not the sleep confirmation
        assert "refused" in result.get("message", "").lower()
        # State must NOT have transitioned
        assert agent.state != AgentState.ASLEEP
        assert not agent._asleep.is_set()

    def test_sleep_refused_when_notification_arrived_mid_turn(self, tmp_path):
        """The exact race from kernel#112: agent observed an EMPTY queue
        at the start of the turn, mail arrived during the LLM call, and
        the agent then calls system(sleep). _notification_fp is still ()
        from the pre-turn baseline; the on-disk fp is non-empty."""
        from lingtai_kernel.intrinsics.system import handle
        agent = _make_agent(tmp_path)
        agent._notification_fp = ()  # baseline: queue was empty

        # Mail arrives MID-TURN
        self._write_notification(agent, "email.json", {
            "header": "human mail", "icon": "📧",
            "priority": "normal", "data": {"count": 1},
        })

        result = handle(agent, {"action": "sleep", "reason": "no unread mail"})

        assert agent.state != AgentState.ASLEEP, (
            "kernel#112 regression: agent must not sleep with mail waiting"
        )
        assert "refused" in result.get("message", "").lower()

    def test_sleep_force_true_overrides_pending_guard(self, tmp_path):
        from lingtai_kernel.intrinsics.system import handle
        agent = _make_agent(tmp_path)
        agent._notification_fp = ()
        self._write_notification(agent, "email.json", {
            "header": "mail", "icon": "📧",
            "priority": "normal", "data": {},
        })

        result = handle(agent, {
            "action": "sleep", "reason": "really tired", "force": True,
        })

        assert result.get("status") == "ok"
        assert agent.state == AgentState.ASLEEP
        assert agent._asleep.is_set()

    def test_sleep_proceeds_when_queue_empty(self, tmp_path):
        """No notifications on disk — sleep should behave as before."""
        from lingtai_kernel.intrinsics.system import handle
        agent = _make_agent(tmp_path)
        agent._notification_fp = ()

        result = handle(agent, {"action": "sleep", "reason": "idle"})

        assert result.get("status") == "ok"
        assert agent.state == AgentState.ASLEEP
        assert agent._asleep.is_set()

    def test_sleep_proceeds_when_fingerprint_already_committed(self, tmp_path):
        """A notification on disk whose fingerprint matches the agent's
        last-committed fingerprint = already processed. Sleep is fine."""
        from lingtai_kernel.intrinsics.system import handle
        from lingtai_kernel.notifications import notification_fingerprint
        agent = _make_agent(tmp_path)

        self._write_notification(agent, "email.json", {
            "header": "old mail", "icon": "📧",
            "priority": "normal", "data": {},
        })
        # Pretend the notification heartbeat has already injected + committed
        agent._notification_fp = notification_fingerprint(agent.working_dir)

        result = handle(agent, {"action": "sleep", "reason": "all caught up"})

        assert result.get("status") == "ok"
        assert agent.state == AgentState.ASLEEP
