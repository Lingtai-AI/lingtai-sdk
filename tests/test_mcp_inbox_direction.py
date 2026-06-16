"""Regression tests for issue #170 (Part C).

Outgoing / bot-sent messages must not be classified as new human messages.
A surfaced outgoing echo can carry a username-like ``from`` value, which the
legacy ``from``-only heuristic would have flipped into a misleading
``[HUMAN]`` / high-priority notification. The inbox now honors the
producer's direction signal (``_direction``/``direction``/``from_me``, at the
top level or inside ``metadata``) and excludes outgoing events from the
human-message classification, while genuine inbound human messages still flag.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from lingtai.agent import Agent
from lingtai.core.mcp.inbox import (
    INBOX_DIRNAME,
    TMP_SUFFIX,
    _is_outgoing_event,
    _scan_once,
)


def _make_mock_service():
    svc = MagicMock()
    svc.get_adapter.return_value = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    return svc


def _mk_agent(tmp_path: Path):
    workdir = tmp_path / "agent"
    return (
        Agent(
            service=_make_mock_service(),
            agent_name="test",
            working_dir=workdir,
            capabilities={"mcp": {}},
        ),
        workdir,
    )


def _write_event(workdir: Path, mcp_name: str, event_id: str, event: dict) -> Path:
    target_dir = workdir / INBOX_DIRNAME / mcp_name
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_dir / f"{event_id}{TMP_SUFFIX}"
    final = target_dir / f"{event_id}.json"
    tmp.write_text(json.dumps(event), encoding="utf-8")
    tmp.rename(final)
    return final


def _read_notification(workdir: Path, mcp_name: str) -> dict:
    notif_file = workdir / ".notification" / f"mcp.{mcp_name}.json"
    assert notif_file.exists(), f"missing notification for {mcp_name}"
    return json.loads(notif_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Unit: _is_outgoing_event direction detection
# ---------------------------------------------------------------------------


def test_is_outgoing_event_top_level_direction():
    assert _is_outgoing_event({"from": "me", "_direction": "outgoing"}) is True
    assert _is_outgoing_event({"from": "me", "direction": "outgoing"}) is True


def test_is_outgoing_event_from_me_boolean():
    assert _is_outgoing_event({"from": "me", "from_me": True}) is True


def test_is_outgoing_event_metadata_direction():
    assert _is_outgoing_event({"from": "me", "metadata": {"_direction": "outgoing"}}) is True
    assert _is_outgoing_event({"from": "me", "metadata": {"from_me": True}}) is True


def test_is_outgoing_event_incoming_and_legacy_are_inbound():
    # Explicit incoming.
    assert _is_outgoing_event({"from": "alice", "_direction": "incoming"}) is False
    # Legacy event with no marker at all.
    assert _is_outgoing_event({"from": "alice"}) is False
    # Non-string / malformed markers are ignored (fail-open to inbound).
    assert _is_outgoing_event({"from": "alice", "_direction": 1}) is False
    assert _is_outgoing_event({"from": "alice", "metadata": "nope"}) is False
    assert _is_outgoing_event({"from": "alice", "from_me": "true"}) is False  # not bool True


# ---------------------------------------------------------------------------
# End-to-end: classification through _scan_once -> .notification
# ---------------------------------------------------------------------------


def test_outgoing_message_not_counted_as_human(tmp_path):
    """An outgoing event (username-like ``from``) must not flag human/high."""
    agent, workdir = _mk_agent(tmp_path)
    _write_event(
        workdir,
        "telegram",
        "ev1",
        {
            "from": "agentbot",
            "subject": "telegram message from agentbot",
            "body": "ack: done",
            "_direction": "outgoing",
        },
    )

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif = _read_notification(workdir, "telegram")
    assert notif["data"]["has_human_messages"] is False
    assert notif["priority"] == "normal"


def test_outgoing_via_metadata_not_counted_as_human(tmp_path):
    agent, workdir = _mk_agent(tmp_path)
    _write_event(
        workdir,
        "telegram",
        "ev1",
        {
            "from": "agentbot",
            "subject": "echo",
            "body": "ack",
            "metadata": {"from_me": True},
        },
    )

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif = _read_notification(workdir, "telegram")
    assert notif["data"]["has_human_messages"] is False
    assert notif["priority"] == "normal"


def test_inbound_human_message_still_flagged(tmp_path):
    """A genuine inbound human message must still flag human/high (no regression)."""
    agent, workdir = _mk_agent(tmp_path)
    _write_event(
        workdir,
        "telegram",
        "ev1",
        {
            "from": "alice",
            "subject": "telegram message from alice",
            "body": "please review the PR",
            "_direction": "incoming",
        },
    )

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif = _read_notification(workdir, "telegram")
    assert notif["data"]["has_human_messages"] is True
    assert notif["priority"] == "high"


def test_legacy_inbound_event_without_marker_still_flagged(tmp_path):
    """Legacy producers omit any direction marker — must remain inbound human."""
    agent, workdir = _mk_agent(tmp_path)
    _write_event(
        workdir,
        "telegram",
        "ev1",
        {"from": "bob", "subject": "hi", "body": "hello"},
    )

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif = _read_notification(workdir, "telegram")
    assert notif["data"]["has_human_messages"] is True
    assert notif["priority"] == "high"


def test_mixed_batch_flags_human_for_the_inbound_one(tmp_path):
    """An outgoing echo alongside a real inbound message still flags human."""
    agent, workdir = _mk_agent(tmp_path)
    _write_event(
        workdir,
        "telegram",
        "ev1",
        {"from": "agentbot", "subject": "echo", "body": "ack", "_direction": "outgoing"},
    )
    _write_event(
        workdir,
        "telegram",
        "ev2",
        {"from": "carol", "subject": "question", "body": "are you there?"},
    )

    _scan_once(agent, workdir / INBOX_DIRNAME)

    notif = _read_notification(workdir, "telegram")
    assert notif["data"]["has_human_messages"] is True
    assert notif["priority"] == "high"
