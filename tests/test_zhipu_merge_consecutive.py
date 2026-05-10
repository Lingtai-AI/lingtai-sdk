"""Tests for _merge_consecutive_same_role in the Zhipu adapter.

Validates the wire-layer sanitization that merges consecutive messages
with the same role, preventing GLM error 1214.
"""

from __future__ import annotations

import logging

from lingtai.llm.zhipu.adapter import _merge_consecutive_same_role


# ---------------------------------------------------------------------------
# Idempotence — no-op cases
# ---------------------------------------------------------------------------


def test_empty_list():
    assert _merge_consecutive_same_role([]) == []


def test_single_message():
    msgs = [{"role": "user", "content": "hello"}]
    assert _merge_consecutive_same_role(msgs) == msgs


def test_no_consecutive_same_role():
    msgs = [
        {"role": "system", "content": "you are a bot"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "bye"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert result == msgs


# ---------------------------------------------------------------------------
# User message merging
# ---------------------------------------------------------------------------


def test_merge_consecutive_user_messages():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 3
    assert result[1] == {"role": "user", "content": "first\nsecond"}


def test_merge_three_consecutive_user_messages():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "a\nb\nc"


# ---------------------------------------------------------------------------
# Assistant message merging
# ---------------------------------------------------------------------------


def test_merge_consecutive_assistant_text_only():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "part1"},
        {"role": "assistant", "content": "part2"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 2
    assert result[1] == {"role": "assistant", "content": "part1\npart2"}


def test_merge_assistant_preserves_tool_calls_from_last():
    """tool_calls should come from the last assistant msg that has them."""
    tc_first = [{"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}}]
    tc_second = [{"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}}]
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "thinking", "tool_calls": tc_first},
        {"role": "assistant", "content": "more", "tool_calls": tc_second},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 2
    merged = result[1]
    assert merged["content"] == "thinking\nmore"
    assert merged["tool_calls"] == tc_second


def test_merge_assistant_only_last_has_tool_calls():
    """First assistant has no tool_calls, second does — should keep them."""
    tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "thought"},
        {"role": "assistant", "content": "", "tool_calls": tc},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 2
    merged = result[1]
    assert merged["content"] == "thought"
    assert merged["tool_calls"] == tc


def test_merge_assistant_only_first_has_tool_calls():
    """First assistant has tool_calls, second doesn't — first's preserved."""
    tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "act", "tool_calls": tc},
        {"role": "assistant", "content": "extra"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 2
    merged = result[1]
    assert merged["content"] == "act\nextra"
    # tool_calls from first msg retained (second has none so no overwrite)
    assert merged["tool_calls"] == tc


# ---------------------------------------------------------------------------
# System and tool messages are never merged
# ---------------------------------------------------------------------------


def test_system_messages_not_merged():
    msgs = [
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "hi"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 3
    assert result[0]["content"] == "first"
    assert result[1]["content"] == "second"


def test_tool_messages_not_merged():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "result1"},
        {"role": "tool", "tool_call_id": "c2", "content": "result2"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 4
    assert result[2]["tool_call_id"] == "c1"
    assert result[3]["tool_call_id"] == "c2"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_content_merged():
    msgs = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "hello"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "hello"


def test_none_content_treated_as_empty():
    msgs = [
        {"role": "user", "content": None},
        {"role": "user", "content": "hello"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "hello"


def test_missing_content_key():
    msgs = [
        {"role": "assistant"},
        {"role": "assistant", "content": "text"},
    ]
    result = _merge_consecutive_same_role(msgs)
    assert len(result) == 1
    assert result[0]["content"] == "text"


def test_does_not_mutate_input():
    """The function mutates the first message in a run (for efficiency),
    but should not change the length of the original list."""
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    original_len = len(msgs)
    _merge_consecutive_same_role(msgs)
    assert len(msgs) == original_len


def test_warning_logged_on_merge(caplog):
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    with caplog.at_level(logging.WARNING):
        _merge_consecutive_same_role(msgs)
    assert any("wire-sanitize" in r.message for r in caplog.records)


def test_realistic_glm_scenario():
    """Simulate the pathological case: assistant text, then assistant
    tool_calls, producing two consecutive assistant messages."""
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": "let me think..."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "search", "arguments": '{"q":"test"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "found it"},
        {"role": "assistant", "content": "here is the answer"},
    ]
    result = _merge_consecutive_same_role(msgs)
    # system, user, merged-assistant, tool, assistant
    assert len(result) == 5
    merged = result[2]
    assert merged["role"] == "assistant"
    assert merged["content"] == "let me think..."
    assert merged["tool_calls"][0]["id"] == "call_1"
    # tool and final assistant untouched
    assert result[3]["role"] == "tool"
    assert result[4]["role"] == "assistant"
    assert result[4]["content"] == "here is the answer"
