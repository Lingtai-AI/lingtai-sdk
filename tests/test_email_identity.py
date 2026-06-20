"""Tests for email identity injection — every sent message carries sender's manifest,
every received summary surfaces sender's identity card.

Replaces test_mail_identity.py which targeted the deleted mail intrinsic.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lingtai.agent import Agent
from lingtai_kernel.intrinsics import email as email_mod
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _make_agent(tmp_path, *, agent_name="alice", admin=None):
    """Build a real Agent rooted at tmp_path with a stable manifest."""
    return Agent(
        service=make_mock_service(),
        agent_name=agent_name,
        working_dir=tmp_path / agent_name,
        admin=admin or {},
    )


# ---------------------------------------------------------------------------
# Identity attached on send
# ---------------------------------------------------------------------------


def test_send_payload_contains_identity(tmp_path):
    """The sent record (in mailbox/sent/{id}/message.json) carries an identity dict."""
    agent = _make_agent(tmp_path, agent_name="alice", admin={"karma": True})
    mail_svc = MagicMock()
    mail_svc.address = "alice@example"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send",
        "address": "/other/agent",
        "message": "hello",
        "subject": "test",
    })
    assert result["status"] == "sent"

    # The mailbox/sent/ entry should carry identity
    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_entries = [d for d in sent_dir.iterdir() if d.is_dir()]
    assert len(sent_entries) == 1
    sent_record = json.loads((sent_entries[0] / "message.json").read_text())
    assert "identity" in sent_record
    identity = sent_record["identity"]
    assert identity["agent_name"] == "alice"
    assert identity["admin"] == {"karma": True}

    agent.stop(timeout=1.0)


def test_send_identity_with_no_admin(tmp_path):
    """Identity works when admin is empty."""
    agent = _make_agent(tmp_path, agent_name="bob", admin={})
    mail_svc = MagicMock()
    mail_svc.address = "bob@example"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    mgr = agent._email_manager
    mgr.handle({"action": "send", "address": "/other", "message": "hi"})

    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_entry = next(d for d in sent_dir.iterdir() if d.is_dir())
    sent_record = json.loads((sent_entry / "message.json").read_text())
    assert sent_record["identity"]["agent_name"] == "bob"
    assert sent_record["identity"]["admin"] == {}

    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Identity surfaced in check
# ---------------------------------------------------------------------------


def _seed_inbox(working_dir: Path, msg_id: str, payload: dict) -> None:
    """Write a message directly to mailbox/inbox/{msg_id}/message.json."""
    inbox = working_dir / "mailbox" / "inbox" / msg_id
    inbox.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "_mailbox_id": msg_id, "received_at": "2026-05-01T10:00:00Z"}
    (inbox / "message.json").write_text(json.dumps(payload))


def test_check_shows_agent_name(tmp_path):
    """check surfaces sender_name when identity has agent_name."""
    agent = _make_agent(tmp_path)
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "/sender/path",
        "subject": "hi",
        "message": "body",
        "identity": {
            "address": "/agents/sender",
            "agent_name": "bob",
            "admin": {"karma": False},
        },
    })
    result = agent._email_manager.handle({"action": "check"})
    assert result["total"] == 1
    msg = result["emails"][0]
    # Inbox check uses mail-style summary which formats from as "name (address)"
    assert msg["from"] == "bob (/sender/path)"
    assert msg.get("sender_name") == "bob"
    assert msg.get("is_human") is False  # admin is not None → it's an agent

    agent.stop(timeout=1.0)


def test_check_no_identity_backwards_compat(tmp_path):
    """Messages without identity surface plain from address; no sender_* fields."""
    agent = _make_agent(tmp_path)
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "/sender/path",
        "subject": "old mail",
        "message": "no identity",
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    assert msg["from"] == "/sender/path"
    assert "sender_name" not in msg
    assert "is_human" not in msg

    agent.stop(timeout=1.0)


def test_check_human_sender(tmp_path):
    """admin=None in identity → is_human=True."""
    agent = _make_agent(tmp_path)
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "human-operator",
        "subject": "task",
        "message": "do this",
        "identity": {
            "address": "/human",
            "agent_name": "the human",
            "admin": None,
        },
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    assert msg.get("is_human") is True

    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Identity surfaced in read
# ---------------------------------------------------------------------------


def test_read_includes_identity(tmp_path):
    """read surfaces sender_name and is_human in the result."""
    agent = _make_agent(tmp_path)
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "alice@example",
        "subject": "hello",
        "message": "from alice",
        "identity": {
            "address": "/agents/alice",
            "agent_name": "alice",
            "admin": {"karma": False},
        },
    })
    result = agent._email_manager.handle({"action": "read", "email_id": ["msg1"]})
    assert "emails" in result
    entry = result["emails"][0]
    assert entry.get("sender_name") == "alice"
    assert entry.get("is_human") is False

    agent.stop(timeout=1.0)


def test_read_no_identity_backwards_compat(tmp_path):
    """read of messages without identity is backwards compatible."""
    agent = _make_agent(tmp_path)
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "anon",
        "subject": "old",
        "message": "no identity",
    })
    result = agent._email_manager.handle({"action": "read", "email_id": ["msg1"]})
    entry = result["emails"][0]
    assert entry["from"] == "anon"
    assert "sender_name" not in entry
    assert "is_human" not in entry

    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Sender disambiguation — same name, different agent_id (issue #57)
# ---------------------------------------------------------------------------


def test_check_disambiguates_same_name_different_agent_id(tmp_path):
    """When sender shares recipient's agent_name but has a different agent_id,
    check should disambiguate with the sender's agent_id."""
    agent = _make_agent(tmp_path, agent_name="mimo-1")
    sender_id = "20260506-000525-ab84"
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "mimo-1",
        "subject": "cross-project",
        "message": "hello from the other mimo-1",
        "identity": {
            "agent_id": sender_id,
            "agent_name": "mimo-1",
            "admin": {},
        },
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    # Should include agent_id to disambiguate
    assert f"agent:{sender_id}" in msg["from"]
    assert "mimo-1" in msg["from"]

    agent.stop(timeout=1.0)


