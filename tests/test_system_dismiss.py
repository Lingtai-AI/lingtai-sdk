"""Tests for notification dismissal — the kernel ``dismiss_channel`` helper as
exercised through the standalone ``notification`` tool.

The ``system`` tool no longer exposes any dismiss/notification verb (see
``test_notification_tool.py`` for the no-compatibility regression anchors).
Dismissal is atomic on the ``notification`` tool:

* ``notification(action="dismiss_channel", channel=...)`` → whole-channel clear,
* ``notification(action="dismiss_event", event_id=..., [channel="system"])``,
* ``notification(action="dismiss_ref", ref_id=..., [channel="system"])``.

The ``soul(action="dismiss")`` convenience alias still routes through the same
shared helper with ``invoked_by="soul"``. Generic dismiss clears one
``.notification/<channel>.json`` file while preserving producer-specific state
semantics.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai_kernel.intrinsics import notification as notif_intrinsic
from lingtai_kernel.notifications import (
    collect_notifications,
    is_generic_dismiss_guarded,
    notification_fingerprint,
    publish,
)

# Shared with test_notification_tool.py — see tests/_notification_helpers.py.
from tests._notification_helpers import (
    StubAgent as _StubAgent,
    events as _events,
    mark_delivered as _mark_delivered,
    publish_large_result_reminder as _publish_large_result_reminder,
)


class _RecordingLock:
    def __init__(self) -> None:
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_exc) -> None:
        return None


def _dismiss_channel(agent, channel, **kwargs):
    return notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": channel, **kwargs}
    )


def _dismiss_event(agent, **kwargs):
    return notif_intrinsic.handle(agent, {"action": "dismiss_event", **kwargs})


def _dismiss_ref(agent, **kwargs):
    return notif_intrinsic.handle(agent, {"action": "dismiss_ref", **kwargs})


def test_dismiss_channel_clears_existing_file(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "soul")

    assert res == {"status": "ok", "channel": "soul", "cleared": True, "forced": False}
    assert collect_notifications(tmp_path) == {}
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None
    nd = _events(agent, "notification_dismiss")[0]
    assert nd["channel"] == "soul"
    assert nd["existed"] is True
    assert nd["invoked_by"] == "notification"
    # The system-tool extra log line is never emitted by the notification path.
    assert _events(agent, "system_dismiss") == []


def test_dismiss_channel_is_idempotent_when_absent(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)

    res = _dismiss_channel(agent, "soul")

    assert res["status"] == "ok"
    assert res["cleared"] is False
    assert res["channel"] == "soul"
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None


def test_dismiss_mcp_dotted_channel(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "mcp.telegram", {"header": "telegram event"})
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "mcp.telegram")

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert "mcp.telegram" not in collect_notifications(tmp_path)


def test_dismiss_validation_errors(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)

    missing = notif_intrinsic.handle(agent, {"action": "dismiss_channel"})
    assert missing["status"] == "error"
    assert missing["reason"] == "missing_channel"

    for bad in ["", "../escape", "..hidden", "bad/slash"]:
        res = _dismiss_channel(agent, bad)
        assert res["status"] == "error"
        # Empty string channel is treated as missing by the tool guard;
        # syntactically-invalid names reach the kernel allowlist check.
        assert res["reason"] in ("invalid_channel", "missing_channel")


def test_email_registers_generic_dismiss_guard() -> None:
    import lingtai_kernel.intrinsics.email  # noqa: F401 - import performs registration

    suggestion = is_generic_dismiss_guarded("email")
    assert suggestion is not None
    assert "email(action='dismiss'" in suggestion
    assert is_generic_dismiss_guarded("soul") is None


def test_guarded_email_refuses_without_force(tmp_path: Path) -> None:
    import lingtai_kernel.intrinsics.email  # noqa: F401 - import performs registration

    agent = _StubAgent(tmp_path)
    publish(tmp_path, "email", {"header": "1 unread"})

    res = _dismiss_channel(agent, "email")

    assert res["status"] == "error"
    assert res["reason"] == "guarded"
    assert "email_id" in res["message"]
    assert "email" in collect_notifications(tmp_path)
    # Provenance is notification, not system.
    assert _events(agent, "notification_dismiss_guarded")
    assert _events(agent, "system_dismiss_guarded") == []


def test_guarded_email_force_clears_surface_but_not_mail_state(tmp_path: Path) -> None:
    from lingtai.agent import Agent

    import lingtai_kernel.intrinsics.email  # noqa: F401 - import performs registration

    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    agent = Agent(service=svc, agent_name="test", working_dir=tmp_path / "test")

    email_id = str(uuid4())
    msg_dir = agent.working_dir / "mailbox" / "inbox" / email_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / "message.json").write_text(json.dumps({
        "_mailbox_id": email_id,
        "from": "sender",
        "to": ["test"],
        "subject": "topic",
        "message": "body",
        "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    agent._on_mail_received({"from": "sender", "subject": "topic", "message": "body"})
    assert "email" in collect_notifications(agent.working_dir)

    res = _dismiss_channel(agent, "email", force=True)

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert res["forced"] is True
    assert "email" not in collect_notifications(agent.working_dir)

    # Producer canonical state (mailbox read-state) is untouched by a mirror clear.
    check = agent._email_manager.handle({"action": "check"})
    assert check["total"] == 1
    assert check["emails"][0]["unread"] is True


def test_soul_dismiss_alias_uses_shared_helper(tmp_path: Path) -> None:
    from lingtai_kernel.intrinsics import soul

    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})
    _mark_delivered(agent)

    res = soul.handle(agent, {"action": "dismiss"})

    assert res["status"] == "ok"
    assert res["channel"] == "soul"
    assert "soul" not in collect_notifications(tmp_path)
    assert _events(agent, "soul_dismiss") == [{}]
    assert _events(agent, "notification_dismiss")[0]["invoked_by"] == "soul"


def test_dismiss_one_channel_preserves_other_channels(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "email", {"header": "1 unread"})
    publish(tmp_path, "soul", {"header": "soul flow"})
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "soul")

    assert res["status"] == "ok"
    out = collect_notifications(tmp_path)
    assert set(out) == {"email"}


def test_post_molt_dismiss_requires_ack_reason(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "post-molt", {"header": "resume work"})

    res = _dismiss_channel(agent, "post-molt")

    assert res["status"] == "error"
    assert res["reason"] == "missing_ack_reason"
    assert "post-molt" in collect_notifications(tmp_path)
    assert _events(agent, "notification_dismiss_missing_reason")[0]["channel"] == "post-molt"


def test_post_molt_dismiss_records_ack_reason(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "post-molt", {"header": "resume work"})
    _mark_delivered(agent)

    res = _dismiss_channel(
        agent, "post-molt", reason="continue: resumed the preserved task"
    )

    assert res == {
        "status": "ok",
        "channel": "post-molt",
        "cleared": True,
        "forced": False,
        "reason": "continue: resumed the preserved task",
    }
    assert "post-molt" not in collect_notifications(tmp_path)
    assert _events(agent, "notification_dismiss")[0]["reason"] == \
        "continue: resumed the preserved task"
    # No system_dismiss line via the notification path.
    assert _events(agent, "system_dismiss") == []


def test_stale_system_dismiss_refuses_and_preserves_newer_file(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "system", {"header": "one", "data": {"events": ["old"]}})
    _mark_delivered(agent)
    delivered_fp = agent._notification_fp
    publish(
        tmp_path,
        "system",
        {"header": "two", "data": {"events": ["old", "new"], "extra": "changed"}},
    )

    res = _dismiss_channel(agent, "system")

    assert res["status"] == "error"
    assert res["reason"] == "stale_channel_version"
    assert res["channel"] == "system"
    assert res["forced"] is False
    assert res["delivered_version"] != res["current_version"]
    assert collect_notifications(tmp_path)["system"]["header"] == "two"
    assert agent._pending_notification_meta == "stale"
    assert agent._pending_notification_fp == (("soul.json", 1, 2),)
    assert agent._notification_fp == delivered_fp
    refusal = _events(agent, "notification_dismiss_refused")[0]
    assert refusal["reason"] == "stale_channel_version"
    assert refusal["invoked_by"] == "notification"


def test_force_bypasses_stale_version_guard(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "system", {"header": "one", "data": {"events": ["old"]}})
    _mark_delivered(agent)
    publish(
        tmp_path,
        "system",
        {"header": "two", "data": {"events": ["old", "new"], "extra": "changed"}},
    )

    res = _dismiss_channel(agent, "system", force=True)

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert res["forced"] is True
    assert "system" not in collect_notifications(tmp_path)
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None


def test_stale_other_channel_does_not_block_delivered_channel(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})
    publish(tmp_path, "system", {"header": "one", "data": {"events": ["old"]}})
    _mark_delivered(agent)
    publish(
        tmp_path,
        "system",
        {"header": "two", "data": {"events": ["old", "new"], "extra": "changed"}},
    )

    res = _dismiss_channel(agent, "soul")

    assert res["status"] == "ok"
    assert res["cleared"] is True
    out = collect_notifications(tmp_path)
    assert set(out) == {"system"}
    assert out["system"]["header"] == "two"


def test_never_delivered_current_channel_refuses_without_force(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "nudge", {"header": "new nudge"})

    res = _dismiss_channel(agent, "nudge")

    assert res["status"] == "error"
    assert res["reason"] == "stale_channel_version"
    assert res["delivered_version"] is None
    assert res["current_version"][0] == "nudge.json"
    assert "nudge" in collect_notifications(tmp_path)
    assert agent._pending_notification_meta == "stale"
    assert agent._pending_notification_fp == (("soul.json", 1, 2),)


def test_system_compare_and_clear_uses_system_notification_lock(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    lock = _RecordingLock()
    agent._system_notification_lock = lock
    publish(tmp_path, "system", {"header": "system event"})
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "system")

    assert res["status"] == "ok"
    assert lock.entered is True
    assert "system" not in collect_notifications(tmp_path)


def test_unknown_notification_channel_is_not_dismissible(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    result = _dismiss_channel(agent, "random")
    assert result["status"] == "error"
    assert result["reason"] == "invalid_channel"
    assert "not allowlisted" in result["message"]


def test_goal_channel_is_protected_from_generic_dismiss(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "goal", {"data": {"status": "active"}})
    agent._notification_fp = notification_fingerprint(tmp_path)

    result = _dismiss_channel(agent, "goal", force=True)

    assert result["status"] == "error"
    assert result["reason"] == "protected_channel"
    assert (tmp_path / ".notification" / "goal.json").exists()
    assert "delete .notification/goal.json" in result["message"]


def test_system_event_dismiss_by_event_id_preserves_other_events(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {
            "header": "2 system notifications",
            "icon": "🔔",
            "priority": "normal",
            "published_at": "2026-06-10T00:00:00Z",
            "data": {
                "events": [
                    {"event_id": "evt_a", "source": "daemon", "ref_id": "a", "body": "A"},
                    {"event_id": "evt_b", "source": "goal.reminder", "ref_id": "goal:current", "body": "B"},
                ]
            },
        },
    )
    agent._notification_fp = notification_fingerprint(tmp_path)

    result = _dismiss_event(agent, event_id="evt_b")

    assert result["status"] == "ok"
    assert result["removed"] == 1
    payload = collect_notifications(tmp_path)["system"]
    assert payload["header"] == "1 system notification"
    assert payload["data"]["events"] == [
        {"event_id": "evt_a", "source": "daemon", "ref_id": "a", "body": "A"}
    ]
    assert getattr(agent, "_goal_reminder_last_dismissed_at", 0) > 0


def test_system_event_dismiss_by_ref_id_clears_file_when_last_event(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {"data": {"events": [{"event_id": "evt_a", "source": "goal.reminder", "ref_id": "goal:current"}]}},
    )
    agent._notification_fp = notification_fingerprint(tmp_path)

    result = _dismiss_ref(agent, ref_id="goal:current")

    assert result["status"] == "ok"
    assert result["removed"] == 1
    assert result["remaining"] == 0
    assert not (tmp_path / ".notification" / "system.json").exists()


def test_large_result_reminder_cleared_by_whole_channel_dismiss(tmp_path: Path) -> None:
    """Whole-channel system dismiss now acks and clears large-result reminders (escape hatch)."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path)
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "system")

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert "acked_large_result_refs" in res
    assert "large_tool_result:toolu_big" in res["acked_large_result_refs"]
    # The ack is persisted.
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_big" in acks
    # Notification file removed (only event was the large-result one).
    assert "system" not in collect_notifications(tmp_path)
    # A dismiss log was emitted (not a refusal).
    assert _events(agent, "large_result_reminder_dismissed")
    assert _events(agent, "notification_dismiss_refused") == []


