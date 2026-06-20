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
            "If you are about to start long work (e.g. a slow bash, daemon, "
            "web_search, or large file write) and a preview is truncated, first "
            "call email(action=\"read\", email_id=[...]) as a normal tool so "
            "you have the full body before acting. To acknowledge or answer the "
            "sender, call email reply directly as a normal tool. "
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


def _enqueue_system_notification(
    agent,
    *,
    source: str,
    ref_id: str,
    body: str,
    skip_if_ref_id_exists: bool = False,
) -> str:
    """Append a system event to ``.notification/system.json``.

    The system intrinsic owns this single file and multiplexes its
    event types inside (mail bounces, daemon notices, MCP-bridged
    events, future kernel events).  Each call merges a new event into
    the existing list, capped at the 20 most recent entries so a noisy
    producer can't blow the agent's context window.

    The merge is read-modify-write on the same file, so concurrent
    arrivals (e.g. a burst of bounces) need a per-agent lock to avoid
    losing writes.  The lock is initialized by ``BaseAgent``; only
    ``system.json`` needs it because ``email.json`` and ``soul.json``
    recompute full state on every publish (no merge).

    Args:
        agent: The agent instance.
        source: "email", "email.bounce", "daemon", "mcp.<name>", etc.
        ref_id: External reference (mail_id for email arrival, etc.).
        body: The localized prose for the agent to read.
        skip_if_ref_id_exists: When True, skip publishing if an event with
            the same ref_id already exists in system.json.  Used by the
            large-result rescan path to avoid duplicate notifications.
            Returns "" (empty string) when skipped.

    Returns:
        An identifier for the event (for logging and back-compat with
        callers that expected a notif_id; not actually used for any
        per-id lifecycle under the new model).  Returns "" when skipped
        due to skip_if_ref_id_exists.
    """
    import secrets
    from datetime import datetime, timezone
    from ..notifications import collect_notifications
    from ..intrinsics.system import publish_notification

    event_id = f"evt_{int(time.time()*1000):x}_{secrets.token_hex(2)}"
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lock = agent._system_notification_lock

    with lock:
        current = collect_notifications(agent._working_dir).get("system", {})
        events = list(current.get("data", {}).get("events", []))

        if skip_if_ref_id_exists:
            for ev in events:
                if ev.get("ref_id") == ref_id:
                    return ""

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


