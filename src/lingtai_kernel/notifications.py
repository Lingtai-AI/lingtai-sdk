"""Notification filesystem — `.notification/` dropbox + sync primitives.

Producers write JSON files; the kernel reads them and syncs the agent's
wire context to match.  This module provides the file-level helpers
(fingerprint, collect, publish, clear).  The sync-loop logic — strip +
reinject into the wire — lives on :class:`BaseAgent`.

Naming convention:

* Kernel intrinsics write ``<intrinsic_name>.json`` (e.g. ``email.json``,
  ``soul.json``, ``system.json``).
* MCP-loaded servers write ``mcp.<server_name>.json`` (e.g.
  ``mcp.imap.json``, ``mcp.telegram.json``).

The basename is the *tool* whose namespace owns the notification.

Notification-file design rationale and staged implementation notes are
preserved in this module, its Git history, and related PR / issue records.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .workdir import workdir_layout


_CHANNEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Notification channels are intentionally allowlisted.  Unknown files in
# `.notification/` are ignored by readers and cannot be published/cleared
# through kernel helpers.  MCP bridge channels are allowlisted as a family
# because server names are dynamic but still owned by the MCP inbox contract.
_NOTIFICATION_CHANNEL_ALLOWLIST: set[str] = {
    "bash",
    "btw",
    "cron",
    "email",
    "goal",
    "molt",
    "nudge",
    "post-molt",
    "soul",
    "system",
    "tool_loop_guard",
}
_NOTIFICATION_CHANNEL_PREFIX_ALLOWLIST: tuple[str, ...] = ("mcp.",)

# Channels that are valid notification surfaces but must not be cleared via
# generic system.dismiss because they are source-of-truth files.
_PROTECTED_GENERIC_DISMISS: dict[str, str] = {
    "goal": (
        "Goal state lives in .notification/goal.json. Do not dismiss it. "
        "To cancel the goal, delete .notification/goal.json; to complete it, "
        "mark its status done/superseded or replace/delete the file. See the "
        "goal manual under system-manual for details."
    ),
}

# Channels whose generic dismissal would leak producer-owned state.
# Producers with durable unread/state mirrors register themselves here at
# import time so notification(action="dismiss_channel", channel=...) can refuse
# unsafe generic clears and point the agent at the producer-specific verb.
_GENERIC_DISMISS_GUARDED: dict[str, str] = {}

# System-notification event sources that must NOT be cleared via dismiss —
# neither by whole-channel clear (force or not), nor by targeted event_id/ref_id
# removal.  These reminders represent unfinished work whose only legitimate
# clear path is the action that actually resolves the work.
# NOTE: large_tool_result is intentionally absent here since PR #425 — those
# reminders may now be dismissed/acknowledged as an escape hatch for stale or
# pre-molt refs that can no longer be summarized.  Summarization remains the
# preferred discharge; dismissal prevents re-notification loops for the same
# tool_call_ids.
_UNDISMISSABLE_SYSTEM_EVENT_SOURCES: frozenset[str] = frozenset()

# Acknowledgement file inside .notification/ where dismissed large-result ref_ids
# are persisted so the rescan does not immediately recreate them.
_LARGE_RESULT_ACK_FILE = ".notification/large_result_acks.json"

# Agent-facing note included in the dismiss result to explain preferred path.
_LARGE_RESULT_DISMISS_NOTE = (
    "large_tool_result reminder acknowledged and removed. "
    "Summarization via system(action='summarize') remains the preferred way to "
    "discharge a large result — it records an agent-authored compact replacement "
    "in runtime history and auto-clears the reminder. Dismissal only clears the "
    "reminder surface; the original large result remains in chat history and "
    "events.jsonl."
)


def _is_undismissable_system_event(ev: object) -> bool:
    """Return True iff *ev* is a system event that dismiss must not remove."""
    return (
        isinstance(ev, dict)
        and ev.get("source") in _UNDISMISSABLE_SYSTEM_EVENT_SOURCES
    )


def _is_large_result_event(ev: object) -> bool:
    """Return True iff *ev* is a large_tool_result system event."""
    return isinstance(ev, dict) and ev.get("source") == "large_tool_result"


def load_large_result_acks(workdir: Path) -> set[str]:
    """Load acknowledged large-result ref_ids from the ack file.

    Returns an empty set when the file is absent or malformed.
    """
    ack_path = workdir / _LARGE_RESULT_ACK_FILE
    try:
        data = json.loads(ack_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {r for r in data if isinstance(r, str)}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return set()


def save_large_result_acks(workdir: Path, ref_ids: set[str]) -> None:
    """Persist the acknowledged large-result ref_ids to the ack file atomically.

    When *ref_ids* is empty the file is deleted (idempotent).
    """
    ack_path = workdir / _LARGE_RESULT_ACK_FILE
    if not ref_ids:
        try:
            ack_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        return
    ack_path.parent.mkdir(exist_ok=True)
    tmp = ack_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(ref_ids), ensure_ascii=False), encoding="utf-8")
    tmp.rename(ack_path)


def ack_large_result_refs(workdir: Path, ref_ids: set[str]) -> None:
    """Add *ref_ids* to the persistent acknowledgement store (union, not replace)."""
    existing = load_large_result_acks(workdir)
    save_large_result_acks(workdir, existing | ref_ids)


def purge_stale_large_result_acks(workdir: Path, current_ref_ids: set[str]) -> None:
    """Remove ack entries that are no longer part of the current large-result set.

    Called by the rescan path after it has determined the live set of pending
    large-result ref_ids so stale acks do not accumulate without bound.
    Only keeps acks whose ref_id is still in *current_ref_ids*.
    """
    existing = load_large_result_acks(workdir)
    if not existing:
        return
    live = existing & current_ref_ids
    if live != existing:
        save_large_result_acks(workdir, live)


def validate_channel_name(channel: str) -> None:
    """Validate the syntax of a `.notification/<channel>.json` channel name.

    The notification filesystem treats the channel as a filename stem.
    Generic dismiss accepts agent-supplied channel names, so it validates
    them before constructing a path. Producer-side publish/clear additionally
    validate allowlist membership before touching the filesystem.
    """
    if not isinstance(channel, str) or not channel:
        raise ValueError("channel must be a non-empty string")
    if ".." in channel:
        raise ValueError("channel must not contain '..'")
    if _CHANNEL_RE.fullmatch(channel) is None:
        raise ValueError(
            "channel must match ^[A-Za-z0-9][A-Za-z0-9_.-]*$"
        )


def is_channel_allowed(channel: str) -> bool:
    """Return whether ``channel`` is on the notification allowlist."""
    try:
        validate_channel_name(channel)
    except ValueError:
        return False
    if channel in _NOTIFICATION_CHANNEL_ALLOWLIST:
        return True
    return any(channel.startswith(prefix) for prefix in _NOTIFICATION_CHANNEL_PREFIX_ALLOWLIST)


def validate_allowed_channel(channel: str) -> None:
    """Validate syntax and allowlist membership for a notification channel."""
    validate_channel_name(channel)
    if not is_channel_allowed(channel):
        allowed = sorted(_NOTIFICATION_CHANNEL_ALLOWLIST)
        prefixes = list(_NOTIFICATION_CHANNEL_PREFIX_ALLOWLIST)
        raise ValueError(
            "notification channel is not allowlisted: "
            f"{channel!r}; allowed={allowed}; allowed_prefixes={prefixes}"
        )


def register_notification_channel(channel: str) -> None:
    """Allow an in-process producer to register an exact notification channel."""
    validate_channel_name(channel)
    _NOTIFICATION_CHANNEL_ALLOWLIST.add(channel)


def register_generic_dismiss_guard(channel: str, suggested_verb: str) -> None:
    """Guard a channel against accidental generic dismissal.

    Category-A producers (notifications that mirror durable producer state)
    call this at import time. Duplicate registration is idempotent; the
    newest suggested verb wins so producers can refine guidance.
    """
    validate_channel_name(channel)
    _GENERIC_DISMISS_GUARDED[channel] = str(suggested_verb)


def is_generic_dismiss_guarded(channel: str) -> str | None:
    """Return the producer-specific suggested verb if guarded."""
    return _GENERIC_DISMISS_GUARDED.get(channel)


def notification_fingerprint(workdir: Path) -> tuple:
    """Compute a content fingerprint of allowlisted `.notification/*.json`.

    Returns a tuple of ``(name, size, sha256)`` triples sorted by name.  Empty
    tuple if the directory is absent or empty.  Used to detect whether the
    producer-visible notification payload changed since the last poll.

    The fingerprint is intentionally byte-content-based rather than mtime-based:
    some chat/MCP producers rewrite byte-identical notification JSON on every poll,
    which used to create fresh mtimes and drive one notification injection per
    heartbeat even when the model-visible notification was unchanged. Semantically
    equivalent JSON with different whitespace or key order is still considered
    changed.
    """
    notif_dir = workdir_layout(workdir).notification_dir
    if not notif_dir.is_dir():
        return ()
    entries = []
    for f in notif_dir.iterdir():
        if not (f.is_file() and f.suffix == ".json" and is_channel_allowed(f.stem)):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        entries.append((f.name, len(data), hashlib.sha256(data).hexdigest()))
    return tuple(sorted(entries))


def collect_notifications(workdir: Path) -> dict:
    """Read `.notification/*.json` and return a dict keyed by stem.

    Keys are filenames without extension (``email``, ``soul``,
    ``mcp.telegram``, …).  Sorted iteration produces deterministic
    ordering so the agent's mental model is stable across reads.

    Returns ``{}`` if the directory is absent, empty, or all files are
    unparseable.  Malformed files are silently skipped — a buggy
    producer should not break the agent.  (Producer authors see the
    skip in their own logs and fix.)
    """
    notif_dir = workdir_layout(workdir).notification_dir
    if not notif_dir.is_dir():
        return {}
    out = {}
    for f in sorted(notif_dir.glob("*.json")):
        if not is_channel_allowed(f.stem):
            continue
        try:
            out[f.stem] = json.loads(f.read_bytes())
        except (json.JSONDecodeError, OSError):
            continue
    return out


def publish(workdir: Path, tool_name: str, payload: dict) -> None:
    """Write a notification file atomically (tmp + rename).

    ``tool_name`` is the stem — ``email``, ``soul``, ``mcp.telegram``, etc.
    Overwrites any prior content for that source.

    The atomicity is important: a reader doing ``listdir`` + ``read_bytes``
    while a producer is mid-write would see truncated JSON.  ``tmp +
    rename`` makes the rename appear atomically to readers.
    """
    validate_allowed_channel(tool_name)
    layout = workdir_layout(workdir)
    notif_dir = layout.notification_dir
    notif_dir.mkdir(exist_ok=True)
    target = layout.notification_file(tool_name)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.rename(target)


def clear(workdir: Path, tool_name: str) -> None:
    """Delete a producer's notification file.  Idempotent.

    Producers call this when their state empties (e.g. mail's unread
    count drops to 0).  Deletion changes the directory fingerprint, so
    the kernel's next sync tick will strip the wire's notification block.
    """
    validate_allowed_channel(tool_name)
    target = workdir_layout(workdir).notification_file(tool_name)
    try:
        target.unlink()
    except (FileNotFoundError, OSError):
        pass


def clear_with_result(workdir: Path, channel: str) -> bool:
    """Delete a notification file and report whether it existed.

    Unlike :func:`clear`, this helper is strict: only a missing file is an
    idempotent no-op. Other ``OSError`` subclasses propagate to the caller
    so agent-facing dismiss can surface honest failures.
    """
    validate_allowed_channel(channel)
    target = workdir_layout(workdir).notification_file(channel)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


def clear_large_result_reminders(agent, tool_call_ids) -> list[str]:
    """Remove large-result reminder events for *tool_call_ids* from system.json.

    This is the preferred discharge path: a *successful*
    system(action="summarize") of a tool_call_id clears its matching
    ``large_tool_result`` reminder automatically after recording an
    agent-authored compact replacement in runtime history.

    Generic notification dismiss (``dismiss_channel``/``dismiss_ref``) can also
    acknowledge and clear large-result reminder mirrors as an escape hatch —
    for stale or pre-molt refs that can no longer be summarized.  Dismissal
    clears the notification surface only; it does not delete or mutate the
    original tool result in chat history or events.jsonl.

    Matches by the canonical ``ref_id`` (``large_tool_result:{tool_call_id}``)
    AND ``source == "large_tool_result"`` so an unrelated event that happens to
    reuse the ref_id string is never silently dropped. Returns the list of
    ref_ids actually removed. Idempotent: missing file / no match is a no-op.

    Runs under the agent's ``_system_notification_lock`` (when available) so it
    does not race concurrent enqueues of new system events.
    """
    wanted_ref_ids = {
        f"large_tool_result:{tcid}" for tcid in tool_call_ids if tcid
    }
    if not wanted_ref_ids:
        return []

    target = workdir_layout(agent._working_dir).notification_file("system")

    def _do_clear() -> list[str]:
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(payload, dict):
            return []
        data_obj = payload.get("data")
        events = data_obj.get("events", []) if isinstance(data_obj, dict) else []
        if not isinstance(events, list):
            return []

        def _is_target(ev: object) -> bool:
            return (
                isinstance(ev, dict)
                and ev.get("source") == "large_tool_result"
                and ev.get("ref_id") in wanted_ref_ids
            )

        removed = [ev.get("ref_id") for ev in events if _is_target(ev)]
        if not removed:
            return []
        kept = [ev for ev in events if not _is_target(ev)]
        try:
            if kept:
                from datetime import datetime, timezone
                payload["header"] = (
                    f"{len(kept)} system notification"
                    f"{'s' if len(kept) != 1 else ''}"
                )
                payload["published_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                data = payload.get("data")
                if not isinstance(data, dict):
                    data = {}
                data["events"] = kept
                payload["data"] = data
                publish(agent._working_dir, "system", payload)
            else:
                clear_with_result(agent._working_dir, "system")
        except OSError:
            return []
        return [r for r in removed if r]

    lock = getattr(agent, "_system_notification_lock", None)
    if lock is not None:
        with lock:
            removed = _do_clear()
    else:
        removed = _do_clear()

    if removed:
        # Pending ACTIVE-state payloads may reference the now-removed events.
        if hasattr(agent, "_pending_notification_meta"):
            agent._pending_notification_meta = None
        if hasattr(agent, "_pending_notification_fp"):
            agent._pending_notification_fp = None
        try:
            agent._log(
                "large_result_reminder_cleared_by_summarize",
                removed_ref_ids=removed,
            )
        except Exception:
            pass
    return removed


def _channel_fingerprint_entry(fp: tuple | None, channel: str) -> tuple | None:
    """Return one channel's fingerprint entry from a directory fingerprint."""
    filename = f"{channel}.json"
    for entry in fp or ():
        try:
            if entry[0] == filename:
                return tuple(entry)
        except (IndexError, TypeError):
            continue
    return None


