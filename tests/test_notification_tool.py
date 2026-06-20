"""Tests for the standalone ``notification`` intrinsic.

The notification tool is the **only** agent-callable home for the notification
verbs.  ``system`` exposes no notification or dismiss verb — there are no
compatibility aliases.  Dismissal is **atomic**:

* ``notification(action="check")`` returns a placeholder dict that the
  meta-block later stamps the live payload onto;
* ``notification(action="dismiss_channel", channel=...)`` clears one channel
  whole and refuses ``event_id``/``ref_id``;
* ``notification(action="dismiss_event", event_id=..., [channel="system"])``
  removes one ``system`` event;
* ``notification(action="dismiss_ref", ref_id=..., [channel="system"])``
  removes ``system`` event(s) by ``ref_id``.

``summarize`` is NOT here — it stays a ``system`` action.

#424 semantics (``large_tool_result`` reminders undismissable; only a
successful ``system(action="summarize")`` clears them) must hold through every
atomic notification action, including ``force``.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lingtai_kernel.intrinsics import (
    ALL_INTRINSICS,
    notification as notif_intrinsic,
    system as sys_intrinsic,
)
from lingtai_kernel.notifications import (
    collect_notifications,
    notification_fingerprint,
    publish,
)


# ---------------------------------------------------------------------------
# Stub agent — mirrors the one used by test_system_dismiss.py.
# ---------------------------------------------------------------------------


@dataclass
class _StubAgent:
    _working_dir: Path
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _notification_fp: tuple = ()
    _pending_notification_meta: str | None = "stale"
    _pending_notification_fp: tuple | None = (("soul.json", 1, 2),)
    _system_notification_lock: threading.Lock = field(default_factory=threading.Lock)

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _events(agent: _StubAgent, name: str) -> list[dict]:
    return [fields for event, fields in agent._logs if event == name]


def _mark_delivered(agent: _StubAgent) -> None:
    agent._notification_fp = notification_fingerprint(agent._working_dir)


def _publish_large_result_reminder(
    tmp_path: Path,
    *,
    tool_call_id: str = "toolu_big",
    extra_events: list[dict] | None = None,
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "event_id": "evt_lr",
            "source": "large_tool_result",
            "ref_id": f"large_tool_result:{tool_call_id}",
            "body": "summarize me",
        }
    ]
    if extra_events:
        events = list(extra_events) + events
    publish(
        tmp_path,
        "system",
        {
            "header": f"{len(events)} system notifications",
            "icon": "🔔",
            "priority": "normal",
            "published_at": "2026-06-20T00:00:00Z",
            "data": {"events": events},
        },
    )


# ---------------------------------------------------------------------------
# Mandatory include + schema availability.
# ---------------------------------------------------------------------------


def test_notification_is_registered_like_system() -> None:
    """The notification intrinsic is in ALL_INTRINSICS — wired for every agent."""
    assert "notification" in ALL_INTRINSICS
    assert ALL_INTRINSICS["notification"]["module"] is notif_intrinsic


def test_notification_wired_into_every_agent() -> None:
    """_wire_intrinsics iterates ALL_INTRINSICS unconditionally → mandatory.

    There is no manifest gate: every key in ALL_INTRINSICS is wired into
    agent._intrinsics. Proving 'notification' lands there alongside 'system'
    is the mandatory-include proof.
    """
    from lingtai_kernel.base_agent import BaseAgent

    wired: dict[str, Any] = {}

    class _FakeAgent:
        _intrinsics = wired

        def _log(self, *a, **k):
            pass

    BaseAgent._wire_intrinsics(_FakeAgent())  # type: ignore[arg-type]

    assert "system" in wired
    assert "notification" in wired
    assert callable(wired["notification"])


def test_notification_schema_exposes_atomic_actions() -> None:
    schema = notif_intrinsic.get_schema("en")
    assert schema["properties"]["action"]["enum"] == [
        "check",
        "dismiss_channel",
        "dismiss_event",
        "dismiss_ref",
    ]
    assert schema["required"] == ["action"]
    # Dismiss params present; summarize 'items' must NOT be here.
    for key in ("channel", "force", "event_id", "ref_id", "reason"):
        assert key in schema["properties"]
    assert "items" not in schema["properties"]


def test_notification_schema_has_no_kitchen_sink_dismiss() -> None:
    """The non-atomic aggregate 'dismiss' / 'summarize' are not actions here."""
    enum = notif_intrinsic.get_schema("en")["properties"]["action"]["enum"]
    assert "dismiss" not in enum
    assert "summarize" not in enum


def test_notification_schema_localized() -> None:
    for lang in ("en", "zh", "wen"):
        desc = notif_intrinsic.get_description(lang)
        assert desc and desc != "notification_tool.description"
        adesc = notif_intrinsic.get_schema(lang)["properties"]["action"]["description"]
        assert adesc and adesc != "notification_tool.action_description"
        # Notification-owned param strings resolve (not the raw key).
        cdesc = notif_intrinsic.get_schema(lang)["properties"]["channel"]["description"]
        assert cdesc and cdesc != "notification_tool.channel_description"


# ---------------------------------------------------------------------------
# system no longer exposes notification / dismiss verbs.
# ---------------------------------------------------------------------------


def test_system_schema_drops_notification_and_dismiss() -> None:
    enum = sys_intrinsic.get_schema("en")["properties"]["action"]["enum"]
    assert "notification" not in enum
    assert "dismiss" not in enum
    # summarize remains a system action.
    assert "summarize" in enum
    # The dismiss-only params are gone from the system schema too.
    props = sys_intrinsic.get_schema("en")["properties"]
    for key in ("channel", "force", "event_id", "ref_id"):
        assert key not in props


def test_system_rejects_notification_action(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = sys_intrinsic.handle(agent, {"action": "notification"})
    assert res["status"] == "error"
    assert "Unknown system action" in res["message"]


def test_system_rejects_dismiss_action(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})
    _mark_delivered(agent)
    res = sys_intrinsic.handle(agent, {"action": "dismiss", "channel": "soul"})
    assert res["status"] == "error"
    assert "Unknown system action" in res["message"]
    # The channel was NOT cleared — system can't dismiss anything.
    assert "soul" in collect_notifications(tmp_path)


def test_system_module_has_no_dismiss_callable() -> None:
    assert not hasattr(sys_intrinsic, "_dismiss")


# ---------------------------------------------------------------------------
# check — placeholder shape.
# ---------------------------------------------------------------------------


def test_check_returns_placeholder_dict(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(agent, {"action": "check"})
    assert res["_notification_placeholder"] is True
    assert "notification(action=check)" in res["message"]
    assert "notifications" not in res


def test_unknown_action_errors(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(agent, {"action": "bogus"})
    assert res["status"] == "error"
    assert "Unknown notification action" in res["message"]


# ---------------------------------------------------------------------------
# dismiss_channel.
# ---------------------------------------------------------------------------


def test_dismiss_channel_clears_surface(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})
    _mark_delivered(agent)

    res = notif_intrinsic.handle(agent, {"action": "dismiss_channel", "channel": "soul"})

    assert res == {"status": "ok", "channel": "soul", "cleared": True, "forced": False}
    assert collect_notifications(tmp_path) == {}
    # Provenance: invoked_by="notification"; no system_dismiss line.
    assert _events(agent, "notification_dismiss")[0]["invoked_by"] == "notification"
    assert _events(agent, "system_dismiss") == []


def test_dismiss_channel_missing_channel(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(agent, {"action": "dismiss_channel"})
    assert res["status"] == "error"
    assert res["reason"] == "missing_channel"


def test_dismiss_channel_rejects_event_target(tmp_path: Path) -> None:
    """dismiss_channel must not accept event_id/ref_id — those are atomic verbs."""
    agent = _StubAgent(tmp_path)
    for kwargs in ({"event_id": "evt_a"}, {"ref_id": "goal:current"}):
        res = notif_intrinsic.handle(
            agent, {"action": "dismiss_channel", "channel": "system", **kwargs}
        )
        assert res["status"] == "error", kwargs
        assert res["reason"] == "channel_dismiss_rejects_event_target", kwargs


# ---------------------------------------------------------------------------
# dismiss_event.
# ---------------------------------------------------------------------------


def test_dismiss_event_removes_one(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {
            "header": "2 system notifications",
            "data": {
                "events": [
                    {"event_id": "evt_a", "source": "daemon", "ref_id": "a", "body": "A"},
                    {"event_id": "evt_b", "source": "daemon", "ref_id": "b", "body": "B"},
                ]
            },
        },
    )
    _mark_delivered(agent)

    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_event", "event_id": "evt_b"}
    )

    assert res["status"] == "ok"
    assert res["removed"] == 1
    events = collect_notifications(tmp_path)["system"]["data"]["events"]
    assert [e["event_id"] for e in events] == ["evt_a"]


def test_dismiss_event_missing_event_id(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(agent, {"action": "dismiss_event"})
    assert res["status"] == "error"
    assert res["reason"] == "missing_event_id"


def test_dismiss_event_defaults_to_system_channel(tmp_path: Path) -> None:
    """No channel given → operates on the 'system' channel."""
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {"data": {"events": [{"event_id": "evt_a", "source": "daemon", "ref_id": "a"}]}},
    )
    _mark_delivered(agent)
    res = notif_intrinsic.handle(agent, {"action": "dismiss_event", "event_id": "evt_a"})
    assert res["status"] == "ok"
    assert res["channel"] == "system"
    assert res["removed"] == 1


# ---------------------------------------------------------------------------
# dismiss_ref.
# ---------------------------------------------------------------------------


def test_dismiss_ref_removes_by_ref(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(
        tmp_path,
        "system",
        {"data": {"events": [{"event_id": "evt_a", "source": "goal.reminder", "ref_id": "goal:current"}]}},
    )
    _mark_delivered(agent)

    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_ref", "ref_id": "goal:current"}
    )

    assert res["status"] == "ok"
    assert res["removed"] == 1
    assert not (tmp_path / ".notification" / "system.json").exists()


def test_dismiss_ref_missing_ref_id(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(agent, {"action": "dismiss_ref"})
    assert res["status"] == "error"
    assert res["reason"] == "missing_ref_id"


# ---------------------------------------------------------------------------
# #424: large_tool_result guard via EVERY atomic action, including force.
# ---------------------------------------------------------------------------


def test_large_result_guard_dismiss_channel(tmp_path: Path) -> None:
    """large_tool_result reminders can now be dismissed as an escape hatch."""
    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path)
    _mark_delivered(agent)

    res = notif_intrinsic.handle(agent, {"action": "dismiss_channel", "channel": "system"})

    assert res["status"] == "ok"
    assert "acked_large_result_refs" in res
    # The event is removed from the channel.
    notifs = collect_notifications(tmp_path)
    events = notifs.get("system", {}).get("data", {}).get("events", [])
    assert not any(ev["source"] == "large_tool_result" for ev in events)


def test_large_result_guard_dismiss_channel_force(tmp_path: Path) -> None:
    """large_tool_result reminders can be dismissed with force — same result."""
    agent = _StubAgent(tmp_path)
    _publish_large_result_reminder(tmp_path)
    _mark_delivered(agent)

    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": "system", "force": True}
    )

    assert res["status"] == "ok"
    assert "acked_large_result_refs" in res
    notifs = collect_notifications(tmp_path)
    events = notifs.get("system", {}).get("data", {}).get("events", [])
    assert not any(ev["source"] == "large_tool_result" for ev in events)


def test_large_result_guard_every_atomic_action(tmp_path: Path) -> None:
    """All atomic actions — channel/event/ref, with or without force — now succeed
    for large_tool_result reminders (escape hatch behaviour from issue #425)."""
    cases = [
        {"action": "dismiss_channel", "channel": "system"},
        {"action": "dismiss_channel", "channel": "system", "force": True},
        {"action": "dismiss_event", "event_id": "evt_lr"},
        {"action": "dismiss_event", "event_id": "evt_lr", "force": True},
        {"action": "dismiss_ref", "ref_id": "large_tool_result:toolu_big"},
        {"action": "dismiss_ref", "ref_id": "large_tool_result:toolu_big", "force": True},
    ]
    for kwargs in cases:
        agent = _StubAgent(tmp_path / json.dumps(kwargs, sort_keys=True))
        _publish_large_result_reminder(agent._working_dir)
        _mark_delivered(agent)
        res = notif_intrinsic.handle(agent, kwargs)
        assert res["status"] == "ok", (kwargs, res)
        assert "acked_large_result_refs" in res, (kwargs, res)
        notifs = collect_notifications(agent._working_dir)
        events = notifs.get("system", {}).get("data", {}).get("events", [])
        assert not any(ev["source"] == "large_tool_result" for ev in events), kwargs


# ---------------------------------------------------------------------------
# summarize is NOT a notification action; the system one still auto-clears.
# ---------------------------------------------------------------------------


def test_summarize_is_not_a_notification_action(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    res = notif_intrinsic.handle(
        agent, {"action": "summarize", "items": [{"tool_call_id": "x", "summary": "y"}]}
    )
    assert res["status"] == "error"
    assert "Unknown notification action" in res["message"]


class _Block:
    def __init__(self, block_id: str, name: str, content: Any) -> None:
        self.id = block_id
        self.name = name
        self.content = content


class _Entry:
    def __init__(self, role: str, content: list) -> None:
        self.role = role
        self.content = content


class _Interface:
    def __init__(self, entries: list) -> None:
        self._entries = entries


class _Chat:
    def __init__(self, entries: list) -> None:
        self.interface = _Interface(entries)


@dataclass
class _SummarizeAgent:
    _working_dir: Path
    _chat: Any = None
    _logs: list = field(default_factory=list)
    _summarize_notification_threshold: int = 3000
    _system_notification_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_notification_meta: Any = None
    _pending_notification_fp: Any = None

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))

    def _save_chat_history(self, ledger_source: str | None = None) -> None:
        pass


