"""LICC v1 client compatibility wrapper.

The canonical first-party LICC producer implementation lives in
``lingtai.core.mcp.licc`` inside lingtai-kernel. This module keeps the
addon's historical ``.lingtai_wechat.licc.push_inbox_event`` import path stable
while preferring the kernel implementation whenever it is available.

A small local fallback remains for standalone development or pre-upgrade
runtime environments where the host kernel does not yet expose the canonical
client helper. The fallback writes the same LICC v1 filesystem event shape.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:  # pragma: no cover - availability depends on the host LingTai runtime.
    from lingtai.core.mcp.licc import push_inbox_event as _kernel_push_inbox_event
except ImportError:  # Older/standalone environments keep using the fallback below.
    _kernel_push_inbox_event = None

log = logging.getLogger(__name__)

LICC_VERSION = 1
INBOX_DIRNAME = ".mcp_inbox"
TMP_SUFFIX = ".json.tmp"
EVENT_SUFFIX = ".json"


def push_inbox_event(
    sender: str,
    subject: str,
    body: str,
    *,
    metadata: dict | None = None,
    wake: bool = True,
) -> bool:
    """Write a LICC event into the host agent's inbox.

    In a current LingTai runtime, delegate to the canonical kernel helper.
    If the helper is unavailable, use the local compatibility fallback so
    older hosts and standalone development remain functional.
    """
    if _kernel_push_inbox_event is not None:
        return _kernel_push_inbox_event(
            sender,
            subject,
            body,
            metadata=metadata,
            wake=wake,
        )
    return _fallback_push_inbox_event(
        sender,
        subject,
        body,
        metadata=metadata,
        wake=wake,
    )


def _fallback_push_inbox_event(
    sender: str,
    subject: str,
    body: str,
    *,
    metadata: dict | None = None,
    wake: bool = True,
) -> bool:
    """Local LICC v1 writer used only when the kernel helper is unavailable."""
    agent_dir = os.environ.get("LINGTAI_AGENT_DIR")
    mcp_name = os.environ.get("LINGTAI_MCP_NAME")

    if not agent_dir or not mcp_name:
        log.warning(
            "LICC: LINGTAI_AGENT_DIR and/or LINGTAI_MCP_NAME not set; "
            "event dropped"
        )
        return False

    event = {
        "licc_version": LICC_VERSION,
        "from": sender,
        "subject": subject,
        "body": body,
        "metadata": metadata or {},
        "wake": wake,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        target_dir = Path(agent_dir) / INBOX_DIRNAME / mcp_name
        target_dir.mkdir(parents=True, exist_ok=True)
        event_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        tmp = target_dir / f"{event_id}{TMP_SUFFIX}"
        final = target_dir / f"{event_id}{EVENT_SUFFIX}"
        # Write + fsync + atomic replace so the host poller never sees a
        # half-written file.
        text = json.dumps(event, ensure_ascii=False)
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.error(
            "LICC: failed to write event for MCP %r via compatibility fallback: %s",
            mcp_name,
            type(exc).__name__,
        )
        return False
