"""Message types and Message dataclass for 灵台 agent inboxes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

MSG_REQUEST = "request"
MSG_USER_INPUT = "user_input"
# Sentinel — no payload; just unblocks inbox.get() so the run loop can pick
# up a queued involuntary tool-call pair from tc_inbox. Used for soul flow
# and system notifications (mail arrival, bounce, future MCP listeners).
MSG_TC_WAKE = "tc_wake"


@dataclass
class Message:
    """A message delivered to an agent's inbox.

    Attributes:
        id:        Unique message ID (auto-generated if not provided).
        type:      One of MSG_REQUEST, MSG_USER_INPUT, MSG_TC_WAKE.
        sender:    Agent ID, "user", etc.
        content:   Payload — str for requests, dict for structured data.
        reply_to:  Links back to original message.
        timestamp: ``time.monotonic()`` when created.
    """

    type: str
    sender: str
    content: Any
    id: str = field(default_factory=lambda: f"msg_{uuid4().hex[:12]}")
    reply_to: str | None = None
    timestamp: float = field(default_factory=time.monotonic)


def _make_message(
    type: str,
    sender: str,
    content: Any,
    *,
    reply_to: str | None = None,
) -> Message:
    return Message(
        id=f"msg_{uuid4().hex[:12]}",
        type=type,
        sender=sender,
        content=content,
        reply_to=reply_to,
    )
