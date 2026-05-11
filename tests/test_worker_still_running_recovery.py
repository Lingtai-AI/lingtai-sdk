"""Regression tests for WorkerStillRunningError fail-closed recovery."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
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


def _worker_error() -> WorkerStillRunningError:
    return WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")


def test_send_with_watchdog_keeps_llm_hang_for_worker_still_running(tmp_path, monkeypatch):
    agent = _FakeAgent(tmp_path)
    (tmp_path / ".llm_hang").write_text(json.dumps({"detected_at": 1}), encoding="utf-8")
    agent._session = SimpleNamespace(send=lambda content: (_ for _ in ()).throw(_worker_error()))

    monkeypatch.setattr(turn.threading, "Timer", lambda *a, **kw: SimpleNamespace(start=lambda: None, cancel=lambda: None, daemon=False))

    with pytest.raises(WorkerStillRunningError):
        turn._send_with_watchdog(agent, "hi")

    payload = json.loads((tmp_path / ".llm_hang").read_text(encoding="utf-8"))
    assert "worker_still_running_at" in payload
    assert "ChatInterface is unsafe" in payload["error"]


def test_send_with_watchdog_removes_llm_hang_for_ordinary_exception(tmp_path, monkeypatch):
    agent = _FakeAgent(tmp_path)
    (tmp_path / ".llm_hang").write_text(json.dumps({"detected_at": 1}), encoding="utf-8")
    agent._session = SimpleNamespace(send=lambda content: (_ for _ in ()).throw(TimeoutError("ordinary")))

    monkeypatch.setattr(turn.threading, "Timer", lambda *a, **kw: SimpleNamespace(start=lambda: None, cancel=lambda: None, daemon=False))

    with pytest.raises(TimeoutError):
        turn._send_with_watchdog(agent, "hi")

    assert not (tmp_path / ".llm_hang").exists()


def test_handle_worker_still_running_sets_asleep_and_signal(tmp_path):
    agent = _FakeAgent(tmp_path)

    turn._handle_worker_still_running(agent, _worker_error())

    assert agent._asleep.is_set()
    assert agent._states[-2:] == [AgentState.STUCK, AgentState.ASLEEP]
    assert (tmp_path / ".llm_hang").exists()
    assert any(name == "llm_worker_still_running" for name, _ in agent._logs)


def test_run_loop_skips_chat_history_save_after_worker_still_running(tmp_path, monkeypatch):
    agent = _FakeAgent(tmp_path)
    agent._shutdown = threading.Event()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent._config = SimpleNamespace(insights_interval=0)
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "go"))
    agent.saves = 0
    agent._save_chat_history = lambda: setattr(agent, "saves", agent.saves + 1)

    def fake_handle(_agent, _msg):
        raise _worker_error()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    def cancel_timer(_agent):
        _agent._shutdown.set()

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", cancel_timer)

    turn._run_loop(agent)

    assert agent.saves == 0
    assert any(name == "chat_history_save_skipped" for name, _ in agent._logs)
    assert (tmp_path / ".llm_hang").exists()


def test_asleep_wake_refuses_when_llm_hang_signal_exists(tmp_path, monkeypatch):
    import time as _time
    agent = _FakeAgent(tmp_path, _state=AgentState.ASLEEP)
    agent._shutdown = threading.Event()
    agent._asleep.set()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "wake?"))
    # Fresh sentinel — well within TTL so wake should still be refused.
    (tmp_path / ".llm_hang").write_text(
        json.dumps({"detected_at": _time.time()}), encoding="utf-8",
    )

    # Stop the loop after it refuses the wake and returns to the asleep wait.
    calls = {"n": 0}
    def cancel_timer(_agent):
        calls["n"] += 1
        if calls["n"] >= 2:
            _agent._shutdown.set()

    monkeypatch.setattr(turn, "_cancel_soul_timer", cancel_timer, raising=False)

    # The function imports _cancel_soul_timer locally; patch the source symbol.
    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", cancel_timer)

    turn._run_loop(agent)

    assert agent._asleep.is_set()
    assert any(name == "wake_refused_llm_hang" for name, _ in agent._logs)
    assert not any(new == AgentState.ACTIVE for new in agent._states)


# ---------------------------------------------------------------------------
# Issue #35 — .llm_hang sentinel TTL + recovery paths
# ---------------------------------------------------------------------------


def test_stale_llm_hang_signal_auto_clears_and_wakes(tmp_path, monkeypatch):
    """A sentinel older than the TTL is dropped at the wake-refusal site
    and the agent is allowed to wake. See issue #35."""
    import time as _time

    agent = _FakeAgent(tmp_path, _state=AgentState.ASLEEP)
    agent._shutdown = threading.Event()
    agent._asleep.set()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent._reset_uptime = lambda: None
    agent._save_chat_history = lambda *a, **kw: None
    agent._config = SimpleNamespace(insights_interval=0)
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "wake?"))
    # Sentinel detected well past the TTL — must be auto-cleared.
    stale = _time.time() - (turn._LLM_HANG_SENTINEL_TTL_SECONDS + 60)
    (tmp_path / ".llm_hang").write_text(
        json.dumps({"detected_at": stale}), encoding="utf-8",
    )

    # Make _handle_message a no-op so the loop completes one turn cleanly,
    # then shutdown.
    def fake_handle(_agent, _msg):
        _agent._shutdown.set()

    monkeypatch.setattr(turn, "_handle_message", fake_handle)

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", lambda _a: None)

    turn._run_loop(agent)

    assert not (tmp_path / ".llm_hang").exists()
    assert any(name == "llm_hang_cleared"
               and fields.get("reason") == "ttl_expired"
               for name, fields in agent._logs)
    # The agent transitioned to ACTIVE (woke up).
    assert any(s == AgentState.ACTIVE for s in agent._states)


