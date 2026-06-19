"""Tests for silence/kill lifecycle architecture.

Silence and kill no longer go through mail. The old mail-type-based approach
has been replaced:
- Silence/sleep use signal files (.interrupt) detected by the heartbeat loop
- Kill/annihilate use the system intrinsic karma actions
- Admin keys are now ``karma`` and ``nirvana`` (not ``silence`` and ``kill``)
- Mail type is always ``normal`` — sending type=``silence`` or ``kill``
  via mail is just treated as a normal message

This file covers internal state (_cancel_event, _admin dict), tool executor
cancel behavior, normal mail, and the new admin key semantics.
Signal file detection and karma system intrinsic actions are tested in
test_karma.py.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai.agent import Agent
from lingtai_kernel.base_agent import BaseAgent


def _persist_inbox_email(working_dir: Path, *, sender="sender", subject="hi",
                          message="body", to=None) -> str:
    """Place an email on disk so _render_unread_digest finds it."""
    email_id = str(uuid4())
    msg_dir = working_dir / "mailbox" / "inbox" / email_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "_mailbox_id": email_id,
        "from": sender,
        "to": to or ["test"],
        "subject": subject,
        "message": message,
        "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (msg_dir / "message.json").write_text(json.dumps(data, indent=2))
    return email_id


def make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


# ---------------------------------------------------------------------------
# Internal state — _cancel_event
# ---------------------------------------------------------------------------


def test_cancel_event_always_created(tmp_path):
    """Agent should always have _cancel_event (no external injection)."""
    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    assert isinstance(agent._cancel_event, threading.Event)
    assert not agent._cancel_event.is_set()


# ---------------------------------------------------------------------------
# Admin dict — new keys
# ---------------------------------------------------------------------------


def test_admin_dict_stored_defaults_empty(tmp_path):
    """Agent without admin= should have an empty _admin dict."""
    agent = BaseAgent(service=make_mock_service(), agent_name="a", working_dir=tmp_path / "t1")
    assert agent._admin == {}


def test_karma_admin_stored(tmp_path):
    """admin={"karma": True} should be stored on the agent as-is."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="b", working_dir=tmp_path / "t2",
        admin={"karma": True},
    )
    assert agent._admin.get("karma") is True


def test_nirvana_admin_stored(tmp_path):
    """admin={"nirvana": True} should be stored on the agent as-is."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="c", working_dir=tmp_path / "t3",
        admin={"nirvana": True},
    )
    assert agent._admin.get("nirvana") is True


def test_old_admin_keys_ignored(tmp_path):
    """admin={"silence": True} should NOT grant karma authority."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="d", working_dir=tmp_path / "t4",
        admin={"silence": True, "kill": True},
    )
    # Old keys are stored as-is (agent just persists the dict), but they do
    # not map to karma/nirvana authority — those gates check for those keys.
    assert not agent._admin.get("karma")
    assert not agent._admin.get("nirvana")


# ---------------------------------------------------------------------------
# Tool executor cancel check
# ---------------------------------------------------------------------------


def test_sequential_execution_stops_on_cancel(tmp_path):
    """Sequential tool execution should return empty when cancel event is set."""
    from lingtai_kernel.loop_guard import LoopGuard
    from lingtai_kernel.tool_executor import ToolExecutor
    from lingtai.llm import ToolCall

    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    agent._cancel_event.set()

    tc = ToolCall(name="system", args={"action": "sleep"}, id="tc1")
    guard = LoopGuard(max_total_calls=10)

    executor = ToolExecutor(
        dispatch_fn=agent._dispatch_tool,
        make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
            name, result, provider=agent._config.provider, **kw
        ),
        guard=guard,
        known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
        logger_fn=agent._log,
    )
    results, intercepted, text = executor.execute(
        [tc], cancel_event=agent._cancel_event, collected_errors=[],
    )

    assert results == []
    assert intercepted is False


# ---------------------------------------------------------------------------
# Normal mail — unchanged behavior
# ---------------------------------------------------------------------------


def test_normal_email_notifies_inbox(tmp_path):
    """Normal-type mail publishes ``.notification/email.json``.

    Under the .notification/ filesystem redesign, mail arrival no longer
    posts MSG_TC_WAKE to the agent inbox.  Instead it writes the unread
    digest to ``.notification/email.json`` and calls ``_wake_nap`` to
    nudge the heartbeat for sub-second sync latency.  The kernel's
    notification sync mechanism reads the file and injects the wire
    pair (or wakes the agent if asleep).
    """
    from lingtai_kernel.notifications import collect_notifications

    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    _persist_inbox_email(agent.working_dir, sender="colleague", subject="hello", message="hi there")
    agent._on_mail_received({
        "_mailbox_id": "test123",
        "from": "colleague", "to": "test", "subject": "hello",
        "message": "hi there", "type": "normal",
    })
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1
    assert not agent._cancel_event.is_set()


def test_non_admin_can_send_normal_mail(tmp_path):
    """Non-admin should be able to send normal mail."""
    agent = BaseAgent(
        service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test", admin={},
    )
    mock_mail = MagicMock()
    mock_mail.address = "127.0.0.1:8000"
    mock_mail.send.return_value = None
    agent._mail_service = mock_mail

    result = agent._intrinsics["email"]({
        "action": "send", "address": "127.0.0.1:8001",
        "subject": "hello", "message": "hi there",
    })
    assert result["status"] == "sent"


# ---------------------------------------------------------------------------
# Mail type=silence/kill — now treated as normal mail (no special handling)
# ---------------------------------------------------------------------------


def test_mail_type_silence_treated_as_normal(tmp_path):
    """type='silence' is treated like normal mail: publishes ``.notification/email.json``,
    does not set cancel."""
    from lingtai_kernel.notifications import collect_notifications

    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    assert not agent._cancel_event.is_set()
    _persist_inbox_email(agent.working_dir, sender="boss", subject="shh", message="be quiet")

    agent._on_mail_received({
        "_mailbox_id": "msg001",
        "from": "boss", "to": "test", "subject": "shh",
        "message": "be quiet", "type": "silence",
    })

    # Must NOT set the cancel event — silence goes through signal files now.
    assert not agent._cancel_event.is_set()
    # Mail published as normal; the notification sync owns wake.
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1


def test_mail_type_kill_treated_as_normal(tmp_path):
    """type='kill' is treated like normal mail: publishes ``.notification/email.json``,
    does not set cancel or shutdown."""
    from lingtai_kernel.notifications import collect_notifications

    agent = BaseAgent(service=make_mock_service(), agent_name="test", working_dir=tmp_path / "test")
    assert not agent._cancel_event.is_set()
    _persist_inbox_email(agent.working_dir, sender="boss", subject="die", message="terminate")

    agent._on_mail_received({
        "_mailbox_id": "msg002",
        "from": "boss", "to": "test", "subject": "die",
        "message": "terminate", "type": "kill",
    })

    # Must NOT set cancel or shutdown — kill goes through karma system intrinsic.
    assert not agent._cancel_event.is_set()
    out = collect_notifications(agent.working_dir)
    assert "email" in out
    assert out["email"]["data"]["count"] == 1
