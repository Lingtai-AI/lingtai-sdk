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

    def has_pending_tool_calls(self) -> bool:
        return self.interface.has_pending_tool_calls()

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
    assert not agent._chat.has_pending_tool_calls()
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
    assert agent._chat.has_pending_tool_calls()
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
    assert not agent._chat.has_pending_tool_calls()
    assert agent._chat.interface.entries[-1].content == [real_result]
    assert not agent._chat.interface.entries[-1].content[0].synthesized
    assert agent.saved == 1
    assert agent.logs[-1] == (
        "tool_results_restored_after_continuation_failure",
        {"result_count": 1},
    )
