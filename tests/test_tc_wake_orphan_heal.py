"""Regression tests for _handle_tc_wake's orphan-tool_call heal path.

Background: _handle_tc_wake splices a synthetic (assistant{tool_call}, ...)
pair into the wire chat by:

    1. iface.add_assistant_message(content=[item.call])    # in-memory write
    2. self._save_chat_history()                           # FLUSH to disk
    3. response = self._session.send([item.result])        # may raise

If step 3 raises, the assistant tool_call is on disk with NO matching
tool_result — an orphan. Production saw this when the agent transitioned
asleep→active via tc_wake but the LLM call timed out before producing the
tool_result, leaving chat_history.jsonl in a chat-completions invariant
violation that bricks the agent on the next dispatch.

The fix wraps step 3 in try/except. On failure the handler:
  - calls iface.close_pending_tool_calls(reason) to synthesize a placeholder
    result for the orphan (same pattern AED already uses)
  - persists the healed chat
  - re-enqueues any unprocessed remaining items
  - re-raises so AED sees the real error rather than the outer except
    swallowing it

These tests exercise that path with a stub session whose .send() raises.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lingtai_kernel.llm.interface import (
    ChatInterface,
    ToolCallBlock,
    ToolResultBlock,
)
from lingtai_kernel.tc_inbox import InvoluntaryToolCall, TCInbox


# ---------------------------------------------------------------------------
# Stubs — minimal subset of BaseAgent surface that _handle_tc_wake reads.
# ---------------------------------------------------------------------------


class _StubChatHolder:
    """Mimics what `agent._chat` returns. The real BaseAgent wraps the
    interface in a session-aware object; the handler only ever reaches in
    via `.interface`, so we expose just that."""

    def __init__(self, interface: ChatInterface):
        self.interface = interface


@dataclass
class _StubSession:
    chat: ChatInterface
    raise_on_send: Exception | None = None
    sends_called_with: list = field(default_factory=list)

    def send(self, results):
        self.sends_called_with.append(results)
        if self.raise_on_send is not None:
            raise self.raise_on_send
        # Successful path: return a stub LLMResponse-like object. The real
        # response object has .usage and is consumed by _process_response;
        # in tests where send doesn't raise we never
        # reach those, so a minimal object is fine.

        class _StubResponse:
            usage = {}
            content = []

        return _StubResponse()


@dataclass
class _StubAgent:
    """Carries just the attributes _handle_tc_wake reads.

    The real handler reads more (executor wiring, _intrinsics, _config, etc.)
    so we drive the orphan-heal path directly through a thin re-implementation
    rather than calling the real method. The thin re-implementation MUST
    mirror the real handler's splice logic — see _drive_splice below — so
    these tests catch regressions in the failure-shape contract.
    """

    _chat: _StubChatHolder
    _session: _StubSession
    _tc_inbox: TCInbox = field(default_factory=TCInbox)
    _appendix_ids_by_source: dict[str, str] = field(default_factory=dict)
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    saves: int = 0

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))

    def _save_chat_history(self) -> None:
        self.saves += 1


def _make_notification_item(
    notif_id: str = "notif_xxx",
    call_id: str = "sn_xxx",
    source: str = "email",
    ref_id: str = "mail_001",
) -> InvoluntaryToolCall:
    call = ToolCallBlock(
        id=call_id,
        name="system",
        args={
            "action": "notification",
            "notif_id": notif_id,
            "source": source,
            "ref_id": ref_id,
            "received_at": "2026-05-02T00:00:00Z",
        },
    )
    result = ToolResultBlock(id=call_id, name="system", content="[system] new mail ...")
    return InvoluntaryToolCall(
        call=call,
        result=result,
        source=f"system.notification:{notif_id}",
        enqueued_at=0.0,
        coalesce=False,
        replace_in_history=False,
    )


def _drive_splice(
    agent: _StubAgent,
    items: list[InvoluntaryToolCall],
    process_response_fn=None,
) -> None:
    """Mirror of the inner splice loop in BaseAgent._handle_tc_wake.

    Kept minimal but structurally identical to the production code path,
    so a regression in the production handler can be reproduced here.
    Re-raises splice failures so callers can assert exception propagation.

    The try/except wraps the ENTIRE per-item splice (remove → add → save →
    send → process_response), matching production: any failure during the
    splice triggers orphan healing. ``process_response_fn`` simulates
    ``_process_response`` so tests can inject failures there too — that
    code path is what bricked lingtai-dev/mimo-v2.5 on 2026-05-02.
    """
    iface = agent._chat.interface
    for idx, item in enumerate(items):
        try:
            if getattr(item, "replace_in_history", False):
                prior_id = agent._appendix_ids_by_source.get(item.source)
                if prior_id is not None:
                    iface.remove_pair_by_call_id(prior_id)
                agent._appendix_ids_by_source.pop(item.source, None)
            iface.add_assistant_message(content=[item.call])
            if getattr(item, "replace_in_history", False):
                agent._appendix_ids_by_source[item.source] = item.call.id
            agent._save_chat_history()

            agent._log("tc_wake_dispatch", source=item.source, call_id=item.call.id)
            response = agent._session.send([item.result])
            agent._save_chat_history()
            if process_response_fn is not None:
                process_response_fn(response)
        except Exception as splice_err:
            if iface.has_pending_tool_calls():
                iface.close_pending_tool_calls(
                    reason=f"tc_wake splice failed: {str(splice_err)[:200]}",
                )
                agent._save_chat_history()
            agent._log(
                "tc_wake_send_error",
                source=item.source,
                call_id=item.call.id,
                error=str(splice_err)[:300],
            )
            for remaining in items[idx + 1 :]:
                agent._tc_inbox.enqueue(remaining)
            raise


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_send_failure_heals_orphan_tool_call():
    """When _session.send raises, the assistant tool_call must NOT remain
    orphaned. The heal path synthesizes a placeholder tool_result so the
    chat is well-formed for the next AED attempt."""
    iface = ChatInterface()
    session = _StubSession(chat=iface, raise_on_send=RuntimeError("LLM timeout"))
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item()

    with pytest.raises(RuntimeError, match="LLM timeout"):
        _drive_splice(agent, [item])

    # The chat must have NO pending tool_calls — the heal fired.
    assert iface.has_pending_tool_calls() is False
    # And there must be exactly one (assistant{tool_call}, user{tool_result})
    # pair in entries: the assistant entry from the splice, plus the
    # synthesized placeholder result.
    entries = iface.conversation_entries()
    assert len(entries) == 2
    assert entries[0].role == "assistant"
    assert isinstance(entries[0].content[0], ToolCallBlock)
    assert entries[0].content[0].id == "sn_xxx"
    assert entries[1].role == "user"
    assert isinstance(entries[1].content[0], ToolResultBlock)
    assert entries[1].content[0].id == "sn_xxx"
    # The placeholder is marked synthesized=True so consumers can tell.
    assert entries[1].content[0].synthesized is True


def test_send_failure_logs_send_error_with_context():
    """The error path emits a tc_wake_send_error log with notif_id/call_id
    context the operator needs for triage."""
    iface = ChatInterface()
    session = _StubSession(chat=iface, raise_on_send=RuntimeError("network drop"))
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item(notif_id="notif_observable", call_id="sn_observable")

    with pytest.raises(RuntimeError):
        _drive_splice(agent, [item])

    error_logs = [(et, f) for (et, f) in agent._logs if et == "tc_wake_send_error"]
    assert len(error_logs) == 1
    _, fields = error_logs[0]
    assert fields["call_id"] == "sn_observable"
    assert fields["source"] == "system.notification:notif_observable"
    assert "network drop" in fields["error"]


def test_send_failure_re_enqueues_unprocessed_items():
    """If the splice fails on item N of M, items N+1..M-1 must be re-queued
    so a future safe boundary can retry them. Items already processed
    (0..N-1) are NOT re-queued — they completed successfully."""
    iface = ChatInterface()
    # Send raises on the SECOND call only — first item splices successfully,
    # second item triggers the heal path.
    raise_after = {"count": 0}

    class _SecondCallRaises(_StubSession):
        def send(self, results):
            raise_after["count"] += 1
            if raise_after["count"] == 2:
                raise RuntimeError("second send fails")
            return super().send(results)

    session = _SecondCallRaises(chat=iface, raise_on_send=None)
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item_a = _make_notification_item(notif_id="notif_a", call_id="sn_a")
    item_b = _make_notification_item(notif_id="notif_b", call_id="sn_b")
    item_c = _make_notification_item(notif_id="notif_c", call_id="sn_c")

    with pytest.raises(RuntimeError, match="second send fails"):
        _drive_splice(agent, [item_a, item_b, item_c])

    # item_a fully processed (no re-enqueue). item_b's call landed and got
    # healed. item_c never started — re-enqueued for retry.
    drained = agent._tc_inbox.drain()
    assert len(drained) == 1
    assert drained[0].call.args["notif_id"] == "notif_c"


def test_send_failure_persists_healed_chat():
    """The heal path must call _save_chat_history AFTER close_pending_tool_calls
    so the orphan repair lands on disk, not just in memory. Otherwise a
    process crash between heal and next save loses the synthesized result."""
    iface = ChatInterface()
    session = _StubSession(chat=iface, raise_on_send=RuntimeError("boom"))
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item()

    saves_before = agent.saves
    with pytest.raises(RuntimeError):
        _drive_splice(agent, [item])

    # At least 2 saves: one after the assistant tool_call lands, one after
    # the synthesized tool_result heals the orphan. The exact count may
    # grow if intermediate saves are added; the invariant is "more saves
    # after heal than before splice."
    assert agent.saves >= saves_before + 2


def test_successful_send_does_not_invoke_heal():
    """Sanity: when send succeeds, no heal happens, no error log fires.
    We're not pessimizing the happy path."""
    iface = ChatInterface()
    session = _StubSession(chat=iface, raise_on_send=None)
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item()

    _drive_splice(agent, [item])

    # The assistant tool_call landed, but no tool_result was added by our
    # stub (the production code adds it via _process_response, which we
    # don't drive). What matters: no heal-related logs fired.
    error_logs = [et for (et, _) in agent._logs if et == "tc_wake_send_error"]
    assert error_logs == []
    # Nothing re-enqueued.
    assert len(agent._tc_inbox) == 0


