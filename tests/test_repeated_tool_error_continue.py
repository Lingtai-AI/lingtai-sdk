"""Regression tests for repeated tool errors staying in normal tool flow.

Incident (2026-06-12, mimo-1): repeated identical tool errors used to trigger a
special turn-level hard-stop path. That path committed tool results and broke the
normal tool loop, which could silently drop the agent to IDLE or require a
separate continuation/notification workaround.

The intended behavior is simpler: a tool error is still a tool result. Even if
the same error repeats, the runtime should keep sending ordinary tool-result
payloads back to the LLM, where the enriched tool error metadata explains the
cause and recovery guidance. Generic tool-loop limits remain the backstop for
true infinite loops; there is no repeated-identical-error hard-stop branch.
"""
from __future__ import annotations

import threading
from pathlib import Path


from lingtai_kernel.base_agent.turn import _process_response
from lingtai_kernel.llm.base import LLMResponse, ToolCall
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


# ---------------------------------------------------------------------------
# Shared test doubles
# ---------------------------------------------------------------------------


class _FakeChat:
    def __init__(self, tool_calls: list[ToolCallBlock] | None = None) -> None:
        self.interface = ChatInterface()
        self.interface.add_system("system")
        self.interface.add_user_message("run tool")
        calls = tool_calls or [
            ToolCallBlock(id="call_1", name="system", args={"action": "dismiss"}),
        ]
        self.interface.add_assistant_message([TextBlock("calling"), *calls])
        self.committed: list[list[ToolResultBlock]] = []

    def commit_tool_results(self, results: list[ToolResultBlock]) -> None:
        self.committed.append(list(results))
        self.interface.add_tool_results(results)


class _FakeAgent:
    def __init__(self, *, working_dir: Path | None = None) -> None:
        self._chat = _FakeChat()
        self.agent_name = "test-agent"
        self._notification_live_holder = None
        self._intrinsics = {}
        self._working_dir = working_dir or Path(
            "/nonexistent/lingtai-test-repeated-tool-error"
        )
        self.saved = 0
        self.save_sources: list[str | None] = []
        self.logs: list[tuple[str, dict]] = []

    def _save_chat_history(self, *, ledger_source: str | None = None) -> None:
        self.saved += 1
        self.save_sources.append(ledger_source)

    def _log(self, event: str, **kwargs) -> None:
        self.logs.append((event, kwargs))


class _FakeGuard:
    def check_limit(self, count: int) -> str | None:
        return None

    def check_invalid_tool_limit(self) -> str | None:
        return None

    def record_calls(self, count: int) -> None:
        pass

    def clear_progress_notice(self) -> None:
        pass


class _RepeatedErrorExecutor:
    """Returns an identical error for every tool call executed."""

    def __init__(self, error: str = "stale_channel_version: channel changed") -> None:
        self.guard = _FakeGuard()
        self.error = error
        self.calls: list[list] = []

    def execute(self, tool_calls, **kwargs):
        calls = list(tool_calls)
        self.calls.append(calls)
        collected_errors = kwargs.get("collected_errors")
        if collected_errors is not None:
            collected_errors.append(self.error)
        results = [
            ToolResultBlock(id=tc.id or f"call_{idx}", name=tc.name, content=self.error)
            for idx, tc in enumerate(calls)
        ]
        return results, False, ""


class _ContinuingSession:
    """Fake session.

    Each ``send`` should receive ordinary tool_results, commit them to the wire,
    return the next scripted ``LLMResponse``, and append that response to the
    wire so pending-call invariants are observable by the tests. Strings are
    still recorded if a regression reintroduces text continuations.
    """

    def __init__(self, chat: _FakeChat, responses: list[LLMResponse]) -> None:
        self.chat = chat
        self.responses = list(responses)
        self.sent: list = []

    def send(self, content):
        self.sent.append(content)
        if isinstance(content, str):
            self.chat.interface.add_user_message(content)
        else:
            self.chat.commit_tool_results(content)
        response = self.responses.pop(0)
        blocks: list = []
        if response.text:
            blocks.append(TextBlock(response.text))
        blocks.extend(
            ToolCallBlock(id=tc.id or "", name=tc.name, args=tc.args)
            for tc in response.tool_calls
        )
        if blocks:
            self.chat.interface.add_assistant_message(blocks)
        return response