def _make_summarize_agent(tmp_path: Path, tool_call_id: str) -> _SummarizeAgent:
    from lingtai_kernel.llm.interface import ToolResultBlock

    block = ToolResultBlock(id=tool_call_id, name="read", content="x" * 5000)
    entry = _Entry("user", [block])
    agent = _SummarizeAgent(_working_dir=tmp_path)
    agent._chat = _Chat([entry])
    return agent


def test_system_summarize_success_clears_large_result_reminder(tmp_path: Path) -> None:
    tool_call_id = "toolu_sum_ok"
    agent = _make_summarize_agent(tmp_path, tool_call_id)
    _publish_large_result_reminder(tmp_path, tool_call_id=tool_call_id)

    res = sys_intrinsic.handle(
        agent,
        {
            "action": "summarize",
            "items": [{"tool_call_id": tool_call_id, "summary": "digested"}],
        },
    )

    assert res["status"] == "ok"
    assert res["summarized"] == 1
    assert f"large_tool_result:{tool_call_id}" in res["cleared_reminders"]
    assert not (tmp_path / ".notification" / "system.json").exists()


def test_system_summarize_failure_does_not_clear_reminder(tmp_path: Path) -> None:
    reminder_tcid = "toolu_real"
    agent = _make_summarize_agent(tmp_path, "toolu_real")
    _publish_large_result_reminder(tmp_path, tool_call_id=reminder_tcid)

    res = sys_intrinsic.handle(
        agent,
        {
            "action": "summarize",
            "items": [{"tool_call_id": "toolu_missing", "summary": "nope"}],
        },
    )

    assert res["status"] == "error"
    assert res["summarized"] == 0
    assert res["failed"] == 1
    assert res["cleared_reminders"] == []
    events = collect_notifications(tmp_path)["system"]["data"]["events"]
    assert any(
        ev["ref_id"] == f"large_tool_result:{reminder_tcid}" for ev in events
    )


