"""Tests: mail intrinsic return paths blank timestamps when time_awareness=False."""
from types import SimpleNamespace

from lingtai_kernel.time_veil import scrub_time_fields


def _agent(time_awareness: bool):
    return SimpleNamespace(_config=SimpleNamespace(time_awareness=time_awareness))


def test_mail_read_payload_blanks_time_fields_when_time_blind():
    agent = _agent(False)
    payload = {
        "_mailbox_id": "abc",
        "from": "alice",
        "to": ["bob"],
        "subject": "hi",
        "message": "body",
        "received_at": "2026-04-15T12:00:00Z",
        "sent_at": "2026-04-15T11:59:00Z",
        "deliver_at": "2026-04-15T11:59:00Z",
    }
    out = scrub_time_fields(agent, payload)
    assert out["received_at"] == ""
    assert out["sent_at"] == ""
    assert out["deliver_at"] == ""
    assert out["from"] == "alice"
    assert out["subject"] == "hi"
    assert out["message"] == "body"


def test_mail_check_summary_blanks_time_when_time_blind():
    agent = _agent(False)
    summary = {
        "id": "abc",
        "from": "alice",
        "to": ["bob"],
        "subject": "hi",
        "preview": "hello",
        "time": "2026-04-15T12:00:00Z",
        "unread": True,
    }
    out = scrub_time_fields(agent, summary)
    assert out["time"] == ""
    assert out["unread"] is True
    assert out["subject"] == "hi"


def test_mail_payload_unchanged_when_time_aware():
    agent = _agent(True)
    payload = {
        "_mailbox_id": "abc",
        "received_at": "2026-04-15T12:00:00Z",
        "subject": "hi",
    }
    out = scrub_time_fields(agent, payload)
    assert out is payload
    assert out["received_at"] == "2026-04-15T12:00:00Z"
