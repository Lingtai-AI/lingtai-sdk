"""Mailbox primitives — filesystem I/O, ID generation, read tracking, display.

Moved from the former monolithic email.py.  Kept as module-level functions so
other code can still import them by name (via the package __init__.py
re-exports).
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ...i18n import t


# ---------------------------------------------------------------------------
# Mailbox directory helpers
# ---------------------------------------------------------------------------

def _new_mailbox_id() -> str:
    """Build a sortable, human-scannable mailbox id.

    Format: ``<YYYYMMDDTHHMMSS>-<4 hex>`` — 20 chars total.
    """
    import uuid
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:4]}"


def mode_field(lang: str = "en") -> dict:
    """Schema field for the address-mode parameter."""
    return {
        "type": "string",
        "enum": ["peer", "abs"],
        "description": t(lang, "email.mode"),
    }


def _mailbox_dir(agent) -> Path:
    return agent._working_dir / "mailbox"


def _inbox_dir(agent) -> Path:
    return _mailbox_dir(agent) / "inbox"


def _outbox_dir(agent) -> Path:
    return _mailbox_dir(agent) / "outbox"


def _sent_dir(agent) -> Path:
    return _mailbox_dir(agent) / "sent"


# ---------------------------------------------------------------------------
# Inbox I/O
# ---------------------------------------------------------------------------

def _load_message(agent, msg_id: str) -> dict | None:
    """Load a single inbox message by ID, or None if not found."""
    msg_file = _inbox_dir(agent) / msg_id / "message.json"
    if not msg_file.is_file():
        return None
    try:
        return json.loads(msg_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _list_inbox(agent) -> list[dict]:
    """List all inbox messages, sorted newest first (by received_at)."""
    inbox = _inbox_dir(agent)
    if not inbox.is_dir():
        return []
    messages = []
    for msg_dir in inbox.iterdir():
        if not msg_dir.is_dir():
            continue
        msg_file = msg_dir / "message.json"
        if not msg_file.is_file():
            continue
        try:
            msg = json.loads(msg_file.read_text())
            messages.append(msg)
        except (json.JSONDecodeError, OSError):
            continue
    messages.sort(key=lambda m: m.get("received_at", ""), reverse=True)
    return messages


# ---------------------------------------------------------------------------
# Read tracking
# ---------------------------------------------------------------------------

def _read_ids_path(agent) -> Path:
    return _mailbox_dir(agent) / "read.json"


def _read_ids(agent) -> set[str]:
    """Load set of read message IDs from read.json."""
    path = _read_ids_path(agent)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text())
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def _save_read_ids(agent, ids: set[str]) -> None:
    """Atomically write read IDs to read.json."""
    path = _read_ids_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids)))
    os.replace(str(tmp), str(path))


def _mark_read(agent, msg_id: str) -> None:
    """Mark a message as read."""
    ids = _read_ids(agent)
    ids.add(msg_id)
    _save_read_ids(agent, ids)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _summary_to_list(raw) -> list[str]:
    """Best-effort coercion of to/cc for display."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw if isinstance(x, str)]


def _message_summary(msg: dict, read_ids: set[str], truncate: int = 500,
                     *, recipient_agent_id: str = "") -> dict:
    """Build a summary dict for check output.

    When *recipient_agent_id* is provided and the sender has a different
    ``agent_id`` but the same ``agent_name`` as the recipient, the sender
    display is disambiguated with the sender's agent_id so the recipient
    can tell them apart.
    """
    msg_id = msg.get("_mailbox_id", "")
    body = msg.get("message", "")
    if truncate > 0 and len(body) > truncate:
        preview = body[:truncate] + f"... ({len(body) - truncate} more chars)"
    else:
        preview = body
    identity = msg.get("identity")
    sender = msg.get("from", "")
    if identity and identity.get("agent_name"):
        name = identity["agent_name"]
        sender_id = identity.get("agent_id", "")
        # Disambiguate when sender is a different agent — always show
        # agent_id when it differs from ours, regardless of name match.
        # This handles abs-mode emails where from is a full path.
        if (recipient_agent_id and sender_id
                and sender_id != recipient_agent_id):
            name = f"{name} (agent:{sender_id})"
        sender = f"{name} ({sender})"
    return {
        "id": msg_id,
        "from": sender,
        "to": _summary_to_list(msg.get("to")),
        "subject": msg.get("subject", ""),
        "preview": preview,
        "time": msg.get("received_at", ""),
        "unread": msg_id not in read_ids,
    }


# ---------------------------------------------------------------------------
# Delivery helpers
# ---------------------------------------------------------------------------

def _is_self_send(agent, address: str) -> bool:
    """Check if the address matches this agent."""
    if address == agent._working_dir.name:
        return True
    if address == str(agent._working_dir):
        return True
    if agent._mail_service is not None and agent._mail_service.address:
        if address == agent._mail_service.address:
            return True
    return False