class _NoopSentTracker:
    pass


def _make_agent(executor, chat=None, *, working_dir):
    agent = _FakeAgent(working_dir=working_dir)
    if chat is not None:
        agent._chat = chat
    agent._executor = executor
    agent._cancel_event = threading.Event()
    agent._on_tool_result_hook = None
    agent._intermediate_text_streamed = True
    agent._sent_tracker = _NoopSentTracker()
    return agent


# ---------------------------------------------------------------------------
# Core invariants
# ---------------------------------------------------------------------------


def test_repeated_identical_tool_errors_remain_normal_tool_results(tmp_path):
    """Three identical tool errors must not become a string continuation.

    The LLM receives each failure as ordinary tool_results and may then choose
    to stop/report. The turn engine should not synthesize a hard-stop text continuation,
    log a repeated-tool-error hard-stop event, or create a special notification.
    """
    agent = _make_agent(_RepeatedErrorExecutor(error="stale_channel_version: changed"), working_dir=tmp_path)
    responses = [
        LLMResponse(text="", tool_calls=[ToolCall(id="call_2", name="system", args={})]),
        LLMResponse(text="", tool_calls=[ToolCall(id="call_3", name="system", args={})]),
        LLMResponse(text="I saw the tool error payloads and will switch strategy.", tool_calls=[]),
    ]
    session = _ContinuingSession(agent._chat, responses)
    agent._session = session

    result = _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="system", args={})]),
        ledger_source="test",
    )

    assert result == {
        "text": "I saw the tool error payloads and will switch strategy.",
        "failed": False,
        "errors": [
            "stale_channel_version: changed",
            "stale_channel_version: changed",
            "stale_channel_version: changed",
        ],
    }
    assert len(session.sent) == 3
    assert all(isinstance(payload, list) for payload in session.sent)
    assert [batch[0].id for batch in agent._chat.committed] == ["call_1", "call_2", "call_3"]
    assert not agent._chat.interface.has_pending_tool_calls()
    assert not any(event.startswith("repeated_tool_error") for event, _ in agent.logs)
    assert not (tmp_path / ".notification" / "repeated_tool_error.json").exists()


def test_repeated_identical_tool_errors_can_continue_past_three(tmp_path):
    """No third-error guard should stop the model from receiving later results."""
    agent = _make_agent(_RepeatedErrorExecutor(error="same tool error"), working_dir=tmp_path)
    agent._session = _ContinuingSession(
        agent._chat,
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="call_2", name="bash", args={})]),
            LLMResponse(text="", tool_calls=[ToolCall(id="call_3", name="bash", args={})]),
            LLMResponse(text="", tool_calls=[ToolCall(id="call_4", name="bash", args={})]),
            LLMResponse(text="now I will stop", tool_calls=[]),
        ],
    )

    result = _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={})]),
        ledger_source="test",
    )

    assert result == {
        "text": "now I will stop",
        "failed": False,
        "errors": ["same tool error"] * 4,
    }
    assert len(agent._session.sent) == 4
    assert all(isinstance(payload, list) for payload in agent._session.sent)
    assert [batch[0].id for batch in agent._chat.committed] == [
        "call_1",
        "call_2",
        "call_3",
        "call_4",
    ]


def test_string_continuation_regression_would_be_visible(tmp_path):
    """Test double records strings so old hard-stop regressions fail loudly."""
    agent = _make_agent(_RepeatedErrorExecutor(error="same tool error"), working_dir=tmp_path)
    agent._session = _ContinuingSession(
        agent._chat,
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="call_2", name="bash", args={})]),
            LLMResponse(text="", tool_calls=[ToolCall(id="call_3", name="bash", args={})]),
            LLMResponse(text="done", tool_calls=[]),
        ],
    )

    _process_response(
        agent,
        LLMResponse(text="", tool_calls=[ToolCall(id="call_1", name="bash", args={})]),
        ledger_source="test",
    )

    assert not [payload for payload in agent._session.sent if isinstance(payload, str)]