def test_wake_refused_log_includes_ttl_remaining(tmp_path, monkeypatch):
    """When the sentinel is fresh, the wake-refusal log carries the
    remaining TTL so operators see actual recovery time. See issue #35."""
    import time as _time

    agent = _FakeAgent(tmp_path, _state=AgentState.ASLEEP)
    agent._shutdown = threading.Event()
    agent._asleep.set()
    agent._cancel_event = threading.Event()
    agent._inbox_timeout = 0.01
    agent.inbox = queue.Queue()
    agent.inbox.put(_make_message(MSG_REQUEST, "human", "wake?"))
    (tmp_path / ".llm_hang").write_text(
        json.dumps({"detected_at": _time.time()}), encoding="utf-8",
    )

    calls = {"n": 0}
    def cancel_timer(_agent):
        calls["n"] += 1
        if calls["n"] >= 2:
            _agent._shutdown.set()

    import lingtai_kernel.intrinsics.soul.flow as soul_flow
    monkeypatch.setattr(soul_flow, "_cancel_soul_timer", cancel_timer)

    turn._run_loop(agent)

    refusals = [fields for name, fields in agent._logs
                if name == "wake_refused_llm_hang"]
    assert refusals, "expected wake_refused_llm_hang log entry"
    assert "ttl_remaining_seconds" in refusals[0]
    # New sentinel — remaining should be close to the full TTL.
    assert refusals[0]["ttl_remaining_seconds"] >= 0
    assert refusals[0]["ttl_remaining_seconds"] <= int(turn._LLM_HANG_SENTINEL_TTL_SECONDS)


def test_send_with_watchdog_attaches_worker_done_callback(tmp_path, monkeypatch):
    """When the worker is still running, the watchdog registers a
    done-callback on the orphaned future so the sentinel clears as soon
    as the worker finally exits — without waiting out the TTL.
    See issue #35."""
    from concurrent.futures import Future

    agent = _FakeAgent(tmp_path)
    agent._session = SimpleNamespace(
        send=lambda content: (_ for _ in ()).throw(_worker_error_with_future()),
    )
    monkeypatch.setattr(
        turn.threading, "Timer",
        lambda *a, **kw: SimpleNamespace(start=lambda: None, cancel=lambda: None,
                                          daemon=False),
    )

    captured: dict = {}
    real_future = Future()
    captured["future"] = real_future

    def _err_factory():
        return WorkerStillRunningError(
            elapsed=300.0, grace=5.0, agent_name="test",
            future=real_future,
        )

    agent._session.send = lambda content: (_ for _ in ()).throw(_err_factory())

    with pytest.raises(WorkerStillRunningError):
        turn._send_with_watchdog(agent, "hi")

    assert (tmp_path / ".llm_hang").exists()  # sentinel still there
    # Worker finally exits — done callback should fire and remove sentinel.
    real_future.set_result(None)
    assert not (tmp_path / ".llm_hang").exists()
    assert any(name == "llm_hang_cleared"
               and fields.get("reason") == "worker_exited"
               for name, fields in agent._logs)


def _worker_error_with_future():  # pragma: no cover — overridden inline above
    return WorkerStillRunningError(elapsed=300.0, grace=5.0, agent_name="test")


def test_perform_refresh_clears_llm_hang_sentinel(tmp_path):
    """system(action='refresh') and TUI /refresh both flow through
    _perform_refresh, which must drop the sentinel before relaunching.
    See issue #35."""
    from lingtai_kernel.base_agent.lifecycle import _perform_refresh

    (tmp_path / ".llm_hang").write_text(
        json.dumps({"detected_at": 1}), encoding="utf-8",
    )

    agent = _FakeAgent(tmp_path)
    agent._save_chat_history = lambda: None
    # No launch cmd → refresh exits early but cleanup must still happen.
    agent._build_launch_cmd = lambda: None

    _perform_refresh(agent)

    assert not (tmp_path / ".llm_hang").exists()
    assert any(name == "llm_hang_cleared"
               and fields.get("reason") == "refresh"
               for name, fields in agent._logs)


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
