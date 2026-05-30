"""Tests for ``system(action='dismiss', channel=...)``.

Generic dismiss clears one `.notification/<channel>.json` file while
preserving producer-specific state semantics. Legacy ``ids=`` calls are still
accepted for one release cycle so old chat-history tails do not crash.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai_kernel.intrinsics import system as sys_intrinsic
from lingtai_kernel.notifications import (
    collect_notifications,
    is_generic_dismiss_guarded,
    publish,
)


@dataclass
class _StubAgent:
    _working_dir: Path
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _pending_notification_meta: str | None = "stale"
    _pending_notification_fp: tuple | None = (("soul.json", 1, 2),)

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def _events(agent: _StubAgent, name: str) -> list[dict]:
    return [fields for event, fields in agent._logs if event == name]


def test_dismiss_channel_clears_existing_file(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})

    res = sys_intrinsic._dismiss(agent, {"channel": "soul"})

    assert res == {"status": "ok", "channel": "soul", "cleared": True, "forced": False}
    assert collect_notifications(tmp_path) == {}
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None
    assert _events(agent, "notification_dismiss")[0]["channel"] == "soul"
    assert _events(agent, "system_dismiss")[0]["existed"] is True


def test_dismiss_channel_is_idempotent_when_absent(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)

    res = sys_intrinsic._dismiss(agent, {"channel": "soul"})

    assert res["status"] == "ok"
    assert res["cleared"] is False
    assert res["channel"] == "soul"
    assert agent._pending_notification_meta is None
    assert agent._pending_notification_fp is None


def test_dismiss_mcp_dotted_channel(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "mcp.telegram", {"header": "telegram event"})

    res = sys_intrinsic.handle(agent, {"action": "dismiss", "channel": "mcp.telegram"})

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert "mcp.telegram" not in collect_notifications(tmp_path)


def test_dismiss_validation_errors(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)

    missing = sys_intrinsic._dismiss(agent, {})
    assert missing["status"] == "error"
    assert missing["reason"] == "missing_channel"

    for bad in ["", "../escape", "..hidden", "bad/slash"]:
        res = sys_intrinsic._dismiss(agent, {"channel": bad})
        assert res["status"] == "error"
        assert res["reason"] == "invalid_channel"


def test_legacy_ids_path_is_accepted_but_ignored(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "still here"})

    res = sys_intrinsic._dismiss(agent, {"ids": ["notif_xxx"]})

    assert res["status"] == "ok"
    assert res["cleared"] is False
    assert "legacy ids ignored" in res["note"]
    assert "soul" in collect_notifications(tmp_path)
    assert _events(agent, "system_dismiss_legacy_ids_ignored")[0]["ids"] == ["notif_xxx"]


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

    res = sys_intrinsic._dismiss(agent, {"channel": "email"})

    assert res["status"] == "error"
    assert res["reason"] == "guarded"
    assert "email_id" in res["message"]
    assert "email" in collect_notifications(tmp_path)
    assert _events(agent, "system_dismiss_guarded")


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

    res = sys_intrinsic._dismiss(agent, {"channel": "email", "force": True})

    assert res["status"] == "ok"
    assert res["cleared"] is True
    assert res["forced"] is True
    assert "email" not in collect_notifications(agent.working_dir)

    check = agent._email_manager.handle({"action": "check"})
    assert check["total"] == 1
    assert check["emails"][0]["unread"] is True


def test_soul_dismiss_alias_uses_shared_helper(tmp_path: Path) -> None:
    from lingtai_kernel.intrinsics import soul

    agent = _StubAgent(tmp_path)
    publish(tmp_path, "soul", {"header": "soul flow"})

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

    res = sys_intrinsic._dismiss(agent, {"channel": "soul"})

    assert res["status"] == "ok"
    out = collect_notifications(tmp_path)
    assert set(out) == {"email"}


def test_post_molt_dismiss_requires_ack_reason(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "post-molt", {"header": "resume work"})

    res = sys_intrinsic._dismiss(agent, {"channel": "post-molt"})

    assert res["status"] == "error"
    assert res["reason"] == "missing_ack_reason"
    assert "post-molt" in collect_notifications(tmp_path)
    assert _events(agent, "notification_dismiss_missing_reason")[0]["channel"] == "post-molt"


def test_post_molt_dismiss_records_ack_reason(tmp_path: Path) -> None:
    agent = _StubAgent(tmp_path)
    publish(tmp_path, "post-molt", {"header": "resume work"})

    res = sys_intrinsic._dismiss(agent, {
        "channel": "post-molt",
        "reason": "continue: resumed the preserved task",
    })

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
    assert _events(agent, "system_dismiss")[0]["reason"] == \
        "continue: resumed the preserved task"
