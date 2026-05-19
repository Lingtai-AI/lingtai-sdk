"""Messaging — mail arrival, notifications, and outbound messaging."""
from __future__ import annotations

import time

from ..message import _make_message, MSG_REQUEST


def _on_mail_received(agent, payload: dict) -> None:
    """Callback for MailService — route incoming mail to inbox.

    This method is never replaced — it is the stable entry point for all
    incoming mail. Lifecycle control (interrupt, sleep, lull, cpr, nirvana)
    is handled by the system intrinsic via signal files, not mail.
    """
    _on_normal_mail(agent, payload)


def _on_normal_mail(agent, payload: dict) -> None:
    """Handle a normal mail — republish the unread digest to ``.notification/email.json``.

    The message is already persisted to mailbox/inbox/ by MailService.
    Mail arrival triggers a fresh write of ``.notification/email.json``;
    the kernel's notification sync mechanism (see
    base_agent/__init__.py:_sync_notifications) detects the fingerprint
    change on the next heartbeat tick and updates the wire's
    notification block accordingly.

    Reads, dismisses, archives, and deletes also trigger this rerender
    through ``EmailManager._rerender_unread_digest`` after they mutate
    read/inbox state, so ``email.json`` remains a mirror of current
    unread mail rather than a stale arrival snapshot.

    The ``_wake_nap`` call is preserved for sub-second latency: it
    nudges the heartbeat loop so notification sync runs within ~1 tick
    instead of waiting for the next periodic poll.  No ``MSG_TC_WAKE``
    here — the sync mechanism owns wake transitions; this just shortens
    the latency on an already-awake agent.
    """
    address = payload.get("from", "unknown")
    subject = payload.get("subject") or "(no subject)"

    agent._wake_nap("mail_arrived")
    agent._log("mail_received", address=address, subject=subject,
               message=payload.get("message", ""))

    _rerender_unread_digest(agent)


def _rerender_unread_digest(agent) -> str | None:
    """Publish (or clear) ``.notification/email.json`` per current unread state.

    Computes the unread set via ``_render_unread_digest``.  When count
    is positive, submits the digest via ``system.publish_notification``;
    when count drops to 0, clears the file so the kernel's sync strips
    the wire's notification block.

    Returns ``"email"`` when published, ``None`` when cleared.  The
    caller doesn't typically use the return value — the side-effect on
    ``.notification/`` is the contract.
    """
    from ..intrinsics.system import publish_notification, clear_notification
    from ..intrinsics.email.primitives import _render_unread_digest

    body, count, newest_ts = _render_unread_digest(agent)

    if count == 0:
        clear_notification(agent._working_dir, "email")
        agent._log("email_notification_cleared")
        return None

    publish_notification(
        agent._working_dir, "email",
        header=f"{count} unread email{'s' if count != 1 else ''}",
        icon="📧",
        instructions=(
            "Each entry above shows its mail ID directly under the "
            "subject — that's the value you pass to email_id when you "
            "call read or dismiss. Each entry also shows a preview of "
            "up to 200 characters. If a preview ends with "
            "'... (N more chars)' the message is truncated and you "
            "must call email(action=\"read\", email_id=[id1, id2, ...]) "
            "to see the full body. If the preview is short and shows "
            "the full content, you can skip the read fetch — but you "
            "still need to clear the notification: call "
            "email(action=\"dismiss\", email_id=[id1, id2, ...]) with "
            "the IDs you have handled. Both 'read' and 'dismiss' accept "
            "a list, so process multiple mails in one call. Until you "
            "read or dismiss a mail, this notification will keep "
            "reminding you about it. "
            "Note: this digest is a live mirror of current unread mail, "
            "not a fixed arrival log. IDs can become stale if you "
            "already read, dismissed, or archived a message through "
            "another path (e.g. email.check → email.read). If read or "
            "dismiss returns 'not_found', the mail was likely already "
            "handled — call email(action=\"check\", unread_only=true) "
            "to see what is still pending."
        ),
        data={
            "count": count,
            "newest_received_at": newest_ts,
            "digest": body,
        },
    )

    agent._log(
        "email_notification_published",
        count=count,
        newest_received_at=newest_ts,
    )
    return "email"