def _safe_version(entry: tuple | None) -> list | None:
    """Return a JSON/log-safe fingerprint representation."""
    return list(entry) if entry is not None else None


def _stale_channel_refusal(
    agent,
    channel: str,
    *,
    invoked_by: str,
    delivered: tuple | None,
    current: tuple,
) -> dict:
    delivered_version = _safe_version(delivered)
    current_version = _safe_version(current)
    try:
        agent._log(
            "notification_dismiss_refused",
            reason="stale_channel_version",
            channel=channel,
            invoked_by=invoked_by,
            forced=False,
            delivered_version=delivered_version,
            current_version=current_version,
        )
        if invoked_by == "system":
            agent._log(
                "system_dismiss_refused",
                reason="stale_channel_version",
                channel=channel,
                forced=False,
                delivered_version=delivered_version,
                current_version=current_version,
            )
    except Exception:
        pass
    return {
        "status": "error",
        "reason": "stale_channel_version",
        "channel": channel,
        "forced": False,
        "delivered_version": delivered_version,
        "current_version": current_version,
        "message": (
            f"Channel '{channel}' changed after the delivered notification "
            "version. Read the current notification state before dismissing, "
            "or pass force=true to knowingly clear it."
        ),
    }


def _ack_and_remove_large_result_events(
    agent,
    channel: str,
    *,
    invoked_by: str,
    force: bool,
    large_events: list,
    all_events: list,
    event_id: str | None,
    ref_id: str | None,
) -> dict:
    """Acknowledge and remove large-result reminder events from system.json.

    Called when dismiss would clear one or more large_tool_result events.
    Persists an acknowledgement so the rescan does not immediately recreate
    the same reminder for the same tool_call_ids. The original large result
    remains in chat history and events.jsonl unchanged.
    """
    large_ref_ids: set[str] = set()
    for ev in large_events:
        r = ev.get("ref_id") if isinstance(ev, dict) else None
        if r:
            large_ref_ids.add(r)

    # Persist the ack so rescan skips these ref_ids until they change.
    try:
        ack_and_remove = ack_large_result_refs
        ack_and_remove(agent._working_dir, large_ref_ids)
    except Exception:
        pass

    # Remove the large-result events (and any other matched events) from the list.
    kept = [ev for ev in all_events if ev not in large_events]

    try:
        if kept:
            from datetime import datetime, timezone
            current_payload: dict = {}
            try:
                target = workdir_layout(agent._working_dir).notification_file("system")
                current_payload = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                pass
            current_payload["header"] = (
                f"{len(kept)} system notification"
                f"{'s' if len(kept) != 1 else ''}"
            )
            from datetime import datetime, timezone
            current_payload["published_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            data = current_payload.get("data")
            if not isinstance(data, dict):
                data = {}
            data["events"] = kept
            current_payload["data"] = data
            publish(agent._working_dir, "system", current_payload)
        else:
            clear_with_result(agent._working_dir, "system")
    except OSError:
        pass

    if hasattr(agent, "_pending_notification_meta"):
        agent._pending_notification_meta = None
    if hasattr(agent, "_pending_notification_fp"):
        agent._pending_notification_fp = None

    try:
        agent._log(
            "large_result_reminder_dismissed",
            channel=channel,
            invoked_by=invoked_by,
            forced=bool(force),
            acked_ref_ids=sorted(large_ref_ids),
            event_id=event_id,
            ref_id=ref_id,
        )
    except Exception:
        pass

    result = {
        "status": "ok",
        "channel": channel,
        "cleared": True,
        "forced": bool(force),
        "acked_large_result_refs": sorted(large_ref_ids),
        "note": _LARGE_RESULT_DISMISS_NOTE,
    }
    if event_id:
        result["event_id"] = event_id
    if ref_id:
        result["ref_id"] = ref_id
    return result


def dismiss_channel(
    agent,
    channel: str,
    *,
    invoked_by: str,
    force: bool = False,
    reason: str | None = None,
    event_id: str | None = None,
    ref_id: str | None = None,
) -> dict:
    """Shared agent-facing notification dismissal helper.

    Used by the standalone ``notification`` tool's atomic dismiss verbs
    (``dismiss_channel``/``dismiss_event``/``dismiss_ref``, all with
    ``invoked_by="notification"``) and the ``soul(action="dismiss")``
    convenience alias. The ``system`` tool no longer exposes any dismiss verb.
    Generic dismiss clears only the notification surface; producer-owned state
    is untouched.

    ``reason`` is optional for ordinary generic channels. For the kernel-owned
    ``post-molt`` continuation channel it is required: clearing that reminder
    is the explicit continue/defer/obsolete acknowledgement requested by
    issue #184.
    """
    try:
        validate_allowed_channel(channel)
    except ValueError as e:
        try:
            agent._log(
                "notification_dismiss_invalid",
                channel=str(channel)[:100],
                invoked_by=invoked_by,
                error=str(e),
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "invalid_channel",
            "channel": channel,
            "message": str(e),
        }

    ack_reason = (reason or "").strip()
    if channel == "post-molt" and not ack_reason:
        try:
            agent._log(
                "notification_dismiss_missing_reason",
                channel=channel,
                invoked_by=invoked_by,
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "missing_ack_reason",
            "channel": channel,
            "message": (
                "post-molt continuation reminders require an acknowledgement "
                "reason. Use reason='<continue|defer|obsolete>: ...'."
            ),
        }

    protected_message = _PROTECTED_GENERIC_DISMISS.get(channel)
    if protected_message:
        try:
            agent._log(
                "notification_dismiss_protected",
                channel=channel,
                invoked_by=invoked_by,
                forced=bool(force),
            )
            if invoked_by == "system":
                agent._log(
                    "system_dismiss_protected",
                    channel=channel,
                    forced=bool(force),
                )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "protected_channel",
            "channel": channel,
            "message": protected_message,
        }

    if (event_id or ref_id) and channel != "system":
        return {
            "status": "error",
            "reason": "atomic_dismiss_requires_system_channel",
            "channel": channel,
            "event_id": event_id,
            "ref_id": ref_id,
            "message": "event_id/ref_id dismiss is only supported for channel='system'.",
        }

    suggested = is_generic_dismiss_guarded(channel)
    if suggested and not force:
        try:
            if invoked_by == "system":
                agent._log(
                    "system_dismiss_guarded",
                    channel=channel,
                    suggested_verb=suggested,
                )
            agent._log(
                "notification_dismiss_guarded",
                channel=channel,
                invoked_by=invoked_by,
                suggested_verb=suggested,
            )
        except Exception:
            pass
        return {
            "status": "error",
            "reason": "guarded",
            "channel": channel,
            "suggested_verb": suggested,
            "message": (
                f"Channel '{channel}' mirrors producer-owned state; use {suggested} "
                "or pass force=true only when knowingly clearing a stale mirror."
            ),
        }

    def _clear_current_channel() -> dict:
        if not force:
            fp = notification_fingerprint(agent._working_dir)
            current = _channel_fingerprint_entry(fp, channel)
            if current is not None:
                delivered = _channel_fingerprint_entry(
                    getattr(agent, "_notification_fp", ()),
                    channel,
                )
                if delivered != current:
                    return _stale_channel_refusal(
                        agent,
                        channel,
                        invoked_by=invoked_by,
                        delivered=delivered,
                        current=current,
                    )

        goal_reminder_cleared_by_whole_system_dismiss = False
        if channel == "system":
            large_result_events: list = []
            all_events: list = []
            try:
                payload = json.loads(workdir_layout(agent._working_dir).notification_file("system").read_text(encoding="utf-8"))
                data_obj = payload.get("data") if isinstance(payload, dict) else {}
                events = data_obj.get("events", []) if isinstance(data_obj, dict) else []
                all_events = events if isinstance(events, list) else []
                large_result_events = [
                    ev for ev in all_events if _is_large_result_event(ev)
                ]
                goal_reminder_cleared_by_whole_system_dismiss = any(
                    isinstance(ev, dict)
                    and ev.get("source") == "goal.reminder"
                    and str(ev.get("ref_id", "")).startswith("goal:")
                    for ev in all_events
                )
            except Exception:
                large_result_events = []
                all_events = []
                goal_reminder_cleared_by_whole_system_dismiss = False

            # A whole-channel system clear that includes large-result reminders
            # acks those reminders and removes them (rather than refusing).  The
            # remaining non-large-result events are also cleared by the full
            # channel dismiss below, but we handle the ack path here so the
            # rescan does not immediately recreate them.
            if large_result_events:
                # Collect all events for the ack-and-remove helper; it removes
                # only the large-result ones; remaining events follow normal path.
                non_large = [ev for ev in all_events if not _is_large_result_event(ev)]
                if not non_large:
                    # All events were large-result: ack-and-remove handles everything.
                    return _ack_and_remove_large_result_events(
                        agent, channel,
                        invoked_by=invoked_by, force=force,
                        large_events=large_result_events, all_events=all_events,
                        event_id=None, ref_id=None,
                    )
                # Mixed: ack large-result ones silently then continue to clear the whole channel.
                try:
                    ack_large_result_refs(
                        agent._working_dir,
                        {ev.get("ref_id") for ev in large_result_events if isinstance(ev, dict) and ev.get("ref_id")},
                    )
                    try:
                        agent._log(
                            "large_result_reminder_dismissed",
                            channel=channel,
                            invoked_by=invoked_by,
                            forced=bool(force),
                            acked_ref_ids=sorted(
                                ev.get("ref_id") for ev in large_result_events
                                if isinstance(ev, dict) and ev.get("ref_id")
                            ),
                            event_id=None,
                            ref_id=None,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
                # Fall through to clear the full channel (all events, including large-result).

        try:
            existed = clear_with_result(agent._working_dir, channel)
        except OSError as e:
            try:
                agent._log(
                    "notification_dismiss_error",
                    channel=channel,
                    invoked_by=invoked_by,
                    forced=bool(force),
                    error=str(e)[:200],
                )
            except Exception:
                pass
            return {
                "status": "error",
                "reason": "clear_failed",
                "channel": channel,
                "message": str(e),
            }

        if existed and goal_reminder_cleared_by_whole_system_dismiss:
            try:
                import time as _time
                agent._goal_reminder_last_dismissed_at = _time.time()
            except Exception:
                pass

        # Any pending ACTIVE-state payload may contain the dismissed channel.
        # Invalidate after a successful clear attempt; stale refusals above
        # leave newer notification state intact for later delivery.
        if hasattr(agent, "_pending_notification_meta"):
            agent._pending_notification_meta = None
        if hasattr(agent, "_pending_notification_fp"):
            agent._pending_notification_fp = None

        try:
            agent._log(
                "notification_dismiss",
                channel=channel,
                invoked_by=invoked_by,
                existed=existed,
                forced=bool(force),
                reason=ack_reason or None,
            )
            if invoked_by == "system":
                agent._log(
                    "system_dismiss",
                    channel=channel,
                    existed=existed,
                    forced=bool(force),
                    reason=ack_reason or None,
                )
            elif invoked_by == "soul":
                agent._log("soul_dismiss")
        except Exception:
            pass

        result = {
            "status": "ok",
            "channel": channel,
            "cleared": existed,
            "forced": bool(force),
        }
        if ack_reason:
            result["reason"] = ack_reason
        return result

    def _dismiss_system_event() -> dict:
        if not (event_id or ref_id):
            return _clear_current_channel()

        if not force:
            fp = notification_fingerprint(agent._working_dir)
            current = _channel_fingerprint_entry(fp, channel)
            if current is not None:
                delivered = _channel_fingerprint_entry(
                    getattr(agent, "_notification_fp", ()),
                    channel,
                )
                if delivered != current:
                    return _stale_channel_refusal(
                        agent,
                        channel,
                        invoked_by=invoked_by,
                        delivered=delivered,
                        current=current,
                    )

        target = workdir_layout(agent._working_dir).notification_file("system")
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except (json.JSONDecodeError, OSError) as e:
            return {
                "status": "error",
                "reason": "read_failed",
                "channel": channel,
                "message": str(e),
            }

        data_obj = payload.get("data")
        events = data_obj.get("events", []) if isinstance(data_obj, dict) else []
        if not isinstance(events, list):
            events = []

        def _match(ev: object) -> bool:
            if not isinstance(ev, dict):
                return False
            if event_id and ev.get("event_id") == event_id:
                return True
            if ref_id and ev.get("ref_id") == ref_id:
                return True
            return False
        removed_events = [ev for ev in events if _match(ev)]
        kept = [ev for ev in events if not _match(ev)]
        removed = len(removed_events)

        # Targeted event_id/ref_id dismiss that matches large-result reminders:
        # ack them and proceed with removal instead of refusing.
        large_matched = [ev for ev in removed_events if _is_large_result_event(ev)]
        if large_matched:
            return _ack_and_remove_large_result_events(
                agent, channel,
                invoked_by=invoked_by, force=force,
                large_events=large_matched, all_events=events,
                event_id=event_id, ref_id=ref_id,
            )

        if removed == 0:
            try:
                agent._log(
                    "notification_event_dismiss",
                    channel=channel,
                    invoked_by=invoked_by,
                    event_id=event_id,
                    ref_id=ref_id,
                    removed=0,
                    forced=bool(force),
                    reason=ack_reason or None,
                )
                if invoked_by == "system":
                    agent._log(
                        "system_event_dismiss",
                        event_id=event_id,
                        ref_id=ref_id,
                        removed=0,
                        forced=bool(force),
                        reason=ack_reason or None,
                    )
            except Exception:
                pass
            result = {
                "status": "ok",
                "channel": channel,
                "cleared": False,
                "removed": 0,
                "remaining": len(kept),
                "forced": bool(force),
            }
            if event_id:
                result["event_id"] = event_id
            if ref_id:
                result["ref_id"] = ref_id
            if ack_reason:
                result["reason"] = ack_reason
            return result

        try:
            if kept:
                from datetime import datetime, timezone
                payload["header"] = (
                    f"{len(kept)} system notification"
                    f"{'s' if len(kept) != 1 else ''}"
                )
                payload["published_at"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                data = payload.get("data")
                if not isinstance(data, dict):
                    data = {}
                data["events"] = kept
                payload["data"] = data
                publish(agent._working_dir, "system", payload)
            else:
                clear_with_result(agent._working_dir, "system")
        except OSError as e:
            return {
                "status": "error",
                "reason": "clear_failed",
                "channel": channel,
                "message": str(e),
            }

        if hasattr(agent, "_pending_notification_meta"):
            agent._pending_notification_meta = None
        if hasattr(agent, "_pending_notification_fp"):
            agent._pending_notification_fp = None

        try:
            if any(
                isinstance(ev, dict)
                and ev.get("source") == "goal.reminder"
                and str(ev.get("ref_id", "")).startswith("goal:")
                for ev in removed_events
            ):
                import time as _time
                agent._goal_reminder_last_dismissed_at = _time.time()
        except Exception:
            pass

        try:
            agent._log(
                "notification_event_dismiss",
                channel=channel,
                invoked_by=invoked_by,
                event_id=event_id,
                ref_id=ref_id,
                removed=removed,
                forced=bool(force),
                reason=ack_reason or None,
            )
            if invoked_by == "system":
                agent._log(
                    "system_event_dismiss",
                    event_id=event_id,
                    ref_id=ref_id,
                    removed=removed,
                    forced=bool(force),
                    reason=ack_reason or None,
                )
        except Exception:
            pass

        result = {
            "status": "ok",
            "channel": channel,
            "cleared": bool(removed),
            "removed": removed,
            "remaining": len(kept),
            "forced": bool(force),
        }
        if event_id:
            result["event_id"] = event_id
        if ref_id:
            result["ref_id"] = ref_id
        if ack_reason:
            result["reason"] = ack_reason
        return result

    if channel == "system":
        with agent._system_notification_lock:
            result = _dismiss_system_event()
    else:
        result = _clear_current_channel()

    if _dismiss_changed_surface(result):
        _signal_notification_dismissed(agent, channel)
    return result


def _dismiss_changed_surface(result: dict) -> bool:
    """Return True iff a dismiss result reflects a real change to the surface.

    A real change is what rewrites the resident ``_meta.notifications`` block on
    older tool results and therefore breaks the Codex WS incremental chain. A
    no-op (file already absent), a refusal (guarded/protected/stale/invalid), or
    a zero-match event dismiss leaves the surface untouched and must NOT trigger
    a fresh-epoch reset.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return False
    return bool(
        result.get("cleared")
        or result.get("removed")
        or result.get("acked_large_result_refs")
    )


def _signal_notification_dismissed(agent, channel: str) -> None:
    """Signal a notification-surface dismiss to the chat session's adapter.

    Best-effort and adapter-agnostic: forwards to ``on_notification_dismissed``
    on the chat session if the hook exists. The Codex adapter INTENTIONALLY
    treats this as a no-op (``CodexResponsesSession.on_notification_dismissed``):
    a dismiss is high-frequency housekeeping, not context compaction, and must
    NOT break the ``previous_response_id`` / strict-prefix continuation chain.
    Only ``on_history_summarized`` rewrites old tool-result payloads enough to
    require a fresh full epoch. (Earlier this hook was wired to force a fresh
    ``ws_full`` epoch on every dismiss; that footgun is now neutralized at the
    adapter — the ``_dismiss_changed_surface`` guard below is retained so the
    signal stays meaningful if the hook is ever re-armed.) Adapters without the
    hook treat the call as a no-op.
    """
    chat = getattr(agent, "_chat", None)
    if chat is None:
        return
    hook = getattr(chat, "on_notification_dismissed", None)
    if not callable(hook):
        return
    try:
        hook(channel)
    except Exception:  # pragma: no cover - defensive hook isolation
        try:
            agent._log(
                "notification_dismiss_hook_failed",
                channel=channel,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Producer-facing helper — the canonical "submit a notification" entry point
# ---------------------------------------------------------------------------


def submit(
    workdir: Path,
    tool_name: str,
    *,
    data: dict,
    header: str,
    icon: str = "🔔",
    priority: str = "normal",
    instructions: str | None = None,
) -> None:
    """Submit a notification with the standard envelope.

    This is the canonical entry point for in-process producers.  It
    wraps :func:`publish` with the envelope shape documented in the
    design (``notification-filesystem-redesign.md`` §2.1.3) and stamps
    ``published_at`` automatically.  Producers supply only what is
    semantically theirs:

    Args:
        workdir: The agent's working directory.
        tool_name: The producer's namespace key — ``email``, ``soul``,
            ``system``, ``mcp.<server>``, …  This becomes both the file
            basename (``<tool_name>.json``) AND the dict key the agent
            sees when it reads ``notification(action="check")``.
        data: Structured payload the agent will read.  No restrictions
            on shape — producers decide.
        header: One-line glanceable summary used by frontends (TUI
            status bar, portal cards) for compact rendering.
        icon: Optional glyph for status indicators.  Defaults to 🔔;
            common conventions: 📧 (mail), 🌊 (soul), 💬 (chat), …
        priority: ``"low"``, ``"normal"``, or ``"high"``.  Frontends
            may surface high-priority notifications more prominently.
        instructions: Optional agent-facing directive describing how to
            dismiss or act on this notification.  Surfaces as a
            top-level field in the wire JSON so the agent reads it
            inline with the rest of the envelope.  Producers that
            require an explicit dismissal action (e.g. email's
            ``read``) put the instructions here so the directive lives
            with the payload rather than in a static prompt.  Omit
            when the notification is purely informational.

    External producers that cannot import the kernel (e.g. MCP servers
    over SSH) should use :func:`publish` directly with the same
    envelope shape.  The contract is the filesystem layout; this
    helper is a Python-side ergonomics layer.

    Example::

        submit(agent._working_dir, "email",
               header=f"{n} unread",
               icon="📧",
               instructions="Call email(action='read', email_id=[...]) to dismiss handled mails.",
               data={"count": n, "previews": [...]})

    To clear a notification (e.g. when state empties) call
    :func:`clear` — there is no separate "submit empty" path.
    """
    from datetime import datetime, timezone

    payload = {
        "header": header,
        "icon": icon,
        "priority": priority,
        "published_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "data": data,
    }
    if instructions is not None:
        payload["instructions"] = instructions
    publish(workdir, tool_name, payload)
