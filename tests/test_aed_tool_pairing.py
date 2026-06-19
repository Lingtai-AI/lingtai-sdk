"""Integration test: AED recovery produces a well-formed request after a
tool-loop send fails.

Simulates the scenario that caused the real-world DeepSeek 400 cascade:
tool-call turn → send raises → AED kicks in → next request must not have
a dangling assistant[tool_calls] followed by a plain-text user message.
"""
from __future__ import annotations

from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
)


def test_close_pending_before_user_message_produces_valid_wire_format():
    """After close_pending_tool_calls + add_user_message, the canonical
    interface converts to a well-formed OpenAI wire sequence with no
    assistant[tool_calls] stranded before a user text message."""
    from lingtai.llm.interface_converters import to_openai

    iface = ChatInterface()
    iface.add_system("you are helpful")
    iface.add_user_message("start")
    iface.add_assistant_message(
        [
            TextBlock(text="checking"),
            ToolCallBlock(id="call_A", name="tool1", args={}),
            ToolCallBlock(id="call_B", name="tool2", args={}),
        ],
    )
    # Simulate: send(tool_results) raised; AED recovers by closing pending.
    iface.close_pending_tool_calls(reason="simulated: tool send failed")
    # AED then injects revive message — must not raise.
    iface.add_user_message("[system] retry — please continue")

    wire = to_openai(iface)

    # The assistant turn with tool_calls must be IMMEDIATELY followed by
    # two 'tool' entries (one per call id), THEN the user message.
    assistant_idx = next(
        i for i, m in enumerate(wire)
        if m["role"] == "assistant" and m.get("tool_calls")
    )
    assert wire[assistant_idx + 1]["role"] == "tool"
    assert wire[assistant_idx + 2]["role"] == "tool"
    # The user turns after the tool entries carry the recovery message.
    assert wire[assistant_idx + 3]["role"] == "user"
    # Every tool_call id is answered before the next assistant/user text.
    answered_ids = {wire[assistant_idx + 1]["tool_call_id"], wire[assistant_idx + 2]["tool_call_id"]}
    assert answered_ids == {"call_A", "call_B"}
