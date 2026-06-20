"""Tests for the LICC v1 contract and the mcp_inbox poller.

Covers: validation; one-shot scan dispatching valid events; dead-letter
of invalid events; .tmp file ignored; wake=false skipping _wake_nap;
multiple MCPs with separate subdirs; signal-only notification body
(issue #37 — no body / sender / subject leaks into the kernel
notification, since the agent already gets that content via the explicit
``<mcp>(action="read")`` tool call).

The poller-as-thread is exercised lightly via start/stop and a manual
event injection; correctness is mostly proved by the synchronous _scan_once
path so we don't depend on timing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lingtai.agent import Agent
from lingtai.core.mcp.inbox import (
    DEAD_DIRNAME,
    INBOX_DIRNAME,
    MCPInboxPoller,
    POLL_INTERVAL,
    TMP_SUFFIX,
    _scan_once,
    validate_event,
)
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def _mk_agent(tmp_path: Path):
    workdir = tmp_path / "agent"
    return Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=workdir,
        capabilities={"mcp": {}},
    ), workdir


def _write_event(workdir: Path, mcp_name: str, event_id: str, event: dict) -> Path:
    """Atomic LICC write — caller is the simulated MCP."""
    target_dir = workdir / INBOX_DIRNAME / mcp_name
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f"{event_id}{TMP_SUFFIX}"
    final = target_dir / f"{event_id}.json"
    tmp.write_text(json.dumps(event), encoding="utf-8")
    tmp.rename(final)
    return final


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def test_validator_accepts_minimal_event():
    ok, err = validate_event({"from": "alice", "subject": "hi", "body": "hello"})
    assert ok, err


def test_validator_accepts_full_event():
    ok, err = validate_event({
        "licc_version": 1,
        "from": "alice", "subject": "hi", "body": "hello",
        "metadata": {"k": "v"}, "wake": False,
        "received_at": "2026-04-29T12:00:00Z",
    })
    assert ok, err


def test_validator_rejects_missing_from():
    ok, err = validate_event({"subject": "hi", "body": "hello"})
    assert not ok and "from" in err


def test_validator_rejects_missing_subject():
    ok, err = validate_event({"from": "a", "body": "hello"})
    assert not ok and "subject" in err


def test_validator_rejects_missing_body():
    ok, err = validate_event({"from": "a", "subject": "hi"})
    assert not ok and "body" in err


def test_validator_rejects_long_subject():
    ok, err = validate_event({"from": "a", "subject": "x" * 250, "body": "b"})
    assert not ok and "subject too long" in err


def test_validator_rejects_unknown_version():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "licc_version": 99})
    assert not ok and "licc_version" in err


def test_validator_rejects_non_bool_wake():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "wake": "yes"})
    assert not ok and "wake" in err


def test_validator_rejects_non_dict_metadata():
    ok, err = validate_event({"from": "a", "subject": "hi", "body": "b", "metadata": "no"})
    assert not ok and "metadata" in err


# ---------------------------------------------------------------------------
# _scan_once dispatch
# ---------------------------------------------------------------------------

def test_scan_dispatches_valid_event_to_notification(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello",
    })

    inbox_root = workdir / INBOX_DIRNAME
    dispatched = _scan_once(agent, inbox_root)
    assert dispatched == 1

    # Notification file published to .notification/mcp.telegram.json.
    notif_file = workdir / ".notification" / "mcp.telegram.json"
    assert notif_file.exists(), "notification file not created"
    import json as _json
    notif = _json.loads(notif_file.read_text(encoding="utf-8"))
    assert "telegram" in notif["header"]
    assert "1 new event" in notif["header"]
    assert notif["data"]["count"] == 1
    assert notif["data"]["source"] == "telegram"
    # Sender / subject / body must NOT be inlined; the agent learns them by
    # explicitly calling the MCP's read/check action.
    assert "alice" not in notif["header"]
    assert "hi" not in notif["header"]
    assert "hello" not in notif["header"]

    # File was deleted on success.
    assert not (inbox_root / "telegram" / "ev1.json").exists()


def test_scan_coalesces_multiple_events_into_one_notification(tmp_path):
    """Issue #37: multiple events from the same MCP in one sweep produce
    exactly one notification.

    Senders + subjects + truncated body snippets are surfaced as triage
    previews; full bodies stay behind the MCP read action so the agent has
    one source of truth and avoids the re-processing loop that issue #37
    originally fixed."""
    import json as _json

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "s1",
        "body": "first 100 chars of body 1 are visible to the agent as a triage hint, the rest stays behind read.",
    })
    _write_event(workdir, "telegram", "ev2", {
        "from": "bob", "subject": "s2",
        "body": "second body content goes here as the snippet.",
    })
    _write_event(workdir, "telegram", "ev3", {
        "from": "carol", "subject": "s3",
        "body": "third body content snippet for triage.",
    })

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 3

    # Exactly ONE notification file, not three.
    notif_file = workdir / ".notification" / "mcp.telegram.json"
    assert notif_file.exists(), "notification file not created"
    notif = _json.loads(notif_file.read_text(encoding="utf-8"))
    assert "telegram" in notif["header"]
    assert "3 new events" in notif["header"]
    assert notif["data"]["count"] == 3

    # Senders, subjects, and body snippets are all surfaced as triage previews.
    previews = notif["data"]["previews"]
    assert len(previews) == 3
    assert {p["from"] for p in previews} == {"alice", "bob", "carol"}
    assert {p["subject"] for p in previews} == {"s1", "s2", "s3"}
    # Body snippets are present and capped at _PREVIEW_FIELD_CAP.
    from lingtai.core.mcp.inbox import _PREVIEW_FIELD_CAP
    for p in previews:
        assert "preview" in p
        assert len(p["preview"]) <= _PREVIEW_FIELD_CAP
    # Instructions render only lightweight routing context. The body previews
    # live once in data.previews and must not be duplicated verbatim into the
    # instructions string.
    for sender in ("alice", "bob", "carol"):
        assert sender in notif["instructions"]
    for subject in ("s1", "s2", "s3"):
        assert subject in notif["instructions"]
    assert "second body content" in previews[1]["preview"]
    assert "second body content" not in notif["instructions"]
    for p in previews:
        assert p["preview"] not in notif["instructions"]


def test_scan_summary_uses_singular_for_one_event(tmp_path):
    import json as _json

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "imap", "ev1", {
        "from": "a", "subject": "s", "body": "b",
    })
    _scan_once(agent, workdir / INBOX_DIRNAME)
    notif_file = workdir / ".notification" / "mcp.imap.json"
    assert notif_file.exists()
    notif = _json.loads(notif_file.read_text(encoding="utf-8"))
    assert "1 new event" in notif["header"]  # no plural 's'


def test_scan_publishes_notification_file(tmp_path):
    """Events are published to .notification/ — no explicit _wake_nap needed."""
    import json as _json

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello",
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif_file = workdir / ".notification" / "mcp.telegram.json"
    assert notif_file.exists()
    notif = _json.loads(notif_file.read_text(encoding="utf-8"))
    assert notif["icon"] == "💬"
    assert notif["data"]["source"] == "telegram"
    assert "previews" in notif["data"]


def test_scan_truncates_long_body_into_preview_snippet(tmp_path):
    """The body snippet (`preview` field) is hard-truncated at
    _PREVIEW_FIELD_CAP — keeps notification footprint bounded even for very
    long bodies (multi-paragraph emails, OCR text, voice transcripts).

    `from` and `subject` pass through uncapped — both are bounded by
    upstream construction (subject is also validated <= 200 chars by
    validate_event)."""
    import json as _json

    from lingtai.core.mcp.inbox import _PREVIEW_FIELD_CAP

    agent, workdir = _mk_agent(tmp_path)
    long_body = "B" * (_PREVIEW_FIELD_CAP + 500)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice@example.com",
        "subject": "S" * 150,  # well under the 200 validate_event cap
        "body": long_body,
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)
    notif_file = workdir / ".notification" / "mcp.telegram.json"
    notif = _json.loads(notif_file.read_text(encoding="utf-8"))
    preview = notif["data"]["previews"][0]
    # Snippet hard-capped.
    assert len(preview["preview"]) == _PREVIEW_FIELD_CAP
    assert preview["preview"] == "B" * _PREVIEW_FIELD_CAP
    # from/subject pass through unchanged — no truncation at this layer.
    assert preview["from"] == "alice@example.com"
    assert preview["subject"] == "S" * 150


def test_scan_legacy_event_without_metadata_yields_only_base_preview_fields(tmp_path):
    """Phase-1 IM seam: events without metadata must produce the exact
    pre-existing preview shape — {from, subject, preview} with no extras.
    Behavioural baseline for the additive metadata change."""
    import json as _json

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello",
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)
    notif = _json.loads((workdir / ".notification" / "mcp.telegram.json").read_text(encoding="utf-8"))
    p = notif["data"]["previews"][0]
    assert set(p.keys()) == {"from", "subject", "preview"}
    assert p["from"] == "alice"
    assert p["subject"] == "hi"
    assert p["preview"] == "hello"


def test_scan_preserves_conversation_metadata_in_preview(tmp_path):
    """Phase-1 IM seam: when an event carries
    metadata.{conversation_ref, message_ref, platform}, those values
    surface in the per-event preview entry and in the notification
    instructions so the agent can route follow-up work to the right
    thread without duplicating body preview text."""
    import json as _json

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice",
        "subject": "hi",
        "body": "hello",
        "metadata": {
            "conversation_ref": "tg:chat:42",
            "message_ref": "tg:msg:9001",
            "platform": "telegram",
        },
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)
    notif = _json.loads((workdir / ".notification" / "mcp.telegram.json").read_text(encoding="utf-8"))
    p = notif["data"]["previews"][0]
    # Existing fields still pass through untouched.
    assert p["from"] == "alice"
    assert p["subject"] == "hi"
    assert p["preview"] == "hello"
    # New IM fields preserved.
    assert p["conversation_ref"] == "tg:chat:42"
    assert p["message_ref"] == "tg:msg:9001"
    assert p["platform"] == "telegram"
    # Instructions render routing metadata concisely, but not the body preview.
    assert "tg:chat:42" in notif["instructions"]
    assert "tg:msg:9001" in notif["instructions"]
    assert "hello" in p["preview"]
    assert "hello" not in notif["instructions"]


def test_scan_ignores_non_string_or_empty_metadata_values(tmp_path):
    """Non-string, empty, or unknown-key metadata values are silently
    dropped — a misbehaving MCP cannot inject arbitrary structures into
    the notification payload via this seam, and legacy/sloppy events do
    not become dead-lettered."""
    import json as _json

    from lingtai.core.mcp.inbox import _PREVIEW_META_FIELD_CAP

    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice",
        "subject": "hi",
        "body": "hello",
        "metadata": {
            "conversation_ref": "",                      # empty → ignored
            "message_ref": 12345,                        # non-string → ignored
            "platform": "x" * (_PREVIEW_META_FIELD_CAP + 100),  # oversize → capped
            "unrelated_field": {"nested": "junk"},       # unknown key → ignored
        },
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)
    notif = _json.loads((workdir / ".notification" / "mcp.telegram.json").read_text(encoding="utf-8"))
    p = notif["data"]["previews"][0]
    assert "conversation_ref" not in p
    assert "message_ref" not in p
    assert "unrelated_field" not in p
    assert p["platform"] == "x" * _PREVIEW_META_FIELD_CAP
    # Base fields still intact.
    assert p["from"] == "alice"


def test_scan_publishes_notification_even_when_wake_false(tmp_path):
    """Notification is always published regardless of wake flag."""
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {
        "from": "alice", "subject": "hi", "body": "hello", "wake": False,
    })

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif_file = workdir / ".notification" / "mcp.telegram.json"
    assert notif_file.exists(), "notification file should exist even with wake=False"


def test_scan_dead_letters_invalid_json(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram"
    target_dir.mkdir(parents=True)
    bad = target_dir / "bad.json"
    bad.write_text("not-json", encoding="utf-8")

    _scan_once(agent, workdir / INBOX_DIRNAME)

    # Original moved into .dead/ with sibling .error.json.
    assert not bad.exists()
    dead = target_dir / DEAD_DIRNAME / "bad.json"
    err = target_dir / DEAD_DIRNAME / "bad.error.json"
    assert dead.is_file()
    assert err.is_file()
    err_doc = json.loads(err.read_text())
    assert "invalid JSON" in err_doc["error"]


def test_scan_dead_letters_missing_required_field(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "ev1", {"from": "a", "subject": "hi"})

    _scan_once(agent, workdir / INBOX_DIRNAME)

    dead_dir = workdir / INBOX_DIRNAME / "telegram" / DEAD_DIRNAME
    assert (dead_dir / "ev1.json").is_file()
    err = json.loads((dead_dir / "ev1.error.json").read_text())
    assert "body" in err["error"]


def test_scan_ignores_tmp_files(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram"
    target_dir.mkdir(parents=True)
    half = target_dir / f"ev1{TMP_SUFFIX}"
    half.write_text(json.dumps({"from": "a", "subject": "s", "body": "b"}))

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 0
    assert half.exists()  # untouched


def test_scan_handles_multiple_mcps(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(workdir, "telegram", "t1", {
        "from": "a", "subject": "via tg", "body": "x",
    })
    _write_event(workdir, "feishu", "f1", {
        "from": "b", "subject": "via fs", "body": "y",
    })

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 2
    # One notification file per MCP.
    assert (workdir / ".notification" / "mcp.telegram.json").exists()
    assert (workdir / ".notification" / "mcp.feishu.json").exists()


def test_scan_skips_dotted_subdirs(tmp_path):
    """The .dead/ subdir contains processed-failed files; don't reprocess."""
    agent, workdir = _mk_agent(tmp_path)
    target_dir = workdir / INBOX_DIRNAME / "telegram" / DEAD_DIRNAME
    target_dir.mkdir(parents=True)
    (target_dir / "old.json").write_text(json.dumps({
        "from": "a", "subject": "s", "body": "b",
    }))

    dispatched = _scan_once(agent, workdir / INBOX_DIRNAME)
    assert dispatched == 0  # .dead/ is skipped


# ---------------------------------------------------------------------------
# Poller lifecycle
# ---------------------------------------------------------------------------

def test_poller_start_creates_inbox_root(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    try:
        assert (workdir / INBOX_DIRNAME).is_dir()
    finally:
        poller.stop()


def test_poller_dispatches_events_async(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    try:
        _write_event(workdir, "telegram", "ev1", {
            "from": "alice", "subject": "async", "body": "hello",
        })
        # Wait up to 2 poll cycles for delivery.
        notif_file = workdir / ".notification" / "mcp.telegram.json"
        deadline = time.monotonic() + (POLL_INTERVAL * 4 + 0.5)
        while time.monotonic() < deadline and not notif_file.exists():
            time.sleep(0.05)
        assert notif_file.exists(), "notification file not created within timeout"
    finally:
        poller.stop()


def test_poller_stop_idempotent(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    poller = MCPInboxPoller(agent)
    poller.start()
    poller.stop()
    poller.stop()  # second stop must not raise