def _persist_to_inbox(agent, payload: dict) -> str:
    """Persist a message directly to mailbox/inbox/{uuid}/message.json."""
    msg_id = _new_mailbox_id()
    msg_dir = _inbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["_mailbox_id"] = msg_id
    payload["received_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (msg_dir / "message.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    return msg_id


def _persist_to_outbox(agent, payload: dict, deliver_at: datetime) -> str:
    """Write a message to outbox/{uuid}/message.json."""
    msg_id = _new_mailbox_id()
    msg_dir = _outbox_dir(agent) / msg_id
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload.pop("_mode", None)
    payload.pop("_dispatch_to", None)
    payload["_mailbox_id"] = msg_id
    payload["deliver_at"] = deliver_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    (msg_dir / "message.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    return msg_id


def _move_to_sent(agent, msg_id: str, sent_at: str, status: str) -> None:
    """Move outbox/{uuid}/ → sent/{uuid}/, enriching with sent_at and status."""
    src = _outbox_dir(agent) / msg_id
    dst = _sent_dir(agent) / msg_id
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        return
    msg_file = src / "message.json"
    if msg_file.is_file():
        try:
            data = json.loads(msg_file.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        data["sent_at"] = sent_at
        data["status"] = status
        msg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    shutil.move(str(src), str(dst))


def _mailman(agent, msg_id: str, payload: dict, deliver_at: datetime,
             *, skip_sent: bool = False) -> None:
    """Daemon thread — one per message. Waits, dispatches, archives to sent."""
    import time as _time

    wait = (deliver_at - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        _time.sleep(wait)

    address = payload.get("_dispatch_to") or payload.get("to", "")
    if isinstance(address, list):
        address = address[0] if address else ""

    mode = payload.pop("_mode", "peer")

    err = None
    try:
        if _is_self_send(agent, address):
            _persist_to_inbox(agent, payload)
            agent._wake_nap("mail_arrived")
            status = "delivered"
        elif agent._mail_service is not None:
            err = agent._mail_service.send(address, payload, mode=mode)
            status = "delivered" if err is None else "refused"
        else:
            err = "No mail service configured"
            status = "refused"
    except Exception as exc:
        err = str(exc)
        status = "refused"

    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not skip_sent:
        _move_to_sent(agent, msg_id, sent_at, status)
    else:
        outbox_entry = _outbox_dir(agent) / msg_id
        if outbox_entry.is_dir():
            shutil.rmtree(outbox_entry)

    agent._log("mail_sent", address=address, subject=payload.get("subject", ""),
               status=status, message=payload.get("message", ""))

    # Bounce notification
    if status == "refused" and err:
        notification = t(
            agent._config.language, "system.mail_bounce",
            error=err, address=address,
            subject=payload.get("subject", "(no subject)"),
        )
        agent._enqueue_system_notification(
            source="email.bounce",
            ref_id=msg_id,
            body=notification,
        )


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------

def _coerce_address_list(raw) -> list[str]:
    """Normalize an address arg into a clean list[str]."""
    if raw is None:
        return []
    if isinstance(raw, str):
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed if x]
            except (json.JSONDecodeError, ValueError):
                pass
        return [raw] if raw else []
    return [str(x) for x in raw if x]


def _preview(body: str, limit: int = 500) -> str:
    if limit <= 0:
        return body
    if len(body) > limit:
        return body[:limit] + f"... ({len(body) - limit} more chars)"
    return body


def _email_time(e: dict) -> str:
    """Extract the best timestamp from an email dict for filtering."""
    return e.get("received_at") or e.get("sent_at") or e.get("time") or ""


# ---------------------------------------------------------------------------
# Unread digest rendering
# ---------------------------------------------------------------------------

def _render_unread_digest(agent, *, max_entries: int = 10, preview_chars: int = 200) -> tuple[str, int, str | None]:
    """Compute and render the current unread mail digest.

    Returns ``(body, count, newest_received_at)``:
      - ``body`` is the rendered prose for the ToolResultBlock.
      - ``count`` is total unread count (may exceed ``max_entries``).
      - ``newest_received_at`` is the ISO timestamp of the most recent
        unread message, or None if count == 0.

    Caller uses ``count`` to short-circuit (don't enqueue when 0) and
    ``newest_received_at`` for the call_block args.
    """
    from ...i18n import t as _t
    from ...time_veil import veil

    read_ids = _read_ids(agent)
    inbox = _list_inbox(agent)  # already newest-first per existing semantics
    unread = [m for m in inbox if m.get("_mailbox_id") not in read_ids]
    count = len(unread)
    if count == 0:
        return ("", 0, None)

    shown = unread[:max_entries]
    newest = shown[0]
    newest_ts = newest.get("received_at") or newest.get("sent_at") or ""

    lang = agent._config.language
    recipient_id = getattr(agent, "_agent_id", "")
    lines = []
    for i, m in enumerate(shown, start=1):
        addr = m.get("from", "unknown")
        identity = m.get("identity") or {}
        name = identity.get("agent_name") or addr
        # Disambiguate when sender is a different agent
        sender_id = identity.get("agent_id", "")
        if (recipient_id and sender_id
                and sender_id != recipient_id):
            name = f"{name} (agent:{sender_id})"
        subj_raw = m.get("subject")
        subject = subj_raw if subj_raw else _t(lang, "email.unread_digest.no_subject")
        ts = m.get("sent_at") or m.get("time") or m.get("received_at") or ""
        sent_at = veil(agent, ts)
        body = m.get("message", "")
        if len(body) > preview_chars:
            preview = body[:preview_chars].replace("\n", " ") + f"... ({len(body) - preview_chars} more chars)"
        else:
            preview = body.replace("\n", " ")
        msg_id = m.get("_mailbox_id", "")
        lines.append(_t(
            lang, "email.unread_digest.entry",
            n=i, address=addr, name=name, subject=subject,
            sent_at=sent_at, preview=preview, id=msg_id,
        ))

    more_line = ""
    if count > max_entries:
        more_line = _t(lang, "email.unread_digest.more", shown=max_entries, total=count)

    body = _t(
        lang, "email.unread_digest",
        count=count,
        recency=veil(agent, newest_ts),
        entries="\n".join(lines),
        more=more_line,
        tool=getattr(agent, "_mailbox_tool", "email"),
    )
    return (body, count, newest_ts)