# ---------------------------------------------------------------------------
# Producer ownership + stale/force + protected boundaries via the new tool.
# ---------------------------------------------------------------------------


def test_guarded_channel_refuses_without_force(tmp_path: Path) -> None:
    import lingtai_kernel.intrinsics.email  # noqa: F401 — registers the guard

    agent = _StubAgent(tmp_path)
    publish(tmp_path, "email", {"header": "1 unread"})

    res = notif_intrinsic.handle(agent, {"action": "dismiss_channel", "channel": "email"})

    assert res["status"] == "error"
    assert res["reason"] == "guarded"
    # Producer surface untouched (canonical state separate from mirror).
    assert "email" in collect_notifications(tmp_path)
    assert _events(agent, "notification_dismiss_guarded")[0]["invoked_by"] == "notification"


def test_stale_channel_refused_without_force(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "system", {"header": "one", "data": {"events": ["old"]}})
    _mark_delivered(agent)
    publish(tmp_path, "system", {"header": "two", "data": {"events": ["old", "new"]}})

    res = notif_intrinsic.handle(agent, {"action": "dismiss_channel", "channel": "system"})

    assert res["status"] == "error"
    assert res["reason"] == "stale_channel_version"
    assert collect_notifications(tmp_path)["system"]["header"] == "two"


