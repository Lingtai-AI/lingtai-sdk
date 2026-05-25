"""Regression tests for preserving real tool results when continuation fails.

The turn engine executes tools locally before asking the LLM to continue from
those tool results. If the provider call fails after the tool result exists,
recovery must not replace the real result with a synthetic completion notice
that loses the result payload.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lingtai_kernel.base_agent.turn import (
    _process_response,
    _restore_tool_results_after_continuation_failure,
)
from lingtai_kernel.llm.base import LLMResponse, ToolCall
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


class _FakeChat:
    def __init__(
        self,
        tool_calls: list[ToolCallBlock] | None = None,
    ) -> None:
        self.interface = ChatInterface()
        self.interface.add_system("system")
        self.interface.add_user_message("run tool")
        calls = tool_calls or [
            ToolCallBlock(id="call_1", name="bash", args={"command": "echo ok"}),
        ]
        self.interface.add_assistant_message([TextBlock("calling"), *calls])
        self.committed: list[list[ToolResultBlock]] = []

    def commit_tool_results(self, results: list[ToolResultBlock]) -> None:
        self.committed.append(list(results))
        self.interface.add_tool_results(results)


class _FakeAgent:
    def __init__(self) -> None:
        self._chat = _FakeChat()
        self._notification_live_holder = None
        self.saved = 0
        self.logs: list[tuple[str, dict]] = []

    def _save_chat_history(self, *, ledger_source: str | None = None) -> None:
        self.saved += 1

    def _log(self, event: str, **kwargs) -> None:
        self.logs.append((event, kwargs))


def test_restores_real_tool_results_when_adapter_rolled_back_user_entry():
    """If send(tool_results) fails after local execution, restore real results."""
    agent = _FakeAgent()
    real_result = ToolResultBlock(id="call_1", name="bash", content="exit_code=0\nstdout=ok")

    restored = _restore_tool_results_after_continuation_failure(
        agent,
        [real_result],
        ledger_source="test",
    )

    assert restored is True
    assert not agent._chat.interface.has_pending_tool_calls()
    assert agent._chat.committed == [[real_result]]
    assert agent.saved == 1
    assert agent.logs == [("tool_results_restored_after_continuation_failure", {"result_count": 1})]

    tail = agent._chat.interface.entries[-1]
    assert tail.role == "user"
    assert tail.content == [real_result]
    assert not tail.content[0].synthesized


def test_restoration_skips_when_adapter_already_left_result():
    """Do not append duplicate results if the adapter did not roll back."""
    agent = _FakeAgent()
    real_result = ToolResultBlock(id="call_1", name="bash", content="exit_code=0")
    agent._chat.commit_tool_results([real_result])
    agent._chat.committed.clear()

    restored = _restore_tool_results_after_continuation_failure(
        agent,
        [real_result],
        ledger_source="test",
    )

    assert restored is False
    assert agent._chat.committed == []
    assert agent.saved == 0
    assert agent._chat.interface.entries[-1].content == [real_result]


def test_restoration_skips_empty_results():
    agent = _FakeAgent()

    restored = _restore_tool_results_after_continuation_failure(
        agent,
        [],
        ledger_source="test",
    )

    assert restored is False
    assert agent._chat.interface.has_pending_tool_calls()
    assert agent.saved == 0


class _FakeGuard:
    def check_limit(self, count: int) -> str | None:
        return None

    def check_invalid_tool_limit(self) -> str | None:
        return None

    def record_calls(self, count: int) -> None:
        pass


class _ProcessExecutor:
    def __init__(self, result: ToolResultBlock) -> None:
        self.guard = _FakeGuard()
        self.result = result

    def execute(self, tool_calls, **kwargs):
        return [self.result], False, ""


class _NoopSentTracker:
    pass


class _FailingSession:
    def __init__(self) -> None:
        self.sent = []

    def send(self, content):
        self.sent.append(content)
        raise RuntimeError("provider continuation failed")


def test_process_response_restores_real_results_when_continuation_send_fails():
    """Regression: _process_response preserves tool output before AED heal runs."""
    agent = _FakeAgent()
    real_result = ToolResultBlock(
        id="call_1",
        name="bash",
        content="exit_code=0\nstdout=ok",
    )
    agent._executor = _ProcessExecutor(real_result)
    agent._session = _FailingSession()
    agent._cancel_event = threading.Event()
    agent._on_tool_result_hook = None
    agent._intermediate_text_streamed = True
    agent._sent_tracker = _NoopSentTracker()
    agent._working_dir = Path("/nonexistent/lingtai-test-tool-result-restore")

    response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"command": "echo ok"})],
    )

    with pytest.raises(RuntimeError, match="provider continuation failed"):
        _process_response(agent, response, ledger_source="test")

    assert agent._session.sent == [[real_result]]
    assert agent._chat.committed == [[real_result]]
    assert not agent._chat.interface.has_pending_tool_calls()
    assert agent._chat.interface.entries[-1].content == [real_result]
    assert not agent._chat.interface.entries[-1].content[0].synthesized
    assert agent.saved == 1
    assert agent.logs[-1] == (
        "tool_results_restored_after_continuation_failure",
        {"result_count": 1},
    )


# ---------------------------------------------------------------------------
# Multi-tool batch (parallel) — issue #170 follow-up
# ---------------------------------------------------------------------------


class _MultiResultExecutor:
    """Executor that returns multiple ToolResultBlocks for a parallel batch."""

    def __init__(self, results: list[ToolResultBlock]) -> None:
        self.guard = _FakeGuard()
        self.results = list(results)

    def execute(self, tool_calls, **kwargs):
        return list(self.results), False, ""


def test_process_response_restores_all_real_results_for_multi_tool_batch():
    """Issue #170: when an OpenAI multi_tool.parallel continuation fails
    (e.g. 400 'No tool output found for function call …'), every tool result
    from the batch must be restored — not just the first — so the AED heal
    doesn't replace real side-effect payloads with synthetic completion
    notices for the dropped ones."""
    multi_calls = [
        ToolCallBlock(
            id="call_EdftAY7JA6RiiI1N04kmEwLQ",
            name="bash",
            args={"command": "printf checkpoint"},
        ),
        ToolCallBlock(
            id="call_X__system_dismiss",
            name="system",
            args={"action": "dismiss", "channel": "system"},
        ),
    ]
    chat = _FakeChat(tool_calls=multi_calls)
    agent = _FakeAgent()
    agent._chat = chat

    real_results = [
        ToolResultBlock(
            id="call_EdftAY7JA6RiiI1N04kmEwLQ",
            name="bash",
            content="exit_code=0\nstdout=checkpoint persisted",
        ),
        ToolResultBlock(
            id="call_X__system_dismiss",
            name="system",
            content={"status": "ok", "channel": "system", "cleared": True},
        ),
    ]

    agent._executor = _MultiResultExecutor(real_results)
    agent._session = _FailingSession()
    agent._cancel_event = threading.Event()
    agent._on_tool_result_hook = None
    agent._intermediate_text_streamed = True
    agent._sent_tracker = _NoopSentTracker()
    agent._working_dir = Path("/nonexistent/lingtai-test-multi-tool-restore")

    response = LLMResponse(
        text="",
        tool_calls=[
            ToolCall(id="call_EdftAY7JA6RiiI1N04kmEwLQ", name="bash", args={"command": "printf checkpoint"}),
            ToolCall(id="call_X__system_dismiss", name="system", args={"action": "dismiss", "channel": "system"}),
        ],
    )

    with pytest.raises(RuntimeError, match="provider continuation failed"):
        _process_response(agent, response, ledger_source="test")

    # Both real results must be committed to the canonical interface (no payload loss).
    assert chat.committed == [real_results]
    assert not chat.interface.has_pending_tool_calls()
    tail = chat.interface.entries[-1]
    assert tail.role == "user"
    # Both ToolResultBlocks ride along the same tail user entry, with real
    # payloads intact and the synthesized flag cleared.
    assert [b.id for b in tail.content] == [
        "call_EdftAY7JA6RiiI1N04kmEwLQ",
        "call_X__system_dismiss",
    ]
    assert all(isinstance(b, ToolResultBlock) for b in tail.content)
    assert all(not b.synthesized for b in tail.content)
    assert tail.content[0].content == "exit_code=0\nstdout=checkpoint persisted"
    assert tail.content[1].content == {
        "status": "ok",
        "channel": "system",
        "cleared": True,
    }


# ---------------------------------------------------------------------------
# Deterministic wire invariant — every assistant tool_call must have a
# matching tool_result before the next provider continuation request.
# ---------------------------------------------------------------------------


def test_close_pending_tool_calls_synthesizes_for_every_call_in_tail_multi_tool():
    """Issue #170 invariant: a tail assistant entry that carries multiple
    ToolCallBlocks (the OpenAI ``multi_tool.parallel`` shape) must be healed
    with one ToolResultBlock per call when none of the real results survived
    the rollback. Without this guarantee the next continuation request
    contains a ``function_call`` with no matching ``function_call_output``
    and the provider returns ``400 No tool output found``.
    """
    iface = ChatInterface()
    iface.add_system("system")
    iface.add_user_message("do two things")
    iface.add_assistant_message(
        [
            TextBlock("calling two tools in parallel"),
            ToolCallBlock(id="call_A", name="bash", args={"command": "ls"}),
            ToolCallBlock(
                id="call_B",
                name="system",
                args={"action": "dismiss", "channel": "system"},
            ),
        ]
    )
    assert iface.has_pending_tool_calls()

    iface.close_pending_tool_calls(reason="multi_tool continuation failed")

    assert not iface.has_pending_tool_calls()
    tail = iface.entries[-1]
    assert tail.role == "user"
    assert [b.id for b in tail.content] == ["call_A", "call_B"]
    assert all(isinstance(b, ToolResultBlock) for b in tail.content)
    assert all(b.synthesized for b in tail.content)


def test_codex_responses_wire_pairs_earlier_orphan_with_synthetic_output():
    """Same wire invariant for orphans that live earlier in the history
    (not the tail). A prior assistant turn may carry a tool_call whose
    matching tool_result was lost — for example, a crashed mid-tool-loop
    restore that left the call but not the result. ``enforce_tool_pairing``
    strips those from non-tail entries, but the Codex Responses path may
    serialize before ``enforce_tool_pairing`` runs (or for adapters that
    rely solely on wire-layer pairing). Either way, the outgoing wire must
    never carry an unmatched ``function_call``.

    The canonical interface refuses to build this shape via the normal
    add_* methods (PendingToolCallsError guards that boundary), so this
    test reconstructs it via :meth:`ChatInterface.from_dict` — the same
    path used to restore a session from disk that crashed mid-loop.
    """
    from lingtai.llm.interface_converters import to_responses_input

    iface = ChatInterface.from_dict(
        [
            {"id": 0, "role": "system", "system": "system", "timestamp": 0.0},
            {
                "id": 1,
                "role": "user",
                "content": [{"type": "text", "text": "first"}],
                "timestamp": 0.0,
            },
            {
                "id": 2,
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_call",
                        "id": "call_earlier_orphan",
                        "name": "bash",
                        "args": {"command": "ls"},
                    }
                ],
                "timestamp": 0.0,
            },
            # No tool_result for call_earlier_orphan — restored history is
            # missing the pair. Later turns continued normally.
            {
                "id": 3,
                "role": "user",
                "content": [{"type": "text", "text": "continue without the result"}],
                "timestamp": 0.0,
            },
            {
                "id": 4,
                "role": "assistant",
                "content": [{"type": "text", "text": "ok continuing"}],
                "timestamp": 0.0,
            },
        ]
    )

    items = to_responses_input(iface)
    function_call_ids = {it["call_id"] for it in items if it.get("type") == "function_call"}
    output_ids = {it["call_id"] for it in items if it.get("type") == "function_call_output"}
    assert function_call_ids <= output_ids, (
        f"Codex Responses wire shipped function_call ids without matching "
        f"function_call_output: {function_call_ids - output_ids}."
    )


def test_tc_wake_legacy_path_restores_real_item_result_when_send_fails():
    """Issue #170: when the legacy tc_inbox splice path's ``send([item.result])``
    fails and the adapter rolled the user entry back, the kernel must
    restore the real item.result before the catch-all heal calls
    ``close_pending_tool_calls`` — otherwise the original notification
    payload is permanently replaced by a synthesized kernel notice and the
    agent loses the message that was on the wire.
    """
    from dataclasses import dataclass, field
    from lingtai_kernel.base_agent.turn import _handle_tc_wake
    from lingtai_kernel.tc_inbox import InvoluntaryToolCall, TCInbox

    iface = ChatInterface()
    iface.add_system("system")
    iface.add_user_message("idle")

    real_notification_result = ToolResultBlock(
        id="sn_notif_1",
        name="system",
        content="[system] real notification payload that must survive heal",
    )

    @dataclass
    class _StubChatHolder:
        interface: ChatInterface

        def commit_tool_results(self, results):
            self.interface.add_tool_results(results)

    @dataclass
    class _StubSession:
        chat: ChatInterface
        sends: list = field(default_factory=list)

        def send(self, payload):
            self.sends.append(payload)
            # Adapter would have appended the user entry before failing;
            # mirror its rollback (drop_trailing user) so the kernel
            # restore path sees the rolled-back tail.
            iface.drop_trailing(lambda e: e.role == "user")
            raise RuntimeError("provider 400 No tool output found")

        def ensure_session(self):
            pass

    @dataclass
    class _StubAgent:
        _chat: _StubChatHolder
        _session: _StubSession
        _tc_inbox: TCInbox = field(default_factory=TCInbox)
        _appendix_ids_by_source: dict = field(default_factory=dict)
        _intrinsics: dict = field(default_factory=dict)
        _tool_handlers: dict = field(default_factory=dict)
        _PARALLEL_SAFE_TOOLS: set = field(default_factory=set)
        _working_dir = None
        _logs: list = field(default_factory=list)
        saves: int = 0

        def _log(self, event_type, **fields):
            self._logs.append((event_type, fields))

        def _save_chat_history(self, *, ledger_source=None):
            self.saves += 1

        def _dispatch_tool(self, call):
            return {"status": "ok"}

        class _ConfigStub:
            max_turns = 8
            language = "en"
            provider = "openai"

        _config = _ConfigStub()

        class _ServiceStub:
            def make_tool_result(self, name, result, **kw):
                return ToolResultBlock(id=kw.get("tool_call_id") or "", name=name, content=result)

        service = _ServiceStub()

    item = InvoluntaryToolCall(
        call=ToolCallBlock(
            id="sn_notif_1",
            name="system",
            args={"action": "notification", "notif_id": "n1"},
        ),
        result=real_notification_result,
        source="system.notification:n1",
        enqueued_at=0.0,
        coalesce=False,
        replace_in_history=False,
    )
    inbox = TCInbox()
    inbox.enqueue(item)

    agent = _StubAgent(
        _chat=_StubChatHolder(iface),
        _session=_StubSession(chat=iface),
        _tc_inbox=inbox,
    )

    from lingtai_kernel.message import Message, MSG_TC_WAKE
    wake_msg = Message(type=MSG_TC_WAKE, sender="kernel", content="", timestamp=0.0)

    with pytest.raises(RuntimeError, match="No tool output found"):
        _handle_tc_wake(agent, wake_msg)

    # After the failure, the real notification payload must still be on
    # the wire — not a synthesized kernel notice that overwrote it.
    tail = iface.entries[-1]
    assert tail.role == "user"
    assert len(tail.content) == 1
    result_block = tail.content[0]
    assert isinstance(result_block, ToolResultBlock)
    assert result_block.id == "sn_notif_1"
    assert (
        result_block.content
        == "[system] real notification payload that must survive heal"
    ), "Real notification payload was overwritten by a synthetic placeholder."
    assert result_block.synthesized is False
    # And the kernel logged that it restored the real result.
    restore_logs = [
        f for et, f in agent._logs if et == "tool_results_restored_after_continuation_failure"
    ]
    assert len(restore_logs) == 1
    assert restore_logs[0] == {"result_count": 1}


def test_codex_responses_wire_pairs_tail_orphan_with_synthetic_output():
    """Issue #170: the Codex Responses adapter must never emit a
    ``function_call`` whose ``call_id`` lacks a matching
    ``function_call_output``. This is the wire-level invariant that
    prevents the provider's ``400 'No tool output found for function
    call …'`` error during AED retries or any other send that runs while
    the canonical tail still has dangling tool_calls.
    """
    from lingtai.llm.interface_converters import to_responses_input

    iface = ChatInterface()
    iface.add_system("system")
    iface.add_user_message("ping")
    iface.add_assistant_message(
        [
            ToolCallBlock(id="call_orphan", name="bash", args={"command": "echo orphan"}),
        ]
    )
    # No tool_result for call_orphan — exactly the rolled-back state from a
    # failed multi_tool continuation that AED is about to retry.
    items = to_responses_input(iface)
    function_call_ids = {it["call_id"] for it in items if it.get("type") == "function_call"}
    output_ids = {it["call_id"] for it in items if it.get("type") == "function_call_output"}
    # Without the wire-layer pairing guard this assertion fails — the
    # outgoing request would carry a ``function_call`` for ``call_orphan``
    # with no matching ``function_call_output`` and the provider would
    # reject the input with ``400 No tool output found``.
    assert function_call_ids <= output_ids, (
        f"Codex Responses wire shipped function_call ids without matching "
        f"function_call_output: {function_call_ids - output_ids}. Add a "
        f"wire-layer pairing guard (close pending tool calls or synthesize "
        f"function_call_output items) before serialization."
    )
