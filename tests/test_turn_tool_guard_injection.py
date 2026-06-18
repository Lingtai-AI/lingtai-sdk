"""Stage 16 — ToolExecutor guard injection plumbing.

The turn loop builds a fresh ``ToolExecutor`` at two sites (``_handle_request``
and ``_handle_tc_wake``).  Both must thread the agent's own
``_tool_call_guard`` into the executor so a real guard chain stored on the
agent is actually consulted before tool dispatch.  Default (empty) guard must
preserve the pre-existing ``default_allow`` pass-through behavior.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import lingtai_kernel.base_agent.turn as turn_module
from lingtai_kernel.base_agent import BaseAgent
from lingtai_kernel.base_agent.turn import _handle_request, _handle_tc_wake
from lingtai_kernel.message import _make_message, MSG_REQUEST
from lingtai_kernel.tool_call_guard import GuardDecision, ToolCallGuard


def _make_service():
    svc = MagicMock()
    svc.model = "test-model"
    svc.make_tool_result.return_value = {"role": "tool", "content": "ok"}
    return svc


def _no_tool_response():
    resp = MagicMock()
    resp.text = "done"
    resp.tool_calls = []
    resp.usage = MagicMock(
        input_tokens=0, output_tokens=0, thinking_tokens=0, cached_tokens=0
    )
    return resp


def test_default_agent_guard_is_empty_pass_through(tmp_path):
    """A freshly constructed agent owns a default-empty ToolCallGuard whose
    decision is the unchanged ``default_allow`` pass-through."""
    agent = BaseAgent(service=_make_service(), working_dir=tmp_path / "a")
    guard = agent._tool_call_guard
    assert isinstance(guard, ToolCallGuard)
    decision = guard.evaluate(MagicMock(tool_name="read", tool_args={}))
    assert decision.check_name == "default_allow"
    assert decision.approval_mode == "pass_through"
    assert decision.allowed is True


def test_handle_request_executor_uses_agent_guard(tmp_path, monkeypatch):
    """``_handle_request`` must build its ToolExecutor with the agent's guard."""
    agent = BaseAgent(service=_make_service(), working_dir=tmp_path / "b")

    def deny_read(proposal):
        if proposal.tool_name == "read":
            return GuardDecision.deny(check_name="deny_read", reason="blocked")
        return None

    agent._tool_call_guard = ToolCallGuard([deny_read])

    # Stub collaborators invoked after executor construction so we can inspect
    # the executor without driving a real LLM round-trip.
    monkeypatch.setattr(turn_module, "_check_molt_pressure", lambda agent: None)
    monkeypatch.setattr(turn_module, "_process_response", lambda agent, response, **kw: None)
    agent._pre_request = MagicMock(return_value="hi")
    agent._sync_notifications = MagicMock()
    agent._session = MagicMock()
    agent._session.send.return_value = _no_tool_response()
    agent._save_chat_history = MagicMock()
    agent._post_request = MagicMock()

    _handle_request(agent, _make_message(MSG_REQUEST, "user", "go"))

    assert agent._executor._tool_call_guard is agent._tool_call_guard


def test_handle_tc_wake_executor_uses_agent_guard(tmp_path, monkeypatch):
    """``_handle_tc_wake`` must build its ToolExecutor with the agent's guard."""
    agent = BaseAgent(service=_make_service(), working_dir=tmp_path / "c")
    agent._tool_call_guard = ToolCallGuard([lambda p: None])
    monkeypatch.setattr(turn_module, "_process_response", lambda agent, response, **kw: None)

    # Minimal chat/interface surface: chat ready (``_chat`` proxies to
    # ``_session.chat``), no pending tool calls, empty legacy tc_inbox so the
    # handler reaches the executor build then the wire-drive path.
    iface = MagicMock()
    iface.has_pending_tool_calls.return_value = False
    chat = MagicMock()
    chat.interface = iface
    agent._session = MagicMock()
    agent._session.chat = chat
    agent._session.send.return_value = _no_tool_response()
    agent._tc_inbox = MagicMock()
    agent._tc_inbox.drain.return_value = []
    agent._save_chat_history = MagicMock()

    _handle_tc_wake(agent, MagicMock())

    assert agent._executor._tool_call_guard is agent._tool_call_guard
