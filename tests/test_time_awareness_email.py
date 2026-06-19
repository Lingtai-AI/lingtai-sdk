"""Tests: email capability return-path scrubbing contract."""
from types import SimpleNamespace

from lingtai_kernel.time_veil import scrub_time_fields


def _agent(time_awareness: bool):
    return SimpleNamespace(_config=SimpleNamespace(time_awareness=time_awareness))


def test_scrub_schedule_payload_time_blind():
    agent = _agent(False)
    payload = {
        "id": "sched-1",
        "status": "scheduled",
        "scheduled_at": "2026-04-15T12:00:00Z",
        "estimated_finish": "2026-04-15T18:00:00Z",
        "last_sent_at": "2026-04-15T11:00:00Z",
        "interval_seconds": 600,
    }
    out = scrub_time_fields(agent, payload)
    assert out["scheduled_at"] == ""
    assert out["estimated_finish"] == ""
    assert out["last_sent_at"] == ""
    assert out["interval_seconds"] == 600
    assert out["status"] == "scheduled"


def test_scrub_email_summary_time_blind():
    agent = _agent(False)
    summary = {
        "id": "abc",
        "from": "alice",
        "to": ["bob"],
        "subject": "hi",
        "preview": "hello",
        "time": "2026-04-15T12:00:00Z",
        "folder": "inbox",
        "unread": True,
    }
    out = scrub_time_fields(agent, summary)
    assert out["time"] == ""
    assert out["subject"] == "hi"
    assert out["unread"] is True


def test_scrub_passthrough_when_time_aware():
    agent = _agent(True)
    payload = {"scheduled_at": "2026-04-15T12:00:00Z", "time": "2026-04-15T12:00:00Z"}
    out = scrub_time_fields(agent, payload)
    assert out is payload
    assert out["scheduled_at"] == "2026-04-15T12:00:00Z"