def test_force_bypasses_stale_on_allowed_channel(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "system", {"header": "one", "data": {"events": ["old"]}})
    _mark_delivered(agent)
    publish(tmp_path, "system", {"header": "two", "data": {"events": ["old", "new"]}})

    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": "system", "force": True}
    )

    assert res["status"] == "ok"
    assert res["forced"] is True
    assert "system" not in collect_notifications(tmp_path)


def test_protected_goal_channel_refused(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "goal", {"data": {"status": "active"}})
    agent._notification_fp = notification_fingerprint(tmp_path)

    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": "goal", "force": True}
    )

    assert res["status"] == "error"
    assert res["reason"] == "protected_channel"
    assert (tmp_path / ".notification" / "goal.json").exists()


def test_post_molt_dismiss_requires_reason(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "post-molt", {"header": "continue?"})
    _mark_delivered(agent)
    res = notif_intrinsic.handle(
        agent, {"action": "dismiss_channel", "channel": "post-molt"}
    )
    assert res["status"] == "error"
    assert res["reason"] == "missing_ack_reason"

    res2 = notif_intrinsic.handle(
        agent,
        {"action": "dismiss_channel", "channel": "post-molt", "reason": "continue: done"},
    )
    assert res2["status"] == "ok"
    assert res2["reason"] == "continue: done"