def test_check_disambiguates_different_names_different_agent_id(tmp_path):
    """When sender has a different agent_name AND different agent_id,
    agent_id is shown for full clarity (no ambiguity)."""
    agent = _make_agent(tmp_path, agent_name="alice")
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "bob-addr",
        "subject": "normal",
        "message": "hi",
        "identity": {
            "agent_id": "20260506-111111-cccc",
            "agent_name": "bob",
            "admin": {},
        },
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    # agent_id shown for clarity — different agent from a different project
    assert "agent:20260506-111111-cccc" in msg["from"]
    assert "bob" in msg["from"]

    agent.stop(timeout=1.0)


def test_check_no_disambiguation_for_self_send(tmp_path):
    """When sender is truly the same agent (same agent_id), no
    disambiguation should occur."""
    agent = _make_agent(tmp_path, agent_name="mimo-1")
    own_id = agent._agent_id
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "mimo-1",
        "subject": "self-note",
        "message": "note to self",
        "identity": {
            "agent_id": own_id,
            "agent_name": "mimo-1",
            "admin": {},
        },
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    # Same agent_id — no disambiguation
    assert msg["from"] == "mimo-1 (mimo-1)"
    assert "agent:" not in msg["from"]

    agent.stop(timeout=1.0)


def test_check_no_disambiguation_without_identity(tmp_path):
    """Messages without identity should not be affected by disambiguation."""
    agent = _make_agent(tmp_path, agent_name="mimo-1")
    _seed_inbox(agent.working_dir, "msg1", {
        "from": "mimo-1",
        "subject": "old-style",
        "message": "no identity card",
    })
    result = agent._email_manager.handle({"action": "check"})
    msg = result["emails"][0]
    # No identity → plain address, no disambiguation
    assert msg["from"] == "mimo-1"
    assert "agent:" not in msg["from"]

    agent.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Disambiguation in _message_summary (unit-level)
