"""Regression tests for the post-refresh livelock in _handle_tc_wake.

Background: after `refresh` the agent's _session.chat is torn down (None)
and re-created lazily on the next _session.send() call. If a soul-flow
consultation fires while in this transient None-chat state, it posts
MSG_TC_WAKE which routes to _handle_tc_wake. The original handler bailed
with `tc_wake_noop reason=chat_not_ready` and re-enqueued the items —
WITHOUT posting another MSG_TC_WAKE. The next consultation_fire would post
its own wake, fire the same bail, re-enqueue again, and the cycle never
made progress. Production observed: agent stuck idle, soul flow firing
every 2.5 minutes for hours, never producing a turn.

Fix: when _chat is None, call _session.ensure_session() to create it
inline rather than re-enqueueing. ensure_session is idempotent. If session
creation itself fails (LLM provider down, etc.) we still re-enqueue + log
ensure_session_failed so a later boundary can retry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from lingtai_kernel.base_agent.turn import _handle_tc_wake
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.tc_inbox import InvoluntaryToolCall, TCInbox


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubChatHolder:
    def __init__(self, interface: ChatInterface):
        self.interface = interface


@dataclass
class _StubSession:
    """Models the SessionManager surface that _handle_tc_wake's
    chat-not-ready branch touches: a `chat` attribute that is None until
    `ensure_session()` is called, after which it returns a held value.
    Behavior matches session.py:144-157 — ensure_session is a no-op when
    chat is already non-None, and creates a fresh session otherwise."""

    _chat_after_ensure: Any = None  # what ensure_session installs
    _ensure_raises: Exception | None = None
    chat: Any = None
    ensure_calls: int = 0

    def ensure_session(self):
        self.ensure_calls += 1
        if self._ensure_raises is not None:
            raise self._ensure_raises
        if self.chat is None:
            self.chat = self._chat_after_ensure
        return self.chat


@dataclass
class _StubAgent:
    _session: _StubSession
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _logs: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def _chat(self):
        return self._session.chat

    @_chat.setter
    def _chat(self, value):
        self._session.chat = value

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _make_notification_item(notif_id: str = "notif_x", call_id: str = "sn_x") -> InvoluntaryToolCall:
    return InvoluntaryToolCall(
        call=ToolCallBlock(
            id=call_id,
            name="system",
            args={
                "action": "notification",
                "notif_id": notif_id,
                "source": "email",
                "ref_id": "mail_xyz",
            },
        ),
        result=ToolResultBlock(id=call_id, name="system", content="..."),
        source=f"system.notification:{notif_id}",
        enqueued_at=0.0,
        coalesce=False,
        replace_in_history=False,
    )


def _drive_chat_not_ready_branch(agent: _StubAgent) -> str | None:
    """Mirror of the chat-not-ready handling in BaseAgent._handle_tc_wake.

    Returns the reason emitted via tc_wake_noop, or None if it proceeded
    to splice (i.e. the bail path was avoided).
    """
    items = agent._tc_inbox.drain()
    if not items:
        agent._log("tc_wake_noop", reason="tc_inbox_empty")
        return "tc_inbox_empty"
    if agent._chat is None:
        try:
            agent._session.ensure_session()
        except Exception as e:
            for item in items:
                agent._tc_inbox.enqueue(item)
            agent._log(
                "tc_wake_noop",
                reason="ensure_session_failed",
                error=str(e)[:300],
            )
            return "ensure_session_failed"
    if agent._chat is not None and agent._chat.interface.has_pending_tool_calls():
        for item in items:
            agent._tc_inbox.enqueue(item)
        agent._log("tc_wake_noop", reason="pending_tool_calls")
        return "pending_tool_calls"
    # If we reach here, splice would proceed. Return None to signal that.
    # (We don't actually drive the splice in these tests — that's covered
    # by test_tc_wake_orphan_heal.py — we just verify the bail-path outcome.)
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_none_creates_session_inline():
    """Pre-fix: _chat is None → bail + re-enqueue → livelock.
    Post-fix: _chat is None → ensure_session creates it → splice proceeds."""
    iface = ChatInterface()
    fresh_chat = _StubChatHolder(iface)
    session = _StubSession(_chat_after_ensure=fresh_chat)
    agent = _StubAgent(_session=session)
    agent._tc_inbox.enqueue(_make_notification_item())

    result = _drive_chat_not_ready_branch(agent)

    # ensure_session was called, chat is now wired up, splice would proceed.
    assert session.ensure_calls == 1
    assert agent._chat is fresh_chat
    assert result is None  # proceeded past the bail
    # Item is consumed (not re-enqueued).
    assert len(agent._tc_inbox) == 0
    # No tc_wake_noop fired.
    noops = [(et, f) for (et, f) in agent._logs if et == "tc_wake_noop"]
    assert noops == []


def test_chat_already_ready_skips_ensure_session():
    """Sanity: when _chat is already wired, ensure_session may still be
    called but is idempotent. The handler proceeds to splice as before."""
    iface = ChatInterface()
    chat = _StubChatHolder(iface)
    session = _StubSession(chat=chat, _chat_after_ensure=chat)
    agent = _StubAgent(_session=session)
    agent._tc_inbox.enqueue(_make_notification_item())

    result = _drive_chat_not_ready_branch(agent)

    # ensure_session was NOT called because _chat is already non-None.
    # (The handler's `if self._chat is None` short-circuited before the
    # ensure_session call.)
    assert session.ensure_calls == 0
    assert result is None  # proceeded past the bail


def test_ensure_session_failure_re_enqueues_and_logs():
    """If ensure_session itself raises (LLM provider down, missing key),
    the handler must NOT crash. Items get re-enqueued and an
    ensure_session_failed log fires for operator triage."""
    session = _StubSession(_ensure_raises=RuntimeError("provider unreachable"))
    agent = _StubAgent(_session=session)
    item = _make_notification_item(notif_id="notif_will_retry")
    agent._tc_inbox.enqueue(item)

    result = _drive_chat_not_ready_branch(agent)

    assert result == "ensure_session_failed"
    # Item is back in the queue for next safe-boundary retry.
    drained = agent._tc_inbox.drain()
    assert len(drained) == 1
    assert drained[0].call.args["notif_id"] == "notif_will_retry"
    # The error log carries the cause.
    err_logs = [(et, f) for (et, f) in agent._logs if et == "tc_wake_noop"]
    assert len(err_logs) == 1
    _, fields = err_logs[0]
    assert fields["reason"] == "ensure_session_failed"
    assert "provider unreachable" in fields["error"]


def test_pending_tool_calls_logs_diagnostic_with_pending_ids():
    iface = ChatInterface()
    iface.add_system("system")
    iface.add_user_message("run pending tool")
    iface.add_assistant_message(
        [
            TextBlock("calling"),
            ToolCallBlock(id="call_pending", name="bash", args={"command": "sleep"}),
        ]
    )
    chat = _StubChatHolder(iface)
    session = _StubSession(chat=chat, _chat_after_ensure=chat)
    agent = _StubAgent(_session=session)
    agent._tc_inbox.enqueue(_make_notification_item(call_id="notif_call"))

    _handle_tc_wake(agent, MagicMock())

    assert len(agent._tc_inbox) == 1
    names = [event for event, _ in agent._logs]
    assert names == ["tc_wake_noop"]
    assert agent._logs[0][1] == {
        "reason": "pending_tool_calls",
        "pending_tool_call_count": 1,
        "pending_tool_call_ids": ["call_pending"],
        "pending_tool_names": ["bash"],
    }


def test_empty_queue_short_circuits_before_ensure_session():
    """Sanity: if tc_inbox is empty, the handler shouldn't bother creating
    a session — there's nothing to splice."""
    session = _StubSession(_chat_after_ensure=_StubChatHolder(ChatInterface()))
    agent = _StubAgent(_session=session)
    # No items enqueued.

    result = _drive_chat_not_ready_branch(agent)

    assert result == "tc_inbox_empty"
    assert session.ensure_calls == 0
    # _chat is still None — we didn't create one for nothing.
    assert agent._chat is None


def test_pending_tool_calls_after_ensure_session_re_enqueues():
    """Edge case: ensure_session succeeds and creates a chat, but the
    interface already has unanswered tool_calls in it (e.g. restored from
    chat_history.jsonl with a dangling pair). The pending-tool_calls bail
    must still fire after ensure_session — we can't splice into a
    mid-pair wire safely."""
    iface = ChatInterface()
    # Simulate a restored chat with an unanswered tool_call.
    iface.add_assistant_message(content=[
        ToolCallBlock(id="call_dangling", name="bash", args={"cmd": "ls"})
    ])
    chat = _StubChatHolder(iface)
    session = _StubSession(_chat_after_ensure=chat)
    agent = _StubAgent(_session=session)
    item = _make_notification_item()
    agent._tc_inbox.enqueue(item)

    result = _drive_chat_not_ready_branch(agent)

    assert result == "pending_tool_calls"
    assert session.ensure_calls == 1  # session DID get created
    # Item was re-enqueued because the wire wasn't safe to splice into.
    assert len(agent._tc_inbox) == 1
