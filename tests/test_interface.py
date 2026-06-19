"""Tests for ChatInterface.pop_orphan_tool_call()."""
from __future__ import annotations

from lingtai_kernel.llm.interface import (
    ChatInterface,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def test_pop_orphan_tool_call_removes_trailing_assistant_with_tool_calls():
    """Trailing assistant entry with ToolCallBlocks should be popped."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([
        TextBlock(text="Let me check."),
        ToolCallBlock(id="tc1", name="bash", args={"command": "ls"}),
    ])
    assert len(iface.entries) == 3

    removed = iface.pop_orphan_tool_call()

    assert removed is True
    assert len(iface.entries) == 2  # system, user


def test_pop_orphan_tool_call_also_removes_trailing_tool_results():
    """If tool results follow the orphan assistant, pop both."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([
        ToolCallBlock(id="tc1", name="bash", args={"command": "ls"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="tc1", name="bash", content="file.txt"),
    ])
    assert len(iface.entries) == 4

    removed = iface.pop_orphan_tool_call()

    assert removed is True
    assert len(iface.entries) == 2  # system, user


def test_pop_orphan_tool_call_noop_when_clean():
    """No orphan -- should not pop anything."""
    iface = ChatInterface()
    iface.add_system("prompt")
    iface.add_user_message("hello")
    iface.add_assistant_message([TextBlock(text="Hi there!")])

    removed = iface.pop_orphan_tool_call()

    assert removed is False
    assert len(iface.entries) == 3


def test_pop_orphan_tool_call_noop_on_empty():
    """Empty interface -- should not crash."""
    iface = ChatInterface()

    removed = iface.pop_orphan_tool_call()

    assert removed is False


# ---------------------------------------------------------------------------
# remove_pair_by_call_id — strict-shape removal of a synthesized pair
# ---------------------------------------------------------------------------


def _seed_strict_pair(iface: ChatInterface, call_id: str, name: str = "soul",
                      args: dict | None = None, content=None) -> None:
    """Append the canonical strict (assistant{tool_call}, user{tool_result})
    pair shape that remove_pair_by_call_id is meant to recognize."""
    iface.add_assistant_message([
        ToolCallBlock(id=call_id, name=name, args=args or {"action": "flow"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id=call_id, name=name, content=content or {"voice": "x"}),
    ])


def test_remove_pair_by_call_id_strict_match():
    iface = ChatInterface()
    iface.add_system("sys")
    iface.add_user_message("hi")
    iface.add_assistant_message([TextBlock(text="ok")])
    _seed_strict_pair(iface, "tc_a")
    iface.add_user_message("more")

    n_before = len(iface.entries)
    removed = iface.remove_pair_by_call_id("tc_a")
    assert removed is True
    assert len(iface.entries) == n_before - 2
    # The surrounding entries are preserved.
    assert iface.entries[-1].role == "user"
    assert iface.entries[-1].content[0].text == "more"


def test_remove_pair_by_call_id_returns_false_when_missing():
    iface = ChatInterface()
    iface.add_system("sys")
    iface.add_user_message("hi")
    iface.add_assistant_message([TextBlock(text="ok")])

    n_before = len(iface.entries)
    removed = iface.remove_pair_by_call_id("tc_missing")
    assert removed is False
    assert len(iface.entries) == n_before


def test_remove_pair_by_call_id_allows_text_plus_tool_call():
    """An assistant entry containing one ToolCallBlock plus TextBlocks IS
    accepted for backward compatibility with older synthesized pairs (see
    ChatInterface.remove_pair_by_call_id docstring). The user entry must
    still be exactly one ToolResultBlock."""
    iface = ChatInterface()
    iface.add_system("sys")
    iface.add_user_message("hi")
    iface.add_assistant_message([
        TextBlock(text="thinking aloud"),
        ToolCallBlock(id="tc_real", name="bash", args={"cmd": "ls"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="tc_real", name="bash", content="file.txt"),
    ])

    n_before = len(iface.entries)
    removed = iface.remove_pair_by_call_id("tc_real")
    assert removed is True
    assert len(iface.entries) == n_before - 2


def test_remove_pair_by_call_id_refuses_multiple_tool_calls():
    """An assistant entry with more than one ToolCallBlock is NOT a
    synthesized appendix pair shape (those carry exactly one call), so
    refuse to remove it. Protects parallel-tool-call history from
    accidental id collisions with appendix mechanisms."""
    iface = ChatInterface()
    iface.add_system("sys")
    iface.add_user_message("hi")
    iface.add_assistant_message([
        ToolCallBlock(id="tc_real", name="bash", args={"cmd": "ls"}),
        ToolCallBlock(id="tc_other", name="bash", args={"cmd": "pwd"}),
    ])
    iface.add_tool_results([
        ToolResultBlock(id="tc_real", name="bash", content="file.txt"),
        ToolResultBlock(id="tc_other", name="bash", content="/tmp"),
    ])

    n_before = len(iface.entries)
    removed = iface.remove_pair_by_call_id("tc_real")
    assert removed is False
    assert len(iface.entries) == n_before


def test_remove_pair_by_call_id_only_first_match():
    """Pair may appear at most once for the soul-flow use case, but defend
    against duplicates: only the first match is removed. (Caller can call
    again to remove subsequent matches if desired.)"""
    iface = ChatInterface()
    _seed_strict_pair(iface, "tc_a")
    iface.add_user_message("between")
    _seed_strict_pair(iface, "tc_a")

    n_before = len(iface.entries)
    removed = iface.remove_pair_by_call_id("tc_a")
    assert removed is True
    assert len(iface.entries) == n_before - 2
    # A second matching pair survives — caller can remove it explicitly.
    assert iface.remove_pair_by_call_id("tc_a") is True


def test_remove_pair_by_call_id_empty_interface():
    iface = ChatInterface()
    assert iface.remove_pair_by_call_id("tc_x") is False
