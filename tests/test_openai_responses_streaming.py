"""Tests for OpenAI Responses API streaming reasoning capture."""

from __future__ import annotations

import json

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from lingtai.llm.interface_converters import to_responses_input
from lingtai.llm.openai.adapter import (
    CodexOpenAIAdapter,
    OpenAIAdapter,
    OpenAIResponsesSession,
)
from lingtai_kernel.llm.base import FunctionSchema
from lingtai_kernel.llm.interface import TextBlock, ThinkingBlock, ToolCallBlock


@dataclass
class Event:
    type: str
    delta: str | None = None
    item: object | None = None
    response: object | None = None
    item_id: str | None = None
    text: str | None = None


class FakeResponses:
    def __init__(self, events: list[Event]):
        self.events = events
        self.kwargs: list[dict] = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        yield from self.events


class FakeClient:
    def __init__(self, events: list[Event]):
        self.responses = FakeResponses(events)


def _usage(*, reasoning_tokens: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=10,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=0),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )


def _completed() -> Event:
    return Event(
        "response.completed",
        response=SimpleNamespace(id="resp_fake", usage=_usage()),
    )


def _function_schema() -> FunctionSchema:
    return FunctionSchema(
        name="report_answer",
        description="Report answer",
        parameters={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )


def _function_call_events() -> list[Event]:
    return [
        Event(
            "response.output_item.added",
            item=SimpleNamespace(
                type="function_call",
                call_id="call_fake123",
                name="report_answer",
            ),
        ),
        Event("response.function_call_arguments.delta", delta='{"answer"'),
        Event("response.function_call_arguments.delta", delta=':"42"}'),
        Event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="function_call",
                call_id="call_fake123",
                name="report_answer",
                arguments='{"answer":"42"}',
            ),
        ),
    ]


def _reasoning_events() -> list[Event]:
    return [
        Event(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id="rs_fake"),
        ),
        Event(
            "response.reasoning_summary_text.delta",
            delta="I should call ",
            item_id="rs_fake",
        ),
        Event(
            "response.reasoning_summary_text.delta",
            delta="the report tool.",
            item_id="rs_fake",
        ),
        Event(
            "response.reasoning_summary_text.done",
            item_id="rs_fake",
            text="I should call the report tool.",
        ),
        Event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="reasoning",
                id="rs_fake",
                summary=[
                    SimpleNamespace(
                        type="summary_text",
                        text="I should call the report tool.",
                    )
                ],
            ),
        ),
    ]


def _create_codex_session(events: list[Event], *, thinking: str = "high"):
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
    )
    adapter._client = FakeClient(events)
    return adapter.create_chat(
        "gpt-5.5",
        "system prompt",
        tools=[_function_schema()],
        force_tool_call=True,
        thinking=thinking,
    )


@pytest.mark.parametrize("thinking", ["low", "medium", "high", "xhigh"])
def test_openai_responses_sends_exact_reasoning_effort(thinking):
    adapter = OpenAIAdapter(api_key="fake", use_responses=True)
    adapter._client = FakeClient([_completed()])
    session = adapter.create_chat("gpt-5.5", "system prompt", thinking=thinking)

    session.send_stream("hello")

    sent = adapter._client.responses.kwargs[-1]
    assert sent["reasoning"] == {"effort": thinking}
    assert "reasoning_effort" not in sent


@pytest.mark.parametrize("thinking", ["low", "medium", "high", "xhigh"])
def test_codex_responses_sends_exact_reasoning_effort(thinking):
    session = _create_codex_session([_completed()], thinking=thinking)

    session.send_stream("hello")

    sent = session._client.responses.kwargs[-1]
    assert sent["reasoning"] == {"effort": thinking}
    assert "reasoning_effort" not in sent


@pytest.mark.parametrize("adapter_cls", [OpenAIAdapter, CodexOpenAIAdapter])
def test_responses_rejects_unsupported_thinking(adapter_cls):
    kwargs = {"api_key": "fake", "use_responses": True}
    if adapter_cls is CodexOpenAIAdapter:
        kwargs.update({"base_url": "http://fake", "force_responses": True})
    adapter = adapter_cls(**kwargs)
    adapter._client = FakeClient([_completed()])

    with pytest.raises(ValueError, match="OpenAI Responses thinking"):
        adapter.create_chat("gpt-5.5", "system prompt", thinking="ultra")


def test_codex_stream_captures_reasoning_and_persists_thinking_block():
    session = _create_codex_session(_reasoning_events() + _function_call_events() + [_completed()])

    result = session.send("please answer via tool")

    assert result.thoughts == ["I should call the report tool."]
    assert result.tool_calls[0].name == "report_answer"

    assistant_entry = session.interface.entries[-1]
    assert isinstance(assistant_entry.content[0], ThinkingBlock)
    assert assistant_entry.content[0].text == "I should call the report tool."
    assert isinstance(assistant_entry.content[1], ToolCallBlock)
    assert not any(
        isinstance(block, TextBlock) and block.text == ""
        for block in assistant_entry.content
    )


