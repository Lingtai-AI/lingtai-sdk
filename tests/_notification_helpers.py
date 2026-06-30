"""Shared test helpers for the notification-cluster tests.

These were previously copy-pasted verbatim between ``test_notification_tool.py``
and ``test_system_dismiss.py``.  They are plain helpers (not pytest fixtures) so
the existing test bodies can keep calling them by name with no signature churn —
each module just imports the names it uses.

Note: ``test_system_notifications.py`` deliberately keeps its own, differently
shaped ``_StubAgent`` (it carries ``_tc_inbox``/``_session`` for the wire/inbox
integration path) and is intentionally not unified here.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lingtai_kernel.notifications import notification_fingerprint, publish


@dataclass
class StubAgent:
    """Minimal agent stub for the atomic dismiss/notification paths."""

    _working_dir: Path
    _logs: list[tuple[str, dict]] = field(default_factory=list)
    _notification_fp: tuple = ()
    _system_notification_lock: threading.Lock = field(default_factory=threading.Lock)

    def _log(self, event_type: str, **fields: Any) -> None:
        self._logs.append((event_type, fields))


def events(agent: StubAgent, name: str) -> list[dict]:
    """Return the field dicts of every ``_log`` call of type *name*."""
    return [fields for event, fields in agent._logs if event == name]


def mark_delivered(agent: StubAgent) -> None:
    """Stamp the agent's notification fingerprint as current (delivered)."""
    agent._notification_fp = notification_fingerprint(agent._working_dir)


def publish_large_result_reminder(
    tmp_path: Path,
    *,
    tool_call_id: str = "toolu_big",
    extra_events: list[dict] | None = None,
) -> None:
    """Publish a ``system.json`` containing one ``large_tool_result`` reminder.

    Any *extra_events* are prepended before the reminder event.  *tmp_path* (and
    any missing parents) is created first so callers may pass a nested,
    not-yet-existing working dir.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    events_payload = [
        {
            "event_id": "evt_lr",
            "source": "large_tool_result",
            "ref_id": f"large_tool_result:{tool_call_id}",
            "body": "summarize me",
        }
    ]
    if extra_events:
        events_payload = list(extra_events) + events_payload
    publish(
        tmp_path,
        "system",
        {
            "header": f"{len(events_payload)} system notifications",
            "icon": "🔔",
            "priority": "normal",
            "published_at": "2026-06-20T00:00:00Z",
            "data": {"events": events_payload},
        },
    )
