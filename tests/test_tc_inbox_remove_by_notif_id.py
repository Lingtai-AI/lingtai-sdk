"""Tests for TCInbox.remove_by_notif_id — used by the dismiss handler to
cover the race where a notification is dismissed before it has been
spliced into the wire chat (still queued in tc_inbox)."""
from __future__ import annotations

from lingtai_kernel.tc_inbox import TCInbox, InvoluntaryToolCall
from lingtai_kernel.llm.interface import ToolCallBlock, ToolResultBlock


def _make_notification_item(notif_id: str, call_id: str) -> InvoluntaryToolCall:
    return InvoluntaryToolCall(
        call=ToolCallBlock(
            id=call_id,
            name="system",
            args={
                "action": "notification",
                "notif_id": notif_id,
                "source": "email",
                "ref_id": "mail_abc",
            },
        ),
        result=ToolResultBlock(id=call_id, name="system", content="..."),
        source=f"system.notification:{notif_id}",
        enqueued_at=0.0,
        coalesce=False,
        replace_in_history=False,
    )


def test_remove_by_notif_id_removes_matching_item():
    inbox = TCInbox()
    inbox.enqueue(_make_notification_item("notif_xxx", "call_001"))
    inbox.enqueue(_make_notification_item("notif_yyy", "call_002"))
    assert inbox.remove_by_notif_id("notif_xxx") is True
    assert len(inbox) == 1
    remaining = inbox.drain()
    assert remaining[0].call.args["notif_id"] == "notif_yyy"


def test_remove_by_notif_id_unknown_id_returns_false():
    inbox = TCInbox()
    inbox.enqueue(_make_notification_item("notif_xxx", "call_001"))
    assert inbox.remove_by_notif_id("notif_nonexistent") is False
    assert len(inbox) == 1


def test_remove_by_notif_id_idempotent():
    inbox = TCInbox()
    inbox.enqueue(_make_notification_item("notif_xxx", "call_001"))
    assert inbox.remove_by_notif_id("notif_xxx") is True
    assert inbox.remove_by_notif_id("notif_xxx") is False


def test_remove_by_notif_id_skips_non_notification_items():
    """A queued item whose call is not a notification (e.g. soul.flow) must
    not match even if a caller passes a string equal to some unrelated arg."""
    inbox = TCInbox()
    soul_item = InvoluntaryToolCall(
        call=ToolCallBlock(
            id="call_soul",
            name="soul",
            args={"action": "flow", "fire_id": "fire_xxx"},
        ),
        result=ToolResultBlock(id="call_soul", name="soul", content="..."),
        source="soul.flow",
        enqueued_at=0.0,
        coalesce=True,
        replace_in_history=True,
    )
    inbox.enqueue(soul_item)
    assert inbox.remove_by_notif_id("fire_xxx") is False  # fire_id is not notif_id
    assert len(inbox) == 1