def test_codex_replay_includes_reasoning_before_function_call():
    session = _create_codex_session(_reasoning_events() + _function_call_events() + [_completed()])

    session.send("please answer via tool")

    items = to_responses_input(session.interface)
    reasoning_index = next(
        idx for idx, item in enumerate(items) if item.get("type") == "reasoning"
    )
    call_index = next(
        idx for idx, item in enumerate(items) if item.get("type") == "function_call"
    )
    assert reasoning_index < call_index
    assert items[reasoning_index] == {
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": "I should call the report tool."}
        ],
    }


def test_reasoning_done_text_is_not_duplicated_after_delta():
    session = _create_codex_session(_reasoning_events() + [_completed()])

    result = session.send("think only")

    assert result.thoughts == ["I should call the report tool."]
    assistant_entry = session.interface.entries[-1]
    thinking_blocks = [
        block for block in assistant_entry.content if isinstance(block, ThinkingBlock)
    ]
    assert [block.text for block in thinking_blocks] == [
        "I should call the report tool."
    ]


def test_reasoning_done_text_is_used_as_fallback_without_delta():
    session = _create_codex_session(
        [
            Event(
                "response.reasoning_summary_text.done",
                item_id="rs_done_only",
                text="Done-only summary.",
            ),
            _completed(),
        ]
    )

    result = session.send("think only")

    assert result.thoughts == ["Done-only summary."]
    assistant_entry = session.interface.entries[-1]
    assert isinstance(assistant_entry.content[0], ThinkingBlock)
    assert assistant_entry.content[0].text == "Done-only summary."


def test_done_only_summary_is_not_duplicated_by_output_item_done():
    session = _create_codex_session(
        [
            Event(
                "response.reasoning_summary_text.done",
                item_id="rs_done_only",
                text="Done-only summary.",
            ),
            Event(
                "response.output_item.done",
                item=SimpleNamespace(
                    type="reasoning",
                    id="rs_done_only",
                    summary=[
                        SimpleNamespace(
                            type="summary_text",
                            text="Done-only summary.",
                        )
                    ],
                ),
            ),
            _completed(),
        ]
    )

    result = session.send("think only")

    assert result.thoughts == ["Done-only summary."]
    assistant_entry = session.interface.entries[-1]
    thinking_blocks = [
        block for block in assistant_entry.content if isinstance(block, ThinkingBlock)
    ]
    assert [block.text for block in thinking_blocks] == ["Done-only summary."]



def test_codex_responses_trace_disabled_by_default(tmp_path, monkeypatch):
    trace_path = tmp_path / "codex_responses_trace.jsonl"
    monkeypatch.delenv("LINGTAI_CODEX_RESPONSES_TRACE", raising=False)
    monkeypatch.setenv("LINGTAI_CODEX_RESPONSES_TRACE_PATH", str(trace_path))
    session = _create_codex_session(_reasoning_events() + _function_call_events() + [_completed()])

    result = session.send("please answer via tool")

    assert result.thoughts == ["I should call the report tool."]
    assert result.tool_calls[0].name == "report_answer"
    assert not trace_path.exists()


def test_codex_responses_trace_records_safe_metadata_when_enabled(tmp_path, monkeypatch):
    trace_path = tmp_path / "codex_responses_trace.jsonl"
    monkeypatch.setenv("LINGTAI_CODEX_RESPONSES_TRACE", "1")
    monkeypatch.setenv("LINGTAI_CODEX_RESPONSES_TRACE_PATH", str(trace_path))
    session = _create_codex_session(_reasoning_events() + _function_call_events() + [_completed()])

    result = session.send("please answer via tool")

    assert result.thoughts == ["I should call the report tool."]
    assert result.text == ""
    assert result.tool_calls[0].name == "report_answer"
    assistant_entry = session.interface.entries[-1]
    assert isinstance(assistant_entry.content[0], ThinkingBlock)
    assert isinstance(assistant_entry.content[1], ToolCallBlock)

    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    event_types = [record["event_type"] for record in records]
    assert "response.reasoning_summary_text.delta" in event_types
    assert "response.output_item.added" in event_types
    assert "response.function_call_arguments.delta" in event_types
    assert "response.completed" in event_types

    reasoning_delta = next(
        record
        for record in records
        if record["event_type"] == "response.reasoning_summary_text.delta"
    )
    assert reasoning_delta["accepted_reasoning"] is True
    assert reasoning_delta["item_id"] == "rs_fake"
    assert reasoning_delta["delta"]["length"] == len("I should call ")
    assert "sha256_12" in reasoning_delta["delta"]
    assert "I should call" not in json.dumps(reasoning_delta)

    function_arg_delta = next(
        record
        for record in records
        if record["event_type"] == "response.function_call_arguments.delta"
    )
    assert function_arg_delta["accepted_reasoning"] is False
    assert function_arg_delta["delta"]["length"] == len('{"answer"')
    assert "answer" not in json.dumps(function_arg_delta)

    completed = next(
        record for record in records if record["event_type"] == "response.completed"
    )
    assert completed["usage"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "cached_tokens": 0,
        "reasoning_tokens": 7,
    }
    assert completed["thoughts"]["after_count"] == 1


def test_openai_responses_stream_captures_summary_thoughts():
    session = OpenAIResponsesSession(
        client=FakeClient(_reasoning_events() + [_completed()]),
        model="gpt-5.5",
        instructions="system prompt",
        tools=None,
        tool_choice=None,
        extra_kwargs={},
    )

    result = session.send_stream("think")

    assert result.thoughts == ["I should call the report tool."]