def test_large_result_reminder_cleared_by_force_whole_channel(tmp_path: Path) -> None:
    """force=true on a whole-channel system dismiss also acks large-result reminders."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path)
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "system", force=True)

    assert res["status"] == "ok"
    assert res["forced"] is True
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_big" in acks
    assert "system" not in collect_notifications(tmp_path)


def test_large_result_reminder_cleared_by_event_id(tmp_path: Path) -> None:
    """Targeted event_id dismiss of a large-result reminder now acks and removes it."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path)
    _mark_delivered(agent)

    res = _dismiss_event(agent, event_id="evt_lr")

    assert res["status"] == "ok"
    assert res["event_id"] == "evt_lr"
    assert "acked_large_result_refs" in res
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_big" in acks
    # Notification file removed.
    assert "system" not in collect_notifications(tmp_path)


def test_large_result_reminder_cleared_by_ref_id(tmp_path: Path) -> None:
    """Targeted ref_id dismiss of a large-result reminder now acks and removes it."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path, tool_call_id="toolu_x")
    _mark_delivered(agent)

    res = _dismiss_ref(agent, ref_id="large_tool_result:toolu_x")

    assert res["status"] == "ok"
    assert res["ref_id"] == "large_tool_result:toolu_x"
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_x" in acks
    assert "system" not in collect_notifications(tmp_path)


def test_large_result_reminder_cleared_by_force_ref_id(tmp_path: Path) -> None:
    """force=true on a targeted ref_id dismiss also acks the large-result reminder."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path, tool_call_id="toolu_x")
    _mark_delivered(agent)

    res = _dismiss_ref(agent, ref_id="large_tool_result:toolu_x", force=True)

    assert res["status"] == "ok"
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_x" in acks


