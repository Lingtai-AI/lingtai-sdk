"""Time-awareness veil — strips time perception from LLM-visible surfaces.

The agent's config carries `time_awareness: bool`. When false, any timestamp
that would be rendered to the LLM is blanked or dropped. On-disk audit trails,
logs, scheduling math, and other internal uses of time are untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone

TIME_KEYS: tuple[str, ...] = (
    "received_at", "sent_at", "deliver_at", "time",
    "scheduled_at", "last_sent_at", "estimated_finish",
    "current_time", "started_at", "uptime_seconds",
    "stamina", "stamina_left", "ts", "date",
)


def now_iso(agent) -> str:
    """Return current ISO-8601 timestamp, or '' if the agent is time-blind.

    When timezone_awareness=True (default), returns OS local time with
    ±HH:MM offset (e.g. '2026-04-15T16:01:00-07:00'). When False, returns
    UTC with Z suffix (e.g. '2026-04-15T23:01:00Z').
    """
    if not agent._config.time_awareness:
        return ""
    if agent._config.timezone_awareness:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def veil(agent, ts: str) -> str:
    """Return ts unchanged if time-aware, else ''.

    Use when you already have a timestamp string (e.g., read from disk)
    that you're about to surface to the LLM.
    """
    if not agent._config.time_awareness:
        return ""
    return ts


def scrub_time_fields(
    agent,
    payload: dict,
    keys: tuple[str, ...] = TIME_KEYS,
    drop_keys: tuple[str, ...] = (),
) -> dict:
    """Return a shallow copy of payload with time fields neutralised.

    - If time-aware: returns payload unchanged (same object).
    - If time-blind: keys in `keys` are blanked to ''. Keys in `drop_keys`
      are removed entirely from the returned dict.
    """
    if agent._config.time_awareness:
        return payload
    out = dict(payload)
    for k in keys:
        if k in out:
            out[k] = ""
    for k in drop_keys:
        out.pop(k, None)
    return out
