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
    def __init__(self) -> None:
        self.interface = ChatInterface()
        self.interface.add_system("system")
        self.interface.add_user_message("run tool")
        self.interface.add_assistant_message(
            [
                TextBlock("calling"),
                ToolCallBlock(id="call_1", name="bash", args={"command": "echo ok"}),
            ]
        )
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


class _HistorySaveFailingAgent(_FakeAgent):
    def _save_chat_history(self, *, ledger_source: str | None = None) -> None:
        raise RuntimeError("history disk full")


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


def test_restore_logs_save_failure_on_existing_recovery_event():
    agent = _HistorySaveFailingAgent()
    real_result = ToolResultBlock(id="call_1", name="bash", content="exit_code=0")

    with pytest.raises(RuntimeError, match="history disk full"):
        _restore_tool_results_after_continuation_failure(
            agent,
            [real_result],
            ledger_source="test",
        )

    assert agent._chat.committed == [[real_result]]
    assert agent.logs == [
        (
            "tool_results_restored_after_continuation_failure",
            {
                "result_count": 1,
                "ledger_source": "test",
                "failed_at": "save_chat_history",
                "save_error": "history disk full",
                "side_effect": "memory_state_may_be_ahead_of_disk",
            },
        )
    ]


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
    def __init__(
        self,
        *,
        stop_reason: str | None = None,
        invalid_reason: str | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        self.invalid_reason = invalid_reason

    def check_limit(self, count: int) -> str | None:
        return self.stop_reason

    def check_invalid_tool_limit(self) -> str | None:
        return self.invalid_reason

    def record_calls(self, count: int) -> None:
        pass


class _ProcessExecutor:
    def __init__(
        self,
        result: ToolResultBlock,
        *,
        guard: _FakeGuard | None = None,
    ) -> None:
        self.guard = guard or _FakeGuard()
        self.result = result
        self.calls = []

    def execute(self, tool_calls, **kwargs):
        self.calls.append(list(tool_calls))
        return [self.result], False, ""


class _NoopSentTracker:
    pass


class _FailingSession:
    def __init__(self) -> None:
        self.sent = []

    def send(self, content):
        self.sent.append(content)
        raise RuntimeError("provider continuation failed")


class _CancelAfterInitialClear:
    def __init__(self) -> None:
        self.clear_count = 0

    def clear(self) -> None:
        self.clear_count += 1

    def is_set(self) -> bool:
        return self.clear_count == 1


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


def test_process_response_logs_cancel_before_tool_dispatch():
    agent = _FakeAgent()
    real_result = ToolResultBlock(
        id="call_1",
        name="bash",
        content="should not execute",
    )
    agent._executor = _ProcessExecutor(real_result)
    agent._cancel_event = _CancelAfterInitialClear()
    agent._on_tool_result_hook = None
    agent._intermediate_text_streamed = True
    agent._sent_tracker = _NoopSentTracker()

    response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"command": "echo ok"})],
    )

    result = _process_response(agent, response, ledger_source="test")

    assert result == {"text": "", "failed": False, "errors": []}
    assert agent._executor.calls == []

    names = [name for name, _ in agent.logs]
    assert names == ["tool_calls_not_dispatched"]
    aborted = agent.logs[0][1]
    assert aborted == {
        "ledger_source": "test",
        "in_tool_loop": False,
        "reason": "cancel_event",
        "call_count": 1,
        "call_ids": ["call_1"],
        "tool_names": ["bash"],
    }


@pytest.mark.parametrize(
    ("guard", "reason", "extra"),
    [
        (
            _FakeGuard(stop_reason="max tool calls reached"),
            "tool_loop_limit",
            {"stop_reason": "max tool calls reached"},
        ),
        (
            _FakeGuard(invalid_reason="too many invalid tools"),
            "invalid_tool_limit",
            {"invalid_reason": "too many invalid tools"},
        ),
    ],
)
def test_process_response_logs_guarded_tool_calls_not_dispatched(guard, reason, extra):
    agent = _FakeAgent()
    real_result = ToolResultBlock(
        id="call_1",
        name="bash",
        content="should not execute",
    )
    agent._executor = _ProcessExecutor(real_result, guard=guard)
    agent._cancel_event = threading.Event()
    agent._on_tool_result_hook = None
    agent._intermediate_text_streamed = True
    agent._sent_tracker = _NoopSentTracker()

    response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"command": "echo ok"})],
    )

    result = _process_response(agent, response, ledger_source="test")

    assert result == {"text": "", "failed": False, "errors": []}
    assert agent._executor.calls == []
    assert agent.logs == [
        (
            "tool_calls_not_dispatched",
            {
                "ledger_source": "test",
                "in_tool_loop": False,
                "reason": reason,
                "call_count": 1,
                "call_ids": ["call_1"],
                "tool_names": ["bash"],
                **extra,
            },
        )
    ]
