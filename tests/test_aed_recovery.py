"""Regression tests for AED recovery paths: WorkerStillRunningError fail-closed
handling in the run loop, plus transient provider-error retry budget.

The previous `.llm_hang` watchdog/sentinel system was removed; this file replaces
`test_worker_still_running_recovery.py`. The remaining safety property is that
when `WorkerStillRunningError` raises out of `_handle_message`, the run loop
puts the agent ASLEEP without saving chat history (the worker may still be
mutating ChatInterface) — no filesystem sentinel involved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import threading
from types import SimpleNamespace

import pytest

from lingtai_kernel.base_agent import turn
from lingtai_kernel.llm_utils import WorkerStillRunningError
from lingtai_kernel.message import _make_message, MSG_REQUEST
from lingtai_kernel.state import AgentState


@dataclass
class _FakeAgent:
    _working_dir: object
    _state: AgentState = AgentState.ACTIVE
    _asleep: threading.Event = field(default_factory=threading.Event)
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _states: list[AgentState] = field(default_factory=list)
    # ``_chat`` is read by ``_run_loop`` when ``_asleep`` is set (to heal
    # dangling tool_calls before sleeping). Default to None — fake agents
    # in this suite never have a live chat session.
    _chat: object = None

    def _log(self, event_type: str, **fields):
        self._logs.append((event_type, fields))

    def _set_state(self, new_state: AgentState, reason: str = ""):
        self._state = new_state
        self._states.append(new_state)
        self._log("agent_state", new=new_state.value, reason=reason)


# ---------------------------------------------------------------------------
# WorkerStillRunningError fail-closed handling in the AED loop
# ---------------------------------------------------------------------------


def test_run_loop_skips_chat_history_save_after_worker_still_running(tmp_path, monkeypatch):
    """When _handle_message raises WorkerStillRunningError, the AED loop
    puts the agent ASLEEP with skip_post_turn_save=True so the in-process
    ChatInterface is not mutated while the worker future is still alive.
    No sentinel file is written."""
    agent = _make_run_loop_agent(tmp_path)
    agent.saves = 0
    agent._save_chat_history = lambda *a, **kw: setattr(agent, "saves", agent.saves + 1)

    def fake_handle(_agent, _msg):
        raise WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert agent.saves == 0
    assert any(name == "chat_history_save_skipped" for name, _ in agent._logs)
    assert any(name == "llm_worker_still_running" for name, _ in agent._logs)
    assert agent._asleep.is_set()
    # Both STUCK and ASLEEP must be written to .agent.json so the TUI's
    # state read is accurate and the heartbeat AED timeout doesn't see a
    # bare STUCK agent (which would trigger redundant recovery).
    assert AgentState.STUCK in agent._states
    assert AgentState.ASLEEP in agent._states
    assert not (tmp_path / ".llm_hang").exists()


def test_run_loop_publishes_system_notification_on_worker_still_running(tmp_path, monkeypatch):
    """When WorkerStillRunningError fires, the run loop publishes an operator-
    visible system notification so the hung-worker condition is surfaced
    beyond the log event and operators don't have to grep events.jsonl."""
    import json
    agent = _make_run_loop_agent(tmp_path)

    def fake_handle(_agent, _msg):
        exc = WorkerStillRunningError(elapsed=305.0, grace=5.0, agent_name="test")
        exc.predecessor_tool_count = 1
        exc.predecessor_tool_names = ["bash"]
        exc.predecessor_payload_chars = 42
        exc.ledger_source = "main"
        raise exc

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    notif_path = tmp_path / ".notification" / "system.json"
    assert notif_path.exists(), "Expected .notification/system.json to be written after WorkerStillRunningError"
    notif = json.loads(notif_path.read_text(encoding="utf-8"))
    events = notif.get("data", {}).get("events", [])
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "kernel.llm_worker_hang"
    assert "305" in ev["ref_id"] or "305" in ev["body"]
    assert "Previous tools: bash (1)" in ev["body"]
    assert "~42 chars" in ev["body"]
    assert "ASLEEP" in ev["body"]
    assert "/refresh" in ev["body"]
    # Log event must also be present, with the same predecessor context.
    worker_logs = [
        fields for name, fields in agent._logs
        if name == "llm_worker_still_running"
    ]
    assert worker_logs
    assert worker_logs[0]["predecessor_tool_names"] == ["bash"]
    assert worker_logs[0]["predecessor_tool_count"] == 1
    assert worker_logs[0]["predecessor_payload_chars"] == 42
    assert worker_logs[0]["ledger_source"] == "main"