def test_orphan_recovery_preserves_call_id_correlation():
    """The synthesized placeholder result must share the orphaned tool_call's
    id, so the next consumer (LLM, log reader, future psyche-keep filter)
    can correlate them. Without this, the heal produces a "zombie" result
    that doesn't belong to any call."""
    iface = ChatInterface()
    session = _StubSession(chat=iface, raise_on_send=RuntimeError("kaboom"))
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item(call_id="sn_correlation_check")

    with pytest.raises(RuntimeError):
        _drive_splice(agent, [item])

    entries = iface.conversation_entries()
    # entries[0]: assistant{tool_call(id=sn_correlation_check)}
    # entries[1]: user{tool_result(id=sn_correlation_check, synthesized=True)}
    assert entries[0].content[0].id == "sn_correlation_check"
    assert entries[1].content[0].id == "sn_correlation_check"
    assert entries[1].content[0].synthesized is True


def test_process_response_failure_heals_orphan():
    """Regression for lingtai-dev/mimo-v2.5 incident on 2026-05-02.

    The orphan-heal path was previously narrow — only `_session.send`
    failures were caught. But in production, the splice's send succeeded
    (so the synthetic call+result both landed) and a follow-up LLM call
    inside `_process_response` timed out at 300s — leaving the wire chat
    with an unanswered tool_call from a tool the model emitted in its
    response. AED then bricked trying to inject a recovery user message
    into a chat with pending tool_calls.

    The fix widens the try/except to cover the whole splice including
    `_process_response`."""
    iface = ChatInterface()

    # Stub session that adds the synthetic pair's tool_result on send,
    # mirroring production where the LLM adapter appends the result before
    # returning the response object.
    class _PairingStubSession(_StubSession):
        def send(self, results):
            for r in results:
                iface.add_tool_results([r])
            return super().send(results)

    session = _PairingStubSession(chat=iface, raise_on_send=None)
    agent = _StubAgent(_chat=_StubChatHolder(iface), _session=session)
    item = _make_notification_item(call_id="sn_first")

    def fake_process_response(response):
        # Production _process_response can append more assistant turns
        # (with tool_calls) and dispatch tools — both of which can fail.
        # Mimic the failure mode: a follow-up turn lands a tool_call onto
        # the wire, then the next dispatch raises.
        followup_call = ToolCallBlock(id="sn_followup", name="read", args={"file_path": "/tmp/x"})
        iface.add_assistant_message(content=[followup_call])
        raise RuntimeError("LLM API call timed out after 300s")

    with pytest.raises(RuntimeError, match="timed out after 300s"):
        _drive_splice(agent, [item], process_response_fn=fake_process_response)

    # The wire must be clean: no pending tool_calls. This is the invariant
    # that AED relies on to inject its recovery user message — without it,
    # the agent bricks on the next dispatch.
    assert iface.has_pending_tool_calls() is False
    # And the heal must have synthesized a placeholder for the follow-up
    # orphan (the original splice's call+result was already complete).
    entries = iface.conversation_entries()
    # entries: assistant{sn_first} user{sn_first} assistant{sn_followup} user{synth-sn_followup}
    assert len(entries) == 4
    assert entries[2].content[0].id == "sn_followup"
    assert entries[3].content[0].id == "sn_followup"
    assert entries[3].content[0].synthesized is True
    # And the heal logged with the FIRST call's call_id (the splice item being processed).
    error_logs = [(et, f) for (et, f) in agent._logs if et == "tc_wake_send_error"]
    assert len(error_logs) == 1
    assert error_logs[0][1]["call_id"] == "sn_first"
    assert "timed out after 300s" in error_logs[0][1]["error"]