def _rescan_large_tool_results(agent) -> int:
    """Scan live chat history for large unsummarized tool results and notify.

    Called at every turn/round boundary so existing oversized
    ``ToolResultBlock``s in context — blocks that arrived before a refresh,
    after a notification was dismissed, or from a history migration — can
    trigger a ``large_tool_result`` system notification even though no new
    tool execution has happened.

    Skips:
      - threshold <= 0 (disabled)
      - ``daemon_tool_result`` tool names (child-daemon relays)
      - blocks already summarized (``artifact == SUMMARIZE_MARKER``)
      - synthesized blocks (``block.synthesized == True``)
      - spill manifests whose ``original_char_count`` is missing or <= threshold

    Dedup:
      - Uses ``skip_if_ref_id_exists=True`` in ``_enqueue_system_notification``
        so an already-present notification (same ``ref_id``) is never duplicated.
      - If the notification was previously dismissed (no longer in system.json),
        the rescan will re-emit it on the next turn — by design (requirement §3).

    Returns the count of notifications published (0 when nothing fired or
    threshold is disabled).
    """
    from ..llm.interface import ToolResultBlock
    from ..intrinsics.system.summarize import _is_already_summarized
    from ..tool_result_artifacts import is_spill_manifest

    threshold = getattr(agent, "_summarize_notification_threshold", 3000)
    if threshold <= 0:
        return 0

    chat = getattr(agent, "_chat", None)
    if chat is None:
        return 0
    iface = getattr(chat, "interface", None)
    if iface is None:
        return 0
    entries = getattr(iface, "_entries", [])

    published = 0
    for entry in entries:
        if entry.role != "user":
            continue
        for block in entry.content:
            if not isinstance(block, ToolResultBlock):
                continue

            # Skip synthesized heal / notification placeholder blocks.
            if getattr(block, "synthesized", False):
                continue

            # Exclude daemon child result relays.
            if block.name == "daemon_tool_result":
                continue

            # Skip already-summarized blocks.
            if _is_already_summarized(block.content):
                continue

            content = block.content
            tool_call_id = block.id or "<unknown>"

            # Determine effective length — mirrors _maybe_notify_large_tool_result.
            is_spill = is_spill_manifest(content)
            if is_spill:
                original_char_count = (
                    content.get("original_char_count")
                    if isinstance(content, dict)
                    else None
                )
                if not isinstance(original_char_count, int) or original_char_count <= threshold:
                    continue
                result_len = original_char_count
                spill_path = content.get("spill_path") if isinstance(content, dict) else None
                preview_text = None
            else:
                import json as _json
                if isinstance(content, str):
                    result_len = len(content)
                    preview_text = content[:200]
                else:
                    try:
                        serialized = _json.dumps(content, ensure_ascii=False, default=str)
                    except (TypeError, ValueError):
                        serialized = str(content)
                    result_len = len(serialized)
                    preview_text = serialized[:200]

                if result_len <= threshold:
                    continue
                spill_path = None

            # Build notification body — same templates as _maybe_notify_large_tool_result.
            _threshold_policy = (
                f"The threshold ({threshold} chars) is set via manifest.summarize_notification_threshold "
                f"in init.json and takes effect after system(action='refresh'). "
                f"It cannot be changed temporarily at runtime. "
                f"If you intentionally keep large results visible, you must either: "
                f"(a) summarize/digest all pending large-result cases in one deliberate batch, or "
                f"(b) tolerate these repeated reminders until you update the persistent config and refresh."
            )
            if is_spill and spill_path:
                body = (
                    f"[large tool result — spilled] tool_name={block.name!r} tool_call_id={tool_call_id}\n"
                    f"Original result length: {result_len} chars "
                    f"(current summarize notification threshold: {threshold} chars).\n"
                    f"The result was too large for the context window and was written to: {spill_path!r}\n"
                    f"Read the sidecar file to access the full content, then call:\n"
                    f"  system(action=\"summarize\", items=[{{\"tool_call_id\": \"{tool_call_id}\", \"summary\": \"<your summary>\"}}])\n"
                    f"to replace the context-visible spill manifest with your own summary.\n"
                    f"Treat this notification as a prompt to act, not just FYI: if the result still matters, "
                    f"digest it now and summarize all pending large-result cases in one deliberate batch "
                    f"before continuing deep work; otherwise the reminder will return until the result is summarized.\n"
                    f"{_threshold_policy}\n"
                    f"The full original remains in the sidecar file and in events.jsonl by tool_call_id."
                )
            elif is_spill:
                body = (
                    f"[large tool result — spilled] tool_name={block.name!r} tool_call_id={tool_call_id}\n"
                    f"Original result length: {result_len} chars "
                    f"(current summarize notification threshold: {threshold} chars).\n"
                    f"The result was spilled to a sidecar file (path not available). "
                    f"Check the spill manifest in your conversation history for the path.\n"
                    f"After reading the sidecar, call:\n"
                    f"  system(action=\"summarize\", items=[{{\"tool_call_id\": \"{tool_call_id}\", \"summary\": \"<your summary>\"}}])\n"
                    f"to replace the context-visible spill manifest with your own summary.\n"
                    f"Treat this notification as a prompt to act, not just FYI: if the result still matters, "
                    f"digest it now and summarize all pending large-result cases in one deliberate batch "
                    f"before continuing deep work; otherwise the reminder will return until the result is summarized.\n"
                    f"{_threshold_policy}"
                )
            else:
                body = (
                    f"[large tool result] tool_name={block.name!r} tool_call_id={tool_call_id}\n"
                    f"Result length: {result_len} chars "
                    f"(current summarize notification threshold: {threshold} chars).\n"
                    f"Preview (first 200 chars): {preview_text!r}\n\n"
                    f"After you have digested this result, call:\n"
                    f"  system(action=\"summarize\", items=[{{\"tool_call_id\": \"{tool_call_id}\", \"summary\": \"<your summary>\"}}])\n"
                    f"to replace the context-visible payload with your own summary.\n"
                    f"Treat this notification as a prompt to act, not just FYI: if the result still matters, "
                    f"digest it now and summarize all pending large-result cases in one deliberate batch "
                    f"before continuing deep work; otherwise the reminder will return until the result is summarized.\n"
                    f"{_threshold_policy}\n"
                    f"The full original remains retrievable from events.jsonl by tool_call_id."
                )

            try:
                enqueue_fn = getattr(agent, "_enqueue_system_notification", None)
                if enqueue_fn is None:
                    continue
                event_id = enqueue_fn(
                    source="large_tool_result",
                    ref_id=f"large_tool_result:{tool_call_id}",
                    body=body,
                    skip_if_ref_id_exists=True,
                )
                if event_id:
                    agent._log(
                        "large_tool_result_rescan_notification_published",
                        tool_name=block.name,
                        tool_call_id=tool_call_id,
                        result_len=result_len,
                        threshold=threshold,
                        is_spill=is_spill,
                    )
                    published += 1
            except Exception as exc:
                agent._log(
                    "large_tool_result_rescan_notification_failed",
                    tool_name=block.name,
                    tool_call_id=tool_call_id,
                    error=str(exc),
                )

    return published


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
