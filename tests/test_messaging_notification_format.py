"""Tests for unread-digest body formatting.

These tests cover the prose body produced by
``_render_unread_digest`` — which is the renderer that built the old
per-arrival ``system(action="notification")`` body and now builds the
single ``email(action="unread")`` digest body. The same edge cases
apply (subject placeholder, sent_at vs received_at fallback, time-blind
agents) — they just live one layer deeper in the code now.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from lingtai_kernel.intrinsics.email.primitives import _render_unread_digest


def _make_agent(tmp_path: Path, *, time_awareness: bool = True, lang: str = "en"):
    """Minimal agent stub good enough for _render_unread_digest to run."""
    agent = MagicMock()
    agent._working_dir = tmp_path
    agent._config = SimpleNamespace(
        language=lang,
        time_awareness=time_awareness,
        timezone_awareness=False,
    )
    agent._mailbox_name = "email box"
    agent._mailbox_tool = "email"
    return agent


def _persist_inbox(tmp_path: Path, payload: dict) -> str:
    """Write payload as mailbox/inbox/{uuid}/message.json. Returns the id."""
    email_id = str(uuid4())
    msg_dir = tmp_path / "mailbox" / "inbox" / email_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    full = {"_mailbox_id": email_id, **payload}
    (msg_dir / "message.json").write_text(json.dumps(full, indent=2))
    return email_id


def test_notification_uses_received_at_when_sent_at_missing(tmp_path):
    """The TUI/kernel sender path doesn't populate sent_at, only
    received_at. The digest must still surface a timestamp."""
    agent = _make_agent(tmp_path)
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "hello",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, count, _ = _render_unread_digest(agent)
    assert count == 1
    assert "2026-05-04T10:00:00Z" in body


def test_notification_subject_placeholder_when_empty(tmp_path):
    """Empty-string subject renders as the localized '(no subject)'
    placeholder, not as a bare label with nothing after it."""
    agent = _make_agent(tmp_path, lang="en")
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "(no subject)" in body


def test_notification_subject_placeholder_when_missing(tmp_path):
    """Same fallback fires when the subject key is absent entirely
    (defensive — covers external addons that don't include the key)."""
    agent = _make_agent(tmp_path, lang="en")
    _persist_inbox(tmp_path, {
        "from": "alice",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "(no subject)" in body


def test_notification_subject_placeholder_localized_zh(tmp_path):
    """zh locale uses （无主题）."""
    agent = _make_agent(tmp_path, lang="zh")
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "（无主题）" in body


def test_notification_subject_placeholder_localized_wen(tmp_path):
    """wen (classical) locale uses （无题）."""
    agent = _make_agent(tmp_path, lang="wen")
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "（无题）" in body


def test_notification_timestamp_blank_when_time_blind(tmp_path):
    """time_awareness=False must blank the timestamp — even when
    received_at is populated, the agent should not see it."""
    agent = _make_agent(tmp_path, time_awareness=False)
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "hello",
        "message": "hi",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "2026-05-04T10:00:00Z" not in body


def test_notification_per_entry_prefers_sent_at_over_received_at(tmp_path):
    """Per-entry "Sent at:" uses sent_at when both are present (authorial
    intent). The digest header line still uses received_at (ledger of
    when this snapshot was taken), so received_at also appears in the
    overall body — but the per-entry line carries sent_at."""
    agent = _make_agent(tmp_path)
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "hello",
        "message": "hi",
        "sent_at": "2026-05-04T09:00:00Z",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    # Per-entry line carries sent_at
    assert "2026-05-04T09:00:00Z" in body


def test_notification_per_entry_prefers_time_over_received_at(tmp_path):
    """Per-entry rendering: legacy `time` field beats received_at in the
    fallback chain when sent_at is absent."""
    agent = _make_agent(tmp_path)
    _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "hello",
        "message": "hi",
        "time": "2026-05-04T08:00:00Z",
        "received_at": "2026-05-04T10:00:00Z",
    })

    body, _, _ = _render_unread_digest(agent)
    assert "2026-05-04T08:00:00Z" in body


def test_notification_digest_includes_mailbox_id(tmp_path):
    """Each digest entry exposes the mailbox ID so the agent can pass
    it to email(action="read"|"dismiss") without a separate check."""
    agent = _make_agent(tmp_path)
    eid = _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "ping",
        "message": "hello",
        "received_at": "2026-05-04T08:00:00Z",
    })
    body, _, _ = _render_unread_digest(agent)
    assert eid in body, f"ID {eid!r} missing from digest:\n{body}"


def test_notification_digest_includes_mailbox_id_zh(tmp_path):
    """Same for the zh template."""
    agent = _make_agent(tmp_path, lang="zh")
    eid = _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "ping",
        "message": "hello",
        "received_at": "2026-05-04T08:00:00Z",
    })
    body, _, _ = _render_unread_digest(agent)
    assert eid in body

def test_notification_digest_prefers_directory_mailbox_id_over_stale_payload(tmp_path):
    """Digest IDs come from the mailbox directory, not a stale JSON field.

    The filesystem directory is the lookup key that email(action="read"|"dismiss")
    accepts. If a copied or externally written message carries an old
    ``_mailbox_id`` inside ``message.json``, the notification digest must still
    expose the real directory id.
    """
    agent = _make_agent(tmp_path)
    real_id = _persist_inbox(tmp_path, {
        "from": "alice",
        "subject": "ping",
        "message": "hello",
        "received_at": "2026-05-04T08:00:00Z",
    })
    msg_file = tmp_path / "mailbox" / "inbox" / real_id / "message.json"
    msg = json.loads(msg_file.read_text())
    msg["_mailbox_id"] = "phantom-id-zzzz"
    msg_file.write_text(json.dumps(msg, indent=2))

    body, _, _ = _render_unread_digest(agent)

    assert real_id in body
    assert "phantom-id-zzzz" not in body
