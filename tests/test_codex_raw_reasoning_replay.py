"""Tests for Codex Responses raw reasoning replay (encrypted_content preservation)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from lingtai.llm.interface_converters import to_responses_input
from lingtai.llm.openai.adapter import CodexOpenAIAdapter
from lingtai_kernel.llm.base import FunctionSchema
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
)


# ---------------------------------------------------------------------------
# Fake stream infrastructure (mirrors test_openai_responses_streaming.py)
# ---------------------------------------------------------------------------


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
        name="answer",
        description="Answer",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )


def _create_codex_session(events: list[Event]):
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
        thinking="high",
    )


# ---------------------------------------------------------------------------
# Helper event builders
# ---------------------------------------------------------------------------


def _reasoning_events_with_encrypted(
    *,
    item_id: str = "rs_enc",
    encrypted_content: str = "ENCRYPTED_BLOB_XYZ",
    summary_text: str = "I will answer.",
) -> list[Event]:
    return [
        Event(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id=item_id),
        ),
        Event(
            "response.reasoning_summary_text.delta",
            delta=summary_text,
            item_id=item_id,
        ),
        Event(
            "response.reasoning_summary_text.done",
            item_id=item_id,
            text=summary_text,
        ),
        Event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="reasoning",
                id=item_id,
                summary=[SimpleNamespace(type="summary_text", text=summary_text)],
                encrypted_content=encrypted_content,
            ),
        ),
    ]


def _reasoning_events_no_encrypted(
    *,
    item_id: str = "rs_plain",
    summary_text: str = "Plain summary.",
) -> list[Event]:
    return [
        Event(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id=item_id),
        ),
        Event(
            "response.reasoning_summary_text.delta",
            delta=summary_text,
            item_id=item_id,
        ),
        Event(
            "response.reasoning_summary_text.done",
            item_id=item_id,
            text=summary_text,
        ),
        Event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="reasoning",
                id=item_id,
                summary=[SimpleNamespace(type="summary_text", text=summary_text)],
                # No encrypted_content attribute
            ),
        ),
    ]


def _reasoning_event_encrypted_without_summary(
    *,
    item_id: str = "rs_no_summary",
    encrypted_content: str = "ENCRYPTED_WITHOUT_SUMMARY",
) -> list[Event]:
    return [
        Event(
            "response.output_item.added",
            item=SimpleNamespace(type="reasoning", id=item_id),
        ),
        Event(
            "response.output_item.done",
            item=SimpleNamespace(
                type="reasoning",
                id=item_id,
                summary=[],
                content=[],
                encrypted_content=encrypted_content,
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Test: include=["reasoning.encrypted_content"] is added to Codex request
# ---------------------------------------------------------------------------


def test_codex_request_includes_reasoning_encrypted_content():
    events = _reasoning_events_with_encrypted() + [_completed()]
    session = _create_codex_session(events)
    session.send("go")

    kwargs_sent = session._client.responses.kwargs[0]
    assert "reasoning.encrypted_content" in kwargs_sent.get("include", [])


def test_codex_include_merge_does_not_duplicate():
    """If extra_kwargs already has include, encrypted_content is appended once."""
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
    )
    events = _reasoning_events_with_encrypted() + [_completed()]
    adapter._client = FakeClient(events)
    # Inject extra_kwargs with an existing include list via _extra_kwargs override
    session = adapter.create_chat("gpt-5.5", "sys", tools=[_function_schema()], thinking="high")
    # Manually inject a pre-existing include to test merge
    session._extra_kwargs["include"] = ["some.other.field"]
    session.send("go")

    kwargs_sent = session._client.responses.kwargs[0]
    include = kwargs_sent.get("include", [])
    assert include.count("reasoning.encrypted_content") == 1
    assert "some.other.field" in include


def test_codex_include_merge_accepts_existing_string_include():
    adapter = CodexOpenAIAdapter(
        api_key="fake",
        base_url="http://fake",
        use_responses=True,
        force_responses=True,
    )
    events = _reasoning_events_with_encrypted() + [_completed()]
    adapter._client = FakeClient(events)
    session = adapter.create_chat("gpt-5.5", "sys", tools=[_function_schema()], thinking="high")
    session._extra_kwargs["include"] = "message.output_text.logprobs"
    session.send("go")

    include = session._client.responses.kwargs[0].get("include", [])
    assert include == ["message.output_text.logprobs", "reasoning.encrypted_content"]


# ---------------------------------------------------------------------------
# Test: encrypted_content captured into ThinkingBlock.provider_data
# ---------------------------------------------------------------------------


def test_encrypted_reasoning_stored_in_provider_data():
    events = _reasoning_events_with_encrypted(
        encrypted_content="BLOB123", summary_text="I will answer."
    ) + [_completed()]
    session = _create_codex_session(events)
    session.send("go")

    entry = session.interface.entries[-1]
    thinking_blocks = [b for b in entry.content if isinstance(b, ThinkingBlock)]
    assert len(thinking_blocks) == 1
    block = thinking_blocks[0]
    assert block.text == "I will answer."
    raw = block.provider_data.get("openai_responses_reasoning_item")
    assert raw is not None
    assert raw["type"] == "reasoning"
    assert raw["encrypted_content"] == "BLOB123"
    assert raw["id"] == "rs_enc"


def test_no_encrypted_content_leaves_provider_data_empty():
    events = _reasoning_events_no_encrypted(summary_text="Plain.") + [_completed()]
    session = _create_codex_session(events)
    session.send("go")

    entry = session.interface.entries[-1]
    thinking_blocks = [b for b in entry.content if isinstance(b, ThinkingBlock)]
    assert len(thinking_blocks) == 1
    block = thinking_blocks[0]
    assert block.text == "Plain."
    assert "openai_responses_reasoning_item" not in block.provider_data


def test_encrypted_reasoning_without_summary_still_stores_provider_data():
    """Codex often returns summary=[] with encrypted_content; keep that state."""
    events = _reasoning_event_encrypted_without_summary(
        encrypted_content="RAW_ONLY_BLOB"
    ) + [_completed()]
    session = _create_codex_session(events)
    session.send("go")

    entry = session.interface.entries[-1]
    thinking_blocks = [b for b in entry.content if isinstance(b, ThinkingBlock)]
    assert len(thinking_blocks) == 1
    block = thinking_blocks[0]
    assert block.text == ""
    raw = block.provider_data.get("openai_responses_reasoning_item")
    assert raw is not None
    assert raw["id"] == "rs_no_summary"
    assert raw["summary"] == []
    assert raw["content"] == []
    assert raw["encrypted_content"] == "RAW_ONLY_BLOB"


# ---------------------------------------------------------------------------
# Test: interface_converters.to_responses_input() prefers raw item
# ---------------------------------------------------------------------------


def test_to_responses_input_uses_raw_item_when_provider_data_present():
    """ThinkingBlock with raw reasoning item replays it verbatim, not as summary_text."""
    iface = ChatInterface()
    iface.add_user_message("hi")
    raw_item = {
        "type": "reasoning",
        "id": "rs_abc",
        "summary": [{"type": "summary_text", "text": "I thought about it."}],
        "encrypted_content": "ENCRYPTED_BLOB",
    }
    iface.add_assistant_message([
        ThinkingBlock(
            text="I thought about it.",
            provider_data={"openai_responses_reasoning_item": raw_item},
        ),
        TextBlock(text="Done."),
    ])

    items = to_responses_input(iface)

    reasoning_items = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    # Must preserve the raw item contents (with encrypted_content), not reconstruct summary_text.
    # It is a deep copy so SDK/request mutation cannot corrupt provider_data history.
    assert reasoning_items[0] is not raw_item
    assert reasoning_items[0] == raw_item
    assert reasoning_items[0]["encrypted_content"] == "ENCRYPTED_BLOB"
    assert reasoning_items[0]["id"] == "rs_abc"
    reasoning_items[0]["encrypted_content"] = "MUTATED"
    assert raw_item["encrypted_content"] == "ENCRYPTED_BLOB"


def test_to_responses_input_fallback_to_summary_text_without_raw_item():
    """ThinkingBlock without raw provider_data still emits summary_text item."""
    iface = ChatInterface()
    iface.add_user_message("hi")
    iface.add_assistant_message([
        ThinkingBlock(text="Just a summary."),
        TextBlock(text="Done."),
    ])

    items = to_responses_input(iface)

    reasoning_items = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "Just a summary."}],
    }
    assert "encrypted_content" not in reasoning_items[0]


def test_to_responses_input_ignores_invalid_raw_item():
    """A provider_data raw item with wrong type falls back to summary_text."""
    iface = ChatInterface()
    iface.add_assistant_message([
        ThinkingBlock(
            text="my thought",
            provider_data={"openai_responses_reasoning_item": {"type": "message", "id": "x"}},
        ),
    ])

    items = to_responses_input(iface)
    reasoning_items = [it for it in items if it.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0] == {
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": "my thought"}],
    }


def test_to_responses_input_ignores_redacted_encrypted_content():
    """Durable history redaction must not replay a placeholder as provider state."""
    iface = ChatInterface()
    iface.add_assistant_message([
        ThinkingBlock(
            text="redacted fallback",
            provider_data={
                "openai_responses_reasoning_item": {
                    "type": "reasoning",
                    "id": "rs_redacted",
                    "summary": [],
                    "content": [],
                    "encrypted_content": "<REDACTED:secret>",
                }
            },
        ),
    ])

    items = to_responses_input(iface)

    assert items == [
        {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "redacted fallback"}],
        }
    ]


def test_multiple_encrypted_reasoning_items_are_preserved_in_order():
    events = (
        _reasoning_event_encrypted_without_summary(
            item_id="rs_first", encrypted_content="FIRST_BLOB"
        )
        + _reasoning_event_encrypted_without_summary(
            item_id="rs_second", encrypted_content="SECOND_BLOB"
        )
        + [_completed()]
    )
    session = _create_codex_session(events)
    session.send("go")

    entry = session.interface.entries[-1]
    thinking_blocks = [b for b in entry.content if isinstance(b, ThinkingBlock)]
    assert [
        b.provider_data["openai_responses_reasoning_item"]["id"]
        for b in thinking_blocks
    ] == ["rs_first", "rs_second"]
    assert [
        b.provider_data["openai_responses_reasoning_item"]["encrypted_content"]
        for b in thinking_blocks
    ] == ["FIRST_BLOB", "SECOND_BLOB"]


# ---------------------------------------------------------------------------
# Test: encrypted_content not leaked into trace log
# ---------------------------------------------------------------------------


def test_trace_log_does_not_contain_encrypted_content(tmp_path, monkeypatch):
    import json

    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("LINGTAI_CODEX_RESPONSES_TRACE", "1")
    monkeypatch.setenv("LINGTAI_CODEX_RESPONSES_TRACE_PATH", str(trace_path))

    events = _reasoning_events_with_encrypted(
        encrypted_content="TOP_SECRET_BLOB", summary_text="Think."
    ) + [_completed()]
    session = _create_codex_session(events)
    session.send("go")

    assert trace_path.exists()
    content = trace_path.read_text()
    assert "TOP_SECRET_BLOB" not in content


# ---------------------------------------------------------------------------
# Test: full round-trip — encrypted item replayed on next turn
# ---------------------------------------------------------------------------


def test_encrypted_reasoning_replayed_verbatim_on_next_turn():
    """After a turn with encrypted reasoning, next send includes the raw item in input."""
    events_turn1 = _reasoning_events_with_encrypted(
        item_id="rs_1", encrypted_content="ENC_BLOB_1", summary_text="Thought 1."
    ) + [_completed()]
    events_turn2 = [_completed()]

    all_events = events_turn1 + events_turn2
    session = _create_codex_session(all_events)

    session.send("first message")

    # Verify provider_data was stored
    entry = session.interface.entries[-1]
    thinking = next(b for b in entry.content if isinstance(b, ThinkingBlock))
    assert thinking.provider_data.get("openai_responses_reasoning_item", {}).get("encrypted_content") == "ENC_BLOB_1"

    # Second turn — what gets sent as input?
    session.send("second message")

    kwargs_turn2 = session._client.responses.kwargs[1]
    input_items = kwargs_turn2["input"]
    reasoning_replay = [it for it in input_items if it.get("type") == "reasoning"]
    assert len(reasoning_replay) == 1
    replayed = reasoning_replay[0]
    assert replayed["encrypted_content"] == "ENC_BLOB_1"
    assert replayed["id"] == "rs_1"
