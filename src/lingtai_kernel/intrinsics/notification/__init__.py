"""Notification intrinsic — the standalone notification surface.

This intrinsic owns the notification-facing verbs: reading the live
notification surface and clearing notification mirrors.  It is the **only**
agent-callable home for those operations — the ``system`` tool no longer
exposes any notification verb (no ``notification``/``dismiss`` compatibility
alias).  ``system`` retains ``summarize`` (and the lifecycle/karma actions);
``summarize`` is *not* a notification verb and stays under ``system``.

Dismissal is **atomic**: there is no single kitchen-sink ``dismiss``.  Each
removal target has its own action so the API expresses exactly what is being
cleared:

Actions:
    check          — voluntary read of the live notification surface.  Returns
                     a placeholder dict; the turn loop's meta-block post-hook
                     stamps the canonical ``notifications`` +
                     ``_notification_guidance`` payload onto this same result.
    dismiss_channel — clear one ``.notification/<channel>.json`` surface whole.
                     Rejects ``event_id``/``ref_id`` (those are atomic-event
                     verbs).  Producer-owned state is never touched; guarded
                     mirrors refuse without ``force``.
    dismiss_event  — remove a single ``system`` event by ``event_id`` from
                     ``.notification/system.json``.
    dismiss_ref    — remove ``system`` event(s) by ``ref_id`` from
                     ``.notification/system.json``.

All three dismiss verbs delegate to the single canonical
:func:`lingtai_kernel.notifications.dismiss_channel` with
``invoked_by="notification"``.  The decision logic (allowlist, ``post-molt``
ack-reason, protected channels, generic-dismiss guard, and stale-channel-version
refusal) lives there, so every guard holds through this tool by construction.
``large_tool_result`` reminders may be dismissed as an escape hatch (e.g. for
stale or pre-molt refs that can no longer be summarized); doing so acknowledges
the ref_id so the rescan does not immediately recreate the same notification.
Summarization via ``system(action="summarize")`` remains the preferred discharge
and auto-clears the matching reminder.
"""
from __future__ import annotations

# Schema (tool registration).
from .schema import get_description, get_schema  # noqa: F401

# Single-source delegate — the canonical dismissal helper.  No notification
# logic is reimplemented here.
from ...notifications import dismiss_channel


# Placeholder returned by ``check`` — the live payload (``notifications`` +
# ``_notification_guidance``) is stamped onto this same result dict by
# ``attach_active_notifications`` in the turn loop.  Returning a dict (not a
# string) is what makes that stamp possible: the meta-block walks backward for
# the freshest *dict-shaped* tool result.
_CHECK_PLACEHOLDER_MESSAGE = (
    "Voluntary notification(action=check) read. The live notification payload "
    "is delivered via the kernel meta-block under the `notifications` and "
    "`_notification_guidance` keys on this same result. If those keys are "
    "absent, no notifications are active."
)


def _check(agent, args: dict) -> dict:
    """Voluntary read of the notification surface — returns a placeholder."""
    return {
        "_notification_placeholder": True,
        "message": _CHECK_PLACEHOLDER_MESSAGE,
    }


def _dismiss_channel(agent, args: dict) -> dict:
    """Clear one notification channel whole.

    Atomic event verbs (``event_id``/``ref_id``) are not accepted here; use
    ``dismiss_event`` / ``dismiss_ref`` for targeted removal.
    """
    channel = args.get("channel")
    if channel is None:
        agent._log("notification_dismiss_missing_channel")
        return {
            "status": "error",
            "reason": "missing_channel",
            "message": "notification(action='dismiss_channel') requires channel=<name>.",
        }
    if args.get("event_id") or args.get("ref_id"):
        return {
            "status": "error",
            "reason": "channel_dismiss_rejects_event_target",
            "channel": channel,
            "message": (
                "dismiss_channel clears a whole channel; use dismiss_event "
                "(event_id=...) or dismiss_ref (ref_id=...) for a single "
                "system event."
            ),
        }
    return dismiss_channel(
        agent,
        channel,
        invoked_by="notification",
        force=bool(args.get("force", False)),
        reason=args.get("reason"),
    )


def _dismiss_event(agent, args: dict) -> dict:
    """Remove a single ``system`` event by ``event_id``."""
    event_id = args.get("event_id")
    if not event_id:
        agent._log("notification_dismiss_missing_event_id")
        return {
            "status": "error",
            "reason": "missing_event_id",
            "message": "notification(action='dismiss_event') requires event_id=<id>.",
        }
    return dismiss_channel(
        agent,
        args.get("channel", "system"),
        invoked_by="notification",
        force=bool(args.get("force", False)),
        reason=args.get("reason"),
        event_id=event_id,
    )


def _dismiss_ref(agent, args: dict) -> dict:
    """Remove ``system`` event(s) by ``ref_id``."""
    ref_id = args.get("ref_id")
    if not ref_id:
        agent._log("notification_dismiss_missing_ref_id")
        return {
            "status": "error",
            "reason": "missing_ref_id",
            "message": "notification(action='dismiss_ref') requires ref_id=<id>.",
        }
    return dismiss_channel(
        agent,
        args.get("channel", "system"),
        invoked_by="notification",
        force=bool(args.get("force", False)),
        reason=args.get("reason"),
        ref_id=ref_id,
    )


def handle(agent, args: dict) -> dict:
    """Handle the standalone ``notification`` tool."""
    action = args.get("action")
    handler = {
        "check": _check,
        "dismiss_channel": _dismiss_channel,
        "dismiss_event": _dismiss_event,
        "dismiss_ref": _dismiss_ref,
    }.get(action)
    if handler is None:
        return {
            "status": "error",
            "message": f"Unknown notification action: {action}",
        }
    return handler(agent, args)
