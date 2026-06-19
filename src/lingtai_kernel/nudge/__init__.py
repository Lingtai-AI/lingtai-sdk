"""Per-agent nudges — periodic checks that emit a notification when
something needs the agent's attention.

Each check is a self-contained module exposing ``check(agent) -> None``.
The check owns its own throttle/dedupe state (stored as attributes on the
agent instance) and decides which notification surface it owns. Most nudges
upsert/remove entries in the shared ``.notification/nudge.json`` payload;
goal reminders read protected ``.notification/goal.json`` and publish short
``goal.reminder`` events into ``.notification/system.json``.

Channel ``.notification/nudge.json`` carries a list of active nudges:

    {
      "header": "<rendered by _render_header — e.g. '2 nudges'>",
      "icon": "🔔",
      "priority": "low",
      "instructions": "Call system(action='dismiss', channel='nudge') ...",
      "data": {"nudges": [{"kind": "kernel_version", ...}, ...]}
    }

Each check identifies its slot by a unique ``kind`` string. ``upsert``
replaces (or appends) one entry; ``remove`` deletes one. When the last
entry leaves, the channel file is cleared so the agent's wire surface
drops the notification entirely. The agent dismisses everything at once
with ``system(action='dismiss', channel='nudge')``.

To add a new nudge: drop ``nudge/<name>.py`` exposing ``check(agent)``,
then add an import + dispatch line to :func:`run_checks` below. No
registry, no protocol — keep the surface flat.

Concurrency: the RMW upsert/remove path holds a small per-agent lock
because multiple checks running on the same heartbeat tick would
otherwise lose entries. The lock is created lazily on first use.
"""
from __future__ import annotations
import threading

from . import kernel_version, goal


__all__ = ["run_checks", "upsert", "remove"]


def run_checks(agent) -> None:
    """Run all registered nudge checks for this agent.

    Cheap on the steady path: each check throttles itself and short-
    circuits before any disk I/O. A failing check is logged but does
    not block subsequent checks.
    """
    _run_one(agent, "kernel_version", kernel_version.check)
    _run_one(agent, "goal", goal.check)


def _run_one(agent, name: str, fn) -> None:
    try:
        fn(agent)
    except Exception as e:
        try:
            agent._log("nudge_check_error", kind=name, error=str(e)[:200])
        except Exception:
            pass


def upsert(agent, kind: str, body: dict) -> None:
    """Replace or append the nudge entry for ``kind``.

    ``body`` is the per-kind payload the check wants the agent to read.
    It is merged into the entry as-is, with ``"kind": kind`` stamped on
    top so the slot key is always present.
    """
    _modify(agent, lambda entries: _replace_kind(entries, kind, body))


def remove(agent, kind: str) -> None:
    """Drop the nudge entry for ``kind``. No-op if absent."""
    _modify(agent, lambda entries: [e for e in entries if e.get("kind") != kind])


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _replace_kind(entries: list, kind: str, body: dict) -> list:
    out = [e for e in entries if e.get("kind") != kind]
    entry = dict(body)
    entry["kind"] = kind
    out.append(entry)
    return out


def _modify(agent, mutate) -> None:
    """Read `nudge.json`, apply ``mutate(entries) -> new_entries``, write.

    Clears the channel file entirely when the resulting list is empty so
    the wire notification surface drops cleanly.
    """
    from ..notifications import collect_notifications, submit, clear

    lock = getattr(agent, "_nudge_channel_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._nudge_channel_lock = lock

    with lock:
        current = collect_notifications(agent._working_dir).get("nudge", {})
        existing = current.get("data", {}).get("nudges", [])
        if not isinstance(existing, list):
            existing = []
        new_entries = mutate(list(existing))

        if not new_entries:
            try:
                clear(agent._working_dir, "nudge")
            except Exception:
                pass
            return

        submit(
            agent._working_dir,
            "nudge",
            header=_render_header(new_entries),
            icon="🔔",
            priority="low",
            instructions=(
                "Call system(action='dismiss', channel='nudge') to "
                "acknowledge and clear ALL nudges at once. Individual "
                "nudges may also describe a specific action to take "
                "(e.g. system(action='refresh') for a kernel upgrade)."
            ),
            data={"nudges": new_entries},
        )


def _render_header(entries: list) -> str:
    n = len(entries)
    return f"{n} nudge{'s' if n != 1 else ''}"
