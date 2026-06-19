"""Tests for OpenAI Responses API input conversion."""

from __future__ import annotations

from lingtai.llm.interface_converters import to_responses_input
from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_responses_input_replays_thinking_block_as_reasoning_summary():
    iface = ChatInterface()
    iface.add_user_message("What should we do?")
    iface.add_assistant_message([
        ThinkingBlock(text="Need to inspect the inbox before answering."),
        TextBlock(text="I'll check first."),
        ToolCallBlock(id="call_123", name="email", args={"action": "check"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="call_123", name="email", content={"count": 0}),
    ])

    items = to_responses_input(iface)

    assert items == [
        {"role": "user", "content": "What should we do?"},
        {
            "type": "reasoning",
            "summary": [
                {
                    "type": "summary_text",
                    "text": "Need to inspect the inbox before answering.",
                }
            ],
        },
        {"role": "assistant", "content": "I'll check first."},
        {
            "type": "function_call",
            "call_id": "call_123",
            "name": "email",
            "arguments": '{"action": "check"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": '{"count": 0}',
        },
    ]


def test_responses_input_omits_empty_thinking_blocks():
    iface = ChatInterface()
    iface.add_assistant_message([
        ThinkingBlock(text=""),
        TextBlock(text="visible"),
    ])

    assert to_responses_input(iface) == [
        {"role": "assistant", "content": "visible"},
    ]