def _enqueue_system_notification(agent, *, source: str, ref_id: str, body: str) -> str:
    """Append a system event to ``.notification/system.json``.

    The system intrinsic owns this single file and multiplexes its
    event types inside (mail bounces, daemon notices, MCP-bridged
    events, future kernel events).  Each call merges a new event into
    the existing list, capped at the 20 most recent entries so a noisy
    producer can't blow the agent's context window.

    The merge is read-modify-write on the same file, so concurrent
    arrivals (e.g. a burst of bounces) need a per-agent lock to avoid
    losing writes.  The lock is created lazily on first use; only
    ``system.json`` needs it because ``email.json`` and ``soul.json``
    recompute full state on every publish (no merge).

    Args:
        agent: The agent instance.
        source: "email", "email.bounce", "daemon", "mcp.<name>", etc.
        ref_id: External reference (mail_id for email arrival, etc.).
        body: The localized prose for the agent to read.

    Returns:
        An identifier for the event (for logging and back-compat with
        callers that expected a notif_id; not actually used for any
        per-id lifecycle under the new model).
    """
    import secrets
    import threading
    from datetime import datetime, timezone
    from ..notifications import collect_notifications
    from ..intrinsics.system import publish_notification

    event_id = f"evt_{int(time.time()*1000):x}_{secrets.token_hex(2)}"
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Lazy per-agent lock for the read-modify-write merge.  Stored as
    # a plain attribute since BaseAgent doesn't declare it (only
    # `system.json` needs this; other producers don't merge).
    lock = getattr(agent, "_system_notification_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._system_notification_lock = lock

    with lock:
        current = collect_notifications(agent._working_dir).get("system", {})
        events = list(current.get("data", {}).get("events", []))

        events.append({
            "event_id": event_id,
            "source": source,
            "ref_id": ref_id,
            "body": body,
            "at": received_at,
        })
        # Cap at the 20 most recent.
        events = events[-20:]

        publish_notification(
            agent._working_dir, "system",
            header=(
                f"{len(events)} system notification"
                f"{'s' if len(events) != 1 else ''}"
            ),
            icon="🔔",
            data={"events": events},
        )

    agent._log(
        "system_notification_published",
        event_id=event_id,
        source=source,
        ref_id=ref_id,
    )
    # Sub-second sync latency: nudge the heartbeat.  Wake transitions
    # are owned by the kernel notification sync mechanism.
    try:
        agent._wake_nap("system_notification_published")
    except Exception as e:
        agent._log(
            "system_notification_wake_error",
            source=source,
            ref_id=ref_id,
            error=str(e)[:200],
        )

    return event_id


def _notify(agent, sender: str, text: str) -> None:
    """Put a system notification into the agent's inbox.

    This is the primary way addons inform the agent about external events.
    The message appears in the agent's conversation as a system message.
    """
    msg = _make_message(MSG_REQUEST, sender, text)
    agent.inbox.put(msg)


def _mail(agent, address: str, message: str, subject: str = "") -> dict:
    """Send a message to another agent (public API). Requires MailService.

    Routes through the email intrinsic (renamed from mail in 0.7.5).
    """
    return agent._intrinsics["email"]({"action": "send", "address": address, "message": message, "subject": subject})


def _send(agent, content: str | dict, sender: str = "user") -> None:
    """Send a message to the agent (fire-and-forget).

    Args:
        agent: The agent instance.
        content: Message content.
        sender: Message sender.
    """
    msg = _make_message(MSG_REQUEST, sender, content)
    agent.inbox.put(msg)
    agent._wake_nap("message_received")