def test_whole_channel_dismiss_with_large_result_and_other_events(tmp_path: Path) -> None:
    """Whole-channel dismiss acks large-result events and clears all events in the channel."""
    from lingtai_kernel.notifications import load_large_result_acks

    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(
        tmp_path,
        extra_events=[{"event_id": "evt_d", "source": "daemon", "ref_id": "d", "body": "D"}],
    )
    _mark_delivered(agent)

    res = _dismiss_channel(agent, "system")

    # Mixed events: large-result acked, whole channel cleared.
    assert res["status"] == "ok"
    acks = load_large_result_acks(tmp_path)
    assert "large_tool_result:toolu_big" in acks
    # All events cleared from the channel.
    assert "system" not in collect_notifications(tmp_path)


def test_non_protected_system_event_still_dismissible_by_event_id(tmp_path: Path) -> None:
    """Dismissing a non-large-result event by event_id still works; reminder stays."""
    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(
        tmp_path,
        extra_events=[{"event_id": "evt_d", "source": "daemon", "ref_id": "d", "body": "D"}],
    )
    _mark_delivered(agent)

    res = _dismiss_event(agent, event_id="evt_d")

    assert res["status"] == "ok"
    assert res["removed"] == 1
    events = collect_notifications(tmp_path)["system"]["data"]["events"]
    sources = {ev["source"] for ev in events}
    assert sources == {"large_tool_result"}


def test_system_event_dismiss_with_malformed_data_is_noop(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {
            "header": "malformed system notification",
            "data": ["not", "a", "dict"],
        },
    )
    agent._notification_fp = notification_fingerprint(tmp_path)

    result = _dismiss_event(agent, event_id="evt_a")

    assert result["status"] == "ok"
    assert result["removed"] == 0
    assert result["remaining"] == 0
    assert collect_notifications(tmp_path)["system"]["data"] == ["not", "a", "dict"]
