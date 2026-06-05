"""LICC v1 client — the producer half of the LingTai Inbox Callback Contract.

``inbox.py`` is the kernel-side *consumer*: it polls ``.mcp_inbox/<mcp>/``,
validates each event, dispatches it to the agent, and deletes the file. This
module is the *producer*: the single function an out-of-process MCP server
calls to drop one event into that inbox.

It is deliberately tiny and dependency-free so an MCP subprocess can::

    from lingtai.core.mcp.licc import push_inbox_event
    push_inbox_event("alice", "new DM", "hey, are you around?")

without importing the agent runtime, the poller, or any provider SDK.
Importing this module starts no threads and touches no filesystem.

Where it writes
---------------
``<agent_dir>/.mcp_inbox/<mcp_name>/<event_id>.json``

``agent_dir`` and ``mcp_name`` default to the two environment variables the
kernel injects into every MCP it spawns (see ``lingtai.agent``)::

    LINGTAI_AGENT_DIR  — absolute path of the agent's working directory
    LINGTAI_MCP_NAME   — this MCP's registry name

Callers (tests, advanced integrations) may pass ``agent_dir`` / ``mcp_name``
explicitly to override the environment.

How it writes
-------------
Atomically, matching the contract the poller relies on: serialize to
``<event_id>.json.tmp``, ``fsync`` the file, then ``os.replace`` it onto the
final ``<event_id>.json``. The poller ignores ``*.json.tmp`` files, so a
half-written event is never observed. The payload conforms to the LICC v1
schema validated by :func:`lingtai.core.mcp.inbox.validate_event`.

Failure policy
--------------
Best-effort and silent: any missing configuration (no agent dir / no mcp
name) or filesystem error returns ``False`` and writes nothing — it never
raises into the calling MCP. Failure logs are intentionally terse and never
include the event ``body``/``subject``/``metadata``, which may carry user
content or secrets.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-export the contract constants from the kernel-side module so producer and
# consumer share one source of truth — they must never drift apart.
from .inbox import EVENT_SUFFIX, INBOX_DIRNAME, LICC_VERSION, TMP_SUFFIX, validate_event

__all__ = [
    "push_inbox_event",
    "LICC_VERSION",
    "INBOX_DIRNAME",
    "TMP_SUFFIX",
    "EVENT_SUFFIX",
]

log = logging.getLogger(__name__)

_ENV_AGENT_DIR = "LINGTAI_AGENT_DIR"
_ENV_MCP_NAME = "LINGTAI_MCP_NAME"
_MCP_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def push_inbox_event(
    sender: str,
    subject: str,
    body: str,
    *,
    metadata: dict[str, Any] | None = None,
    wake: bool = True,
    received_at: str | None = None,
    agent_dir: str | os.PathLike[str] | None = None,
    mcp_name: str | None = None,
    event_id: str | None = None,
) -> bool:
    """Atomically push one LICC v1 event into the agent's MCP inbox.

    Args:
        sender: Human-readable sender (required, non-empty). Written to the
            ``"from"`` JSON key.
        subject: One-line summary (required, non-empty, <= 200 chars to pass
            the kernel validator).
        body: Full message body (required string; may be empty).
        metadata: Optional dict of extra fields. The kernel surfaces the
            scalar keys ``conversation_ref`` / ``message_ref`` / ``platform``
            in the notification preview. Defaults to ``{}``.
        wake: Whether delivery should wake a napping agent. Default ``True``.
        received_at: ISO 8601 timestamp. Defaults to the current UTC time.
        agent_dir: Agent working directory. Defaults to ``$LINGTAI_AGENT_DIR``.
        mcp_name: This MCP's registry name. Defaults to ``$LINGTAI_MCP_NAME``.
        event_id: Filename stem for the event. Defaults to a fresh UUID4,
            which also guarantees uniqueness across repeated pushes. Explicit
            values must be a single safe filename segment.

    Returns:
        ``True`` if the event file was written and atomically renamed into
        place; ``False`` on missing configuration, invalid LICC payload fields,
        unsafe path components, or write/serialization failure. Never raises.

    ``mcp_name`` and ``event_id`` are path components. Explicit/env values
    that do not match the kernel MCP name convention or safe event-id
    filename convention are rejected with ``False`` rather than being used
    in a path.
    """
    resolved_dir = agent_dir if agent_dir is not None else os.environ.get(_ENV_AGENT_DIR)
    resolved_mcp = mcp_name if mcp_name is not None else os.environ.get(_ENV_MCP_NAME)

    # No agent dir or no MCP name → we don't know where to write. No-op.
    if not resolved_dir or not resolved_mcp:
        return False
    if not isinstance(resolved_mcp, str) or not _MCP_NAME_RE.fullmatch(resolved_mcp):
        return False

    stem = event_id if event_id is not None else uuid.uuid4().hex
    if not isinstance(stem, str) or not _EVENT_ID_RE.fullmatch(stem):
        return False

    event = {
        "licc_version": LICC_VERSION,
        "from": sender,
        "subject": subject,
        "body": body,
        "metadata": metadata if metadata is not None else {},
        "wake": wake,
        "received_at": received_at if received_at is not None else _now_iso(),
    }

    valid, _err = validate_event(event)
    if not valid:
        return False

    tmp_path: Path | None = None

    try:
        mcp_dir = Path(resolved_dir) / INBOX_DIRNAME / resolved_mcp
        tmp_path = mcp_dir / f"{stem}{TMP_SUFFIX}"
        final_path = mcp_dir / f"{stem}{EVENT_SUFFIX}"
        mcp_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(event, ensure_ascii=False)
        # Write + flush + fsync so the bytes are durable before the rename
        # makes the event visible to the poller.
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        # Atomic publish: the poller only ever sees a complete file.
        os.replace(tmp_path, final_path)
        return True
    except (OSError, TypeError, ValueError) as e:
        # Terse, content-free log: never echo body/subject/metadata, which may
        # carry user content or secrets. Errno + class is enough to triage.
        log.warning(
            "licc: failed to push event for mcp %r: %s", resolved_mcp, type(e).__name__
        )
        # Best-effort cleanup of a stray .tmp so it can't linger. The poller
        # ignores .tmp files anyway, but leaving litter is untidy.
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False
