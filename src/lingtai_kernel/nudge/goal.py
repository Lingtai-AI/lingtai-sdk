"""Goal reminder nudge.

A protected ``.notification/goal.json`` file is the goal source of truth.
When an active goal remains while the agent has been IDLE for the configured
reminder delay, this check publishes one short ``goal.reminder`` event into
``.notification/system.json``.  The reminder only says to inspect the goal;
the actual objective and instructions stay in ``goal.json``.
"""
from __future__ import annotations

import time
from typing import Any

from ..notifications import (
    _rewrite_system_events_locked,
    collect_notifications,
)
from ..state import AgentState


_DEFAULT_DELAY_SECONDS = 120.0
_REMINDER_BODY = "Goal reminder: read .notification/goal.json and follow its instructions; see the goal manual under system-manual."


def check(agent) -> None:
    """Publish a short system reminder when an active goal survives IDLE."""
    # Cheap gate before touching disk: goal reminders are IDLE-only.
    if getattr(agent, "_state", None) != AgentState.IDLE:
        return

    notifications = collect_notifications(agent._working_dir)
    goal = notifications.get("goal")
    if not isinstance(goal, dict) or not _is_active(goal):
        _clear_goal_reminders(agent, keep_ref_id=None)
        try:
            agent._goal_reminder_last_goal_ref = None
            agent._goal_reminder_last_published_ref = None
        except Exception:
            pass
        return

    now = time.time()
    idle_since = float(getattr(agent, "_state_changed_at", now) or now)
    delay = _reminder_delay(agent, goal)
    if now - idle_since < delay:
        return

    goal_id = _goal_id(goal)
    ref_id = f"goal:{goal_id}"
    _clear_goal_reminders(agent, keep_ref_id=ref_id)

    # If the same goal reminder is already present, do not duplicate it.
    system = collect_notifications(agent._working_dir).get("system", {})
    for event in _system_events(system):
        if isinstance(event, dict) and event.get("source") == "goal.reminder" and event.get("ref_id") == ref_id:
            return

    last_dismissed = float(getattr(agent, "_goal_reminder_last_dismissed_at", 0.0) or 0.0)
    if last_dismissed and now - last_dismissed < delay:
        return

    agent._enqueue_system_notification(
        source="goal.reminder",
        ref_id=ref_id,
        body=_REMINDER_BODY,
    )
    agent._goal_reminder_last_goal_ref = ref_id
    agent._goal_reminder_last_published_ref = ref_id
    try:
        agent._log(
            "goal_reminder_published",
            ref_id=ref_id,
            delay_seconds=delay,
            idle_seconds=round(now - idle_since, 3),
        )
    except Exception:
        pass


def _is_active(goal: dict[str, Any]) -> bool:
    data = goal.get("data") if isinstance(goal.get("data"), dict) else {}
    status = goal.get("status", data.get("status", "active"))
    return str(status or "active").lower() not in {"done", "complete", "completed", "superseded", "cancelled", "canceled", "inactive"}


def _goal_id(goal: dict[str, Any]) -> str:
    data = goal.get("data") if isinstance(goal.get("data"), dict) else {}
    raw = goal.get("id") or data.get("id") or goal.get("title") or data.get("title") or "current"
    text = str(raw).strip() or "current"
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in text)
    return safe[:80] or "current"


def _reminder_delay(agent, goal: dict[str, Any]) -> float:
    data = goal.get("data") if isinstance(goal.get("data"), dict) else {}
    raw = goal.get("reminder_delay_seconds", data.get("reminder_delay_seconds"))
    if raw is None:
        raw = getattr(agent, "_soul_delay", _DEFAULT_DELAY_SECONDS)
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = _DEFAULT_DELAY_SECONDS
    if delay <= 0:
        delay = _DEFAULT_DELAY_SECONDS
    return max(1.0, delay)


def _system_events(system: Any) -> list:
    if not isinstance(system, dict):
        return []
    data = system.get("data")
    events = data.get("events", []) if isinstance(data, dict) else []
    return events if isinstance(events, list) else []


def _is_goal_reminder_event(event: object) -> bool:
    return isinstance(event, dict) and event.get("source") == "goal.reminder"


def _clear_goal_reminders(agent, *, keep_ref_id: str | None) -> None:
    """Remove stale goal.reminder system events.

    ``keep_ref_id`` is the currently active goal reminder ref. ``None`` means no
    goal is active, so all goal reminder events are stale.
    """
    lock = getattr(agent, "_system_notification_lock", None)
    if lock is None:
        _clear_goal_reminders_locked(agent, keep_ref_id=keep_ref_id)
    else:
        with lock:
            _clear_goal_reminders_locked(agent, keep_ref_id=keep_ref_id)


def _clear_goal_reminders_locked(agent, *, keep_ref_id: str | None) -> None:
    notifications = collect_notifications(agent._working_dir)
    system = notifications.get("system", {})
    events = _system_events(system)
    if not events:
        return

    def _keep(event: object) -> bool:
        if not _is_goal_reminder_event(event):
            return True
        if keep_ref_id is not None and isinstance(event, dict) and event.get("ref_id") == keep_ref_id:
            return True
        return False

    kept = [event for event in events if _keep(event)]
    if len(kept) == len(events):
        return

    try:
        payload = dict(system) if isinstance(system, dict) else {}
        _rewrite_system_events_locked(agent, kept, payload=payload)
    except Exception as e:
        try:
            agent._log("goal_reminder_clear_error", error=str(e)[:200])
        except Exception:
            pass
        return

    try:
        agent._log(
            "goal_reminder_cleared",
            keep_ref_id=keep_ref_id,
            removed=len(events) - len(kept),
        )
    except Exception:
        pass