# ---------------------------------------------------------------------------
# AED transient provider retry
# ---------------------------------------------------------------------------


class _FakeInterface:
    def __init__(self):
        self.heals: list[tuple[str, bool]] = []

    def has_pending_tool_calls(self):
        return False

    def close_pending_tool_calls(self, *, reason: str, tool_completed: bool = False):
        self.heals.append((reason, tool_completed))


def _make_run_loop_agent(tmp_path):
    agent = _FakeAgent(tmp_path)
    agent.agent_name = "test"
    agent._shutdown = threading.Event()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent._reset_uptime = lambda: None
    agent._save_chat_history = lambda *a, **kw: None
    agent._config = SimpleNamespace(
        insights_interval=0,
        max_aed_attempts=10,
        language="en",
        time_awareness=True,
        timezone_awareness=True,
    )
    iface = _FakeInterface()
    agent._session = SimpleNamespace(
        chat=SimpleNamespace(interface=iface),
        _rebuild_session=lambda interface: setattr(agent, "rebuilds", getattr(agent, "rebuilds", 0) + 1),
    )
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "go"))
    agent._preset_fallback_attempted = False
    agent._can_fallback_preset = lambda: False
    # Required by _enqueue_system_notification when WorkerStillRunningError fires.
    agent._system_notification_lock = threading.Lock()
    agent._wake_nap = lambda _reason: None
    return agent


def test_transient_provider_error_retries_before_aed_count(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("An error occurred while processing your request")
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert calls["n"] == 3
    assert [name for name, _ in agent._logs].count("aed_transient_retry") == 2
    assert not any(name == "aed_attempt" for name, _ in agent._logs)
    assert getattr(agent, "rebuilds", 0) == 0
    assert all(tool_completed for _, tool_completed in agent._session.chat.interface.heals)


def test_transient_provider_error_counts_as_aed_after_retry_budget(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1
    calls = {"n": 0}

    def fake_handle(_agent, _msg):
        calls["n"] += 1
        raise RuntimeError("peer closed connection without sending complete message body")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)
    monkeypatch.setattr(turn.time, "sleep", lambda _seconds: None)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert calls["n"] == turn._TRANSIENT_AED_RETRY_LIMIT + 1
    assert [name for name, _ in agent._logs].count("aed_transient_retry") == turn._TRANSIENT_AED_RETRY_LIMIT
    assert any(name == "aed_transient_exhausted" for name, _ in agent._logs)
    assert any(name == "aed_attempt" and fields["attempt"] == 1 for name, fields in agent._logs)
    assert any(name == "aed_exhausted" for name, _ in agent._logs)
    assert agent._asleep.is_set()


def test_structural_error_skips_transient_retry(tmp_path, monkeypatch):
    agent = _make_run_loop_agent(tmp_path)
    agent._config.max_aed_attempts = 1

    def fake_handle(_agent, _msg):
        raise ValueError("bad schema")

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: _a._shutdown.set())

    turn._run_loop(agent)

    assert not any(name == "aed_transient_retry" for name, _ in agent._logs)
    assert any(name == "aed_attempt" and fields["attempt"] == 1 for name, fields in agent._logs)


def test_empty_llm_response_is_classified_transient():
    err = turn.EmptyLLMResponseError(ledger_source="main", in_tool_loop=False)
    assert turn._is_transient_provider_error(err) is True


def test_status_code_classifier_treats_only_5xx_as_transient():
    class StatusError(Exception):
        def __init__(self, status_code: int):
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    assert turn._is_transient_provider_error(StatusError(503)) is True
    assert turn._is_transient_provider_error(StatusError(429)) is False
    assert turn._is_transient_provider_error(StatusError(400)) is False
