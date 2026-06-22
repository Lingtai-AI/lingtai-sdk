"""Turn-level integration tests for the latest-only ``_meta`` agent/guidance blocks.

These tests drive ``base_agent.turn._process_response`` end-to-end (with light
fakes) to verify the parent-identified blockers are actually fixed at the
boundary, not just in the helper:

  * blocker #1 — ``attach_active_runtime`` is invoked at the tool-batch
    boundary, so the latest provider-visible result gets ``_meta.agent_meta``
    and ``_meta.guidance``.
  * the latest-only invariant — a prior result loses ``_meta.agent_meta`` once a
    newer dict-shaped result takes over across consecutive batches.

The helper-level semantics (promotion, pending scaffolding, guidance schema)
are covered in ``tests/test_meta_block.py``; this file proves the wiring.
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
from lingtai_kernel.meta_block import stamp_meta


class _Guard:
    """Minimal guard exposing total_calls for the runtime counter."""

    def __init__(self, total_calls: int = 0) -> None:
        self.total_calls = total_calls

    def check_limit(self, count: int) -> str | None:
        return None

    def check_invalid_tool_limit(self) -> str | None:
        return None

    def record_calls(self, count: int) -> None:
        self.total_calls += count

    def clear_progress_notice(self) -> None:
        pass


class _Executor:
    """Returns one pre-stamped dict result per batch (mimics ToolExecutor)."""

    def __init__(self, contents: list[dict], guard: _Guard) -> None:
        self.guard = guard
        self._contents = list(contents)
        self._i = 0

    def execute(self, tool_calls, **kwargs):
        calls = list(tool_calls)
        content = self._contents[self._i]
        self._i += 1
        block = ToolResultBlock(id=calls[0].id or "call", name=calls[0].name, content=content)
        return [block], False, ""


class _Chat:
    def __init__(self) -> None:
        self.interface = ChatInterface()
        self.interface.add_system("system")
        self.interface.add_user_message("run tool")
        self.interface.add_assistant_message(
            [TextBlock("calling"), ToolCallBlock(id="call_1", name="bash", args={"c": "x"})]
        )
        self.committed: list[list] = []

    def commit_tool_results(self, results) -> None:
        self.committed.append(list(results))
        self.interface.add_tool_results(results)


class _Session:
    """Single-shot session: commits the batch and returns a terminal response."""

    def __init__(self, chat: _Chat) -> None:
        self.chat = chat
        self.sent = []

    def send(self, content):
        self.sent.append(content)
        self.chat.commit_tool_results(content)
        return LLMResponse(text="done", tool_calls=[])


class _Agent:
    def __init__(self, tmp_path: Path, contents: list[dict]) -> None:
        self._chat = _Chat()
        self.agent_name = "rt-agent"
        self._notification_live_holder = None
        self._runtime_live_holder = None
        self._intrinsics = {}
        self._working_dir = tmp_path
        self._cancel_event = threading.Event()
        self._on_tool_result_hook = None
        self._intermediate_text_streamed = True
        self._sent_tracker = object()
        self.guard = _Guard(total_calls=2)
        self._executor = _Executor(contents, self.guard)
        self._session = _Session(self._chat)
        self.saved = 0
        self.logs: list[tuple[str, dict]] = []

    def _save_chat_history(self, *, ledger_source: str | None = None) -> None:
        self.saved += 1

    def _log(self, event: str, **kwargs) -> None:
        self.logs.append((event, kwargs))


def _stamped(meta_value: str) -> dict:
    """A dict tool-result content already carrying a _runtime_pending snapshot."""
    content = {"status": "ok", "echo": meta_value}
    stamp_meta(content, {"current_time": meta_value, "context": {"usage": 0.1}}, 5)
    return content


def test_runtime_block_lands_on_latest_result_at_turn_boundary(tmp_path):
    agent = _Agent(tmp_path, [_stamped("T1")])

    response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})],
    )
    _process_response(agent, response, ledger_source="test")

    holder = agent._runtime_live_holder
    assert holder is not None, "attach_active_runtime was not invoked at the boundary"
    assert holder["_meta"]["agent_meta"]["current_time"] == "T1"
    # The turn records the batch's calls on the guard (2 seeded + 1 this batch),
    # and the boundary stamps the live total under _meta.agent_meta.
    assert holder["_meta"]["agent_meta"]["active_turn_tool_calls"] == 3
    # guidance from guidance.json rides on the latest result, with meta_readme
    # as an ordered section rather than a sibling key.
    guidance = holder["_meta"]["guidance"]
    assert guidance["schema_version"] == 1
    assert "meta_readme" not in guidance
    assert any(section.get("id") == "meta_readme" for section in guidance["sections"])
    # transient scaffolding is gone; no top-level counter repetition.
    assert "_runtime_pending" not in holder
    assert "active_turn_tool_calls" not in holder


def test_prior_runtime_block_is_stripped_when_newer_result_arrives(tmp_path):
    agent = _Agent(tmp_path, [_stamped("T1"), _stamped("T2")])

    first_response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_1", name="bash", args={"c": "x"})],
    )
    _process_response(agent, first_response, ledger_source="test")
    first_holder = agent._runtime_live_holder
    assert first_holder["_meta"]["agent_meta"]["current_time"] == "T1"

    # Stage a second assistant turn with a fresh tool call, then process it.
    agent._chat.interface.add_assistant_message(
        [TextBlock("again"), ToolCallBlock(id="call_2", name="bash", args={"c": "y"})]
    )
    agent._session = _Session(agent._chat)
    second_response = LLMResponse(
        text="",
        tool_calls=[ToolCall(id="call_2", name="bash", args={"c": "y"})],
    )
    _process_response(agent, second_response, ledger_source="test")

    second_holder = agent._runtime_live_holder
    assert second_holder is not first_holder
    assert second_holder["_meta"]["agent_meta"]["current_time"] == "T2"
    # The previous holder must have shed its agent_meta/guidance (latest-only).
    assert "_meta" not in first_holder or "agent_meta" not in first_holder["_meta"]
