"""Tests for the client-side LICC writer (``lingtai.core.mcp.licc``).

``inbox.py`` is the kernel-side *consumer* of LICC v1 — it polls
``.mcp_inbox/<mcp>/*.json``, validates, dispatches, and deletes. This
module tests the *producer* half: the lightweight ``push_inbox_event``
helper an out-of-process MCP imports to atomically drop an event into the
agent's inbox.

The two halves must agree on the wire format, so several tests round-trip a
pushed event back through ``inbox.validate_event`` to prove compatibility.

Coverage:
- missing env / missing args → ``False`` no-op (no file written)
- default schema + defaults (licc_version, wake, received_at)
- explicit ``agent_dir`` / ``mcp_name`` parameters
- env-var defaults (``LINGTAI_AGENT_DIR`` / ``LINGTAI_MCP_NAME``)
- atomicity: only the final ``.json`` remains, no leftover ``.tmp``
- unicode bodies/subjects round-trip
- repeated pushes produce distinct event files (uniqueness)
- invalid path components / serialization or filesystem errors → ``False``
- pushed events pass ``inbox.validate_event``
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.core.mcp import licc
from lingtai.core.mcp.inbox import (
    EVENT_SUFFIX,
    INBOX_DIRNAME,
    LICC_VERSION,
    TMP_SUFFIX,
    _scan_once,
    validate_event,
)
from tests._service_helpers import make_gemini_mock_service as make_mock_service


# ---------------------------------------------------------------------------
# Re-exported constants must match the kernel-side contract exactly.
# ---------------------------------------------------------------------------

def test_constants_match_inbox():
    assert licc.LICC_VERSION == LICC_VERSION == 1
    assert licc.INBOX_DIRNAME == INBOX_DIRNAME == ".mcp_inbox"
    assert licc.TMP_SUFFIX == TMP_SUFFIX


def _events_in(agent_dir: Path, mcp_name: str) -> list[Path]:
    d = agent_dir / INBOX_DIRNAME / mcp_name
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.name.endswith(EVENT_SUFFIX)
                  and not p.name.endswith(TMP_SUFFIX))


def _read_event(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))




def _mk_agent(tmp_path: Path):
    workdir = tmp_path / "agent"
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    ), workdir


# ---------------------------------------------------------------------------
# Missing-config no-op
# ---------------------------------------------------------------------------

def test_missing_env_and_args_is_noop(monkeypatch, tmp_path):
    """No agent dir and no mcp name anywhere → return False, write nothing."""
    monkeypatch.delenv("LINGTAI_AGENT_DIR", raising=False)
    monkeypatch.delenv("LINGTAI_MCP_NAME", raising=False)

    ok = licc.push_inbox_event("alice", "hi", "hello")
    assert ok is False
    # Nothing should have been created anywhere under tmp_path.
    assert not any(tmp_path.rglob("*.json"))


def test_missing_mcp_name_is_noop(monkeypatch, tmp_path):
    """Agent dir present but no mcp name → no-op (we don't know where to write)."""
    monkeypatch.setenv("LINGTAI_AGENT_DIR", str(tmp_path))
    monkeypatch.delenv("LINGTAI_MCP_NAME", raising=False)

    ok = licc.push_inbox_event("alice", "hi", "hello")
    assert ok is False
    assert not (tmp_path / INBOX_DIRNAME).exists()


def test_missing_agent_dir_is_noop(monkeypatch, tmp_path):
    """MCP name present but no agent dir → no-op."""
    monkeypatch.delenv("LINGTAI_AGENT_DIR", raising=False)
    monkeypatch.setenv("LINGTAI_MCP_NAME", "telegram")

    ok = licc.push_inbox_event("alice", "hi", "hello")
    assert ok is False


# ---------------------------------------------------------------------------
# Happy path: schema + defaults
# ---------------------------------------------------------------------------

def test_push_writes_event_with_defaults(tmp_path):
    ok = licc.push_inbox_event(
        "alice", "hi", "hello",
        agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is True

    events = _events_in(tmp_path, "telegram")
    assert len(events) == 1
    event = _read_event(events[0])

    assert event["licc_version"] == LICC_VERSION
    assert event["from"] == "alice"
    assert event["subject"] == "hi"
    assert event["body"] == "hello"
    # Defaults: wake True, metadata empty dict, received_at populated (ISO-ish).
    assert event["wake"] is True
    assert event["metadata"] == {}
    assert isinstance(event["received_at"], str) and event["received_at"]


def test_push_respects_explicit_fields(tmp_path):
    ok = licc.push_inbox_event(
        "bob", "subj", "body text",
        metadata={"conversation_ref": "tg:chat:1"},
        wake=False,
        received_at="2026-01-01T00:00:00+00:00",
        agent_dir=tmp_path, mcp_name="feishu",
    )
    assert ok is True
    event = _read_event(_events_in(tmp_path, "feishu")[0])
    assert event["from"] == "bob"
    assert event["wake"] is False
    assert event["metadata"] == {"conversation_ref": "tg:chat:1"}
    assert event["received_at"] == "2026-01-01T00:00:00+00:00"


def test_push_uses_env_defaults(monkeypatch, tmp_path):
    """With no explicit args, agent dir + mcp name come from env."""
    monkeypatch.setenv("LINGTAI_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("LINGTAI_MCP_NAME", "imap")

    ok = licc.push_inbox_event("carol", "s", "b")
    assert ok is True
    events = _events_in(tmp_path, "imap")
    assert len(events) == 1
    assert _read_event(events[0])["from"] == "carol"


def test_explicit_args_override_env(monkeypatch, tmp_path):
    """Explicit agent_dir / mcp_name win over env vars."""
    monkeypatch.setenv("LINGTAI_AGENT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("LINGTAI_MCP_NAME", "wrong-mcp")

    right = tmp_path / "right"
    ok = licc.push_inbox_event(
        "dave", "s", "b", agent_dir=right, mcp_name="right-mcp",
    )
    assert ok is True
    assert _events_in(right, "right-mcp")
    assert not (tmp_path / "wrong").exists()


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------

def test_push_leaves_no_tmp_file(tmp_path):
    """After a successful push only the final .json exists — the .tmp is gone."""
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is True
    mcp_dir = tmp_path / INBOX_DIRNAME / "telegram"
    tmps = [p for p in mcp_dir.iterdir() if p.name.endswith(TMP_SUFFIX)]
    finals = [p for p in mcp_dir.iterdir() if p.name.endswith(EVENT_SUFFIX)
              and not p.name.endswith(TMP_SUFFIX)]
    assert tmps == []
    assert len(finals) == 1


def test_event_filename_is_json_not_tmp(tmp_path):
    licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="telegram",
        event_id="fixed-id",
    )
    final = tmp_path / INBOX_DIRNAME / "telegram" / "fixed-id.json"
    assert final.is_file()
    assert not (tmp_path / INBOX_DIRNAME / "telegram" / f"fixed-id{TMP_SUFFIX}").exists()


# ---------------------------------------------------------------------------
# Schema and path-component safety
# ---------------------------------------------------------------------------

def test_invalid_payload_is_noop(tmp_path):
    """The canonical producer should not intentionally write dead-letterable events."""
    ok = licc.push_inbox_event(
        "alice", "", "hello", agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is False
    assert not any(tmp_path.rglob("*.json"))


def test_invalid_mcp_name_is_noop(tmp_path):
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="../escape",
    )
    assert ok is False
    assert not any(tmp_path.rglob("*.json"))


def test_invalid_event_id_is_noop(tmp_path):
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="telegram",
        event_id="../escape",
    )
    assert ok is False
    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------------------
# Unicode
# ---------------------------------------------------------------------------

def test_push_handles_unicode(tmp_path):
    ok = licc.push_inbox_event(
        "爱丽丝", "主题 — émoji 🚀", "正文 with 中文 and 🌟 and ✨",
        agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is True
    event = _read_event(_events_in(tmp_path, "telegram")[0])
    assert event["from"] == "爱丽丝"
    assert event["subject"] == "主题 — émoji 🚀"
    assert event["body"] == "正文 with 中文 and 🌟 and ✨"


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------

def test_repeated_pushes_are_distinct_files(tmp_path):
    for i in range(5):
        assert licc.push_inbox_event(
            "alice", f"subj {i}", f"body {i}",
            agent_dir=tmp_path, mcp_name="telegram",
        ) is True
    events = _events_in(tmp_path, "telegram")
    assert len(events) == 5
    # Each file has a unique name (no collisions / overwrites).
    assert len({p.name for p in events}) == 5
    # And each carries a distinct subject (no clobbering).
    subjects = {_read_event(p)["subject"] for p in events}
    assert subjects == {f"subj {i}" for i in range(5)}


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_non_json_metadata_returns_false(tmp_path):
    """Serialization failures are swallowed → False, never raises."""
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", metadata={"bad": object()},
        agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is False


def test_invalid_agent_dir_returns_false():
    """Invalid explicit agent_dir values are swallowed → False, never raises."""
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=object(), mcp_name="telegram",
    )
    assert ok is False


def test_oserror_returns_false(tmp_path, monkeypatch):
    """A filesystem failure during write is swallowed → False, never raises."""
    def boom(*a, **k):
        raise OSError("disk full")

    # Make the atomic os.replace fail; push must catch and return False.
    monkeypatch.setattr(licc.os, "replace", boom)
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is False


def test_mkdir_oserror_returns_false(tmp_path, monkeypatch):
    """If the inbox dir cannot be created, push returns False without raising."""
    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)
    ok = licc.push_inbox_event(
        "alice", "hi", "hello", agent_dir=tmp_path, mcp_name="telegram",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Round-trip compatibility with the kernel-side validator
# ---------------------------------------------------------------------------

def test_pushed_event_passes_inbox_validator(tmp_path):
    """The producer's output must validate as a legal LICC event."""
    licc.push_inbox_event(
        "alice", "hi", "hello",
        metadata={"platform": "telegram"},
        agent_dir=tmp_path, mcp_name="telegram",
    )
    event = _read_event(_events_in(tmp_path, "telegram")[0])
    ok, err = validate_event(event)
    assert ok, err


def test_pushed_event_with_all_fields_passes_validator(tmp_path):
    licc.push_inbox_event(
        "alice", "s" * 200, "long body " * 100,
        metadata={"conversation_ref": "c", "message_ref": "m", "platform": "p"},
        wake=False,
        agent_dir=tmp_path, mcp_name="telegram",
    )
    event = _read_event(_events_in(tmp_path, "telegram")[0])
    ok, err = validate_event(event)
    assert ok, err


# ---------------------------------------------------------------------------
# Producer → consumer integration
# ---------------------------------------------------------------------------

def test_pushed_event_is_consumed_by_scan_once(tmp_path):
    """The canonical producer's output should drive the real consumer path."""
    agent, workdir = _mk_agent(tmp_path)

    assert licc.push_inbox_event(
        "alice", "new DM", "hello from producer",
        metadata={"platform": "telegram", "conversation_ref": "chat-1"},
        agent_dir=workdir,
        mcp_name="telegram",
    ) is True

    event_files = _events_in(workdir, "telegram")
    assert len(event_files) == 1

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 1
    assert not event_files[0].exists(), "consumer should delete the consumed event"

    notif_file = workdir / ".notification" / "mcp.telegram.json"
    assert notif_file.exists()
    notif = json.loads(notif_file.read_text(encoding="utf-8"))
    preview = notif["data"]["previews"][0]
    assert preview["from"] == "alice"
    assert preview["subject"] == "new DM"
    assert preview["preview"] == "hello from producer"
    assert preview["platform"] == "telegram"
    assert preview["conversation_ref"] == "chat-1"