# ---------------------------------------------------------------------------

from lingtai_kernel.intrinsics.email.primitives import _message_summary


def test_message_summary_disambiguates_with_recipient_id():
    """_message_summary adds agent_id when names match but IDs differ."""
    msg = {
        "_mailbox_id": "test-id",
        "from": "mimo-1",
        "subject": "test",
        "message": "body",
        "received_at": "2026-05-01T10:00:00Z",
        "identity": {
            "agent_id": "sender-id-1234",
            "agent_name": "mimo-1",
        },
    }
    result = _message_summary(msg, set(), recipient_agent_id="recipient-id-5678")
    assert "agent:sender-id-1234" in result["from"]


def test_message_summary_no_disambiguation_same_id():
    """_message_summary does not disambiguate when agent_ids match."""
    msg = {
        "_mailbox_id": "test-id",
        "from": "mimo-1",
        "subject": "test",
        "message": "body",
        "received_at": "2026-05-01T10:00:00Z",
        "identity": {
            "agent_id": "same-id",
            "agent_name": "mimo-1",
        },
    }
    result = _message_summary(msg, set(), recipient_agent_id="same-id")
    assert result["from"] == "mimo-1 (mimo-1)"
    assert "agent:" not in result["from"]


def test_message_summary_no_disambiguation_without_recipient_id():
    """_message_summary is backward-compatible — no recipient_agent_id means no disambiguation."""
    msg = {
        "_mailbox_id": "test-id",
        "from": "mimo-1",
        "subject": "test",
        "message": "body",
        "received_at": "2026-05-01T10:00:00Z",
        "identity": {
            "agent_id": "sender-id-1234",
            "agent_name": "mimo-1",
        },
    }
    result = _message_summary(msg, set())
    assert result["from"] == "mimo-1 (mimo-1)"
    assert "agent:" not in result["from"]

# ---------------------------------------------------------------------------
# Cross-project from field: full path for abs mode
# ---------------------------------------------------------------------------


def test_abs_mode_from_field_uses_full_path(tmp_path):
    """When mode='abs', the from field should be the sender's full working directory."""
    agent = _make_agent(tmp_path, agent_name="mimo-1")
    mail_svc = MagicMock()
    mail_svc.address = "mimo-1"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send",
        "address": "/other/project/.lingtai/mimo-1",
        "message": "hello cross-project",
        "mode": "abs",
    })
    assert result["status"] == "sent"

    # The sent record should have the full path as from
    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_entries = [d for d in sent_dir.iterdir() if d.is_dir()]
    assert len(sent_entries) == 1
    sent_record = json.loads((sent_entries[0] / "message.json").read_text())
    # from should be the full working directory path
    assert sent_record["from"] == str(agent.working_dir)
    assert "/" in sent_record["from"]  # full path contains slashes

    agent.stop(timeout=1.0)


def test_peer_mode_from_field_uses_relative_name(tmp_path):
    """When mode='peer' (default), the from field should be the relative name."""
    agent = _make_agent(tmp_path, agent_name="mimo-1")
    mail_svc = MagicMock()
    mail_svc.address = "mimo-1"
    mail_svc.send.return_value = None
    agent._mail_service = mail_svc

    mgr = agent._email_manager
    result = mgr.handle({
        "action": "send",
        "address": "sibling-agent",
        "message": "hello peer",
    })
    assert result["status"] == "sent"

    sent_dir = agent.working_dir / "mailbox" / "sent"
    sent_entries = [d for d in sent_dir.iterdir() if d.is_dir()]
    assert len(sent_entries) == 1
    sent_record = json.loads((sent_entries[0] / "message.json").read_text())
    # from should be the relative name
    assert sent_record["from"] == "mimo-1"

    agent.stop(timeout=1.0)
