"""Tests for ChatInterface.remove_pair_by_notif_id — the dismiss helper for
system-notification synthetic pairs."""
from __future__ import annotations

from lingtai_kernel.llm.interface import (
    ChatInterface, ToolCallBlock, ToolResultBlock, TextBlock,
)


def _make_chat_with_notification_pair(notif_id: str, call_id: str) -> ChatInterface:
    """Build a minimal ChatInterface with one synthetic notification pair."""
    chat = ChatInterface()
    chat.add_assistant_message(content=[
        ToolCallBlock(
            id=call_id,
            name="system",
            args={
                "action": "notification",
                "notif_id": notif_id,
                "source": "email",
                "ref_id": "mail_abc",
            },
        )
    ])
    chat.add_tool_results([
        ToolResultBlock(id=call_id, name="system", content="[system] new mail ..."),
    ])
    return chat


def test_remove_pair_by_notif_id_removes_matching_pair():
    chat = _make_chat_with_notification_pair("notif_xxx", "call_001")
    assert chat.remove_pair_by_notif_id("notif_xxx") is True
    # Both entries gone — chat is empty (no system entry was added).
    assert len(chat.conversation_entries()) == 0


def test_remove_pair_by_notif_id_unknown_id_returns_false():
    chat = _make_chat_with_notification_pair("notif_xxx", "call_001")
    assert chat.remove_pair_by_notif_id("notif_nonexistent") is False
    # Pair still present.
    assert len(chat.conversation_entries()) == 2


def test_remove_pair_by_notif_id_idempotent():
    chat = _make_chat_with_notification_pair("notif_xxx", "call_001")
    assert chat.remove_pair_by_notif_id("notif_xxx") is True
    assert chat.remove_pair_by_notif_id("notif_xxx") is False  # already gone


def test_remove_pair_by_notif_id_skips_non_matching_args_action():
    """An assistant tool_call without args.action='notification' or with a
    different notif_id should not match. Defensive — we don't want to
    accidentally remove regular tool-calls that happen to have a 'notif_id'
    field for some other reason."""
    chat = ChatInterface()
    chat.add_assistant_message(content=[
        ToolCallBlock(
            id="call_001",
            name="system",
            args={"action": "nap"},  # not a notification
        )
    ])
    chat.add_tool_results([
        ToolResultBlock(id="call_001", name="system", content="ok"),
    ])
    # Even if caller passes a notif_id that happens to be empty, no match.
    assert chat.remove_pair_by_notif_id("notif_xxx") is False
    assert len(chat.conversation_entries()) == 2


def test_remove_pair_by_notif_id_handles_only_notification_pairs():
    """Mixed history: one regular tool_call + one notification pair.
    Only the notification pair gets removed."""
    chat = ChatInterface()
    # Regular tool call (e.g. agent calling email.check)
    chat.add_assistant_message(content=[
        ToolCallBlock(id="call_real", name="email", args={"action": "check"}),
    ])
    chat.add_tool_results([
        ToolResultBlock(id="call_real", name="email", content="{...}"),
    ])
    # Synthetic notification pair
    chat.add_assistant_message(content=[
        ToolCallBlock(
            id="call_notif",
            name="system",
            args={
                "action": "notification",
                "notif_id": "notif_xxx",
                "source": "email",
                "ref_id": "mail_abc",
            },
        )
    ])
    chat.add_tool_results([
        ToolResultBlock(id="call_notif", name="system", content="..."),
    ])

    assert chat.remove_pair_by_notif_id("notif_xxx") is True
    # Regular tool-call pair still present.
    entries = chat.conversation_entries()
    assert len(entries) == 2
    # First entry should be the assistant tool_call for email.check.
    assert entries[0].role == "assistant"
    assert entries[0].content[0].name == "email"
