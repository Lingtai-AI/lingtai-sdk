"""Sent message tracker — deduplication and poll backoff for external channels.

Tracks recently sent messages to external channels (Telegram, IMAP, WeChat,
Feishu, WhatsApp) so the turn engine can:
1. Warn on duplicate sends within a short window.
2. Apply exponential backoff on polling/check actions when no new messages found.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class _SentEntry:
    content_hash: str
    recipient: str
    channel: str
    timestamp: float


# Tool names whose "send" action should trigger idle-after-send.
SEND_TOOLS: frozenset[str] = frozenset({
    "telegram", "imap", "wechat", "feishu", "whatsapp",
})

# Actions that count as "sending a message to a human".
SEND_ACTIONS: frozenset[str] = frozenset({
    "send", "reply", "reply_all",
})

# Actions that count as "polling for responses".
CHECK_ACTIONS: frozenset[str] = frozenset({
    "check", "read",
})


def _content_hash(content: str, recipient: str) -> str:
    """Deterministic hash of message content + recipient."""
    raw = f"{recipient}:{content}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


class SentMessageTracker:
    """Track recently sent messages for dedup and poll backoff.

    Thread-safe: all public methods are safe to call from any thread
    (the tracker is only used from the single-threaded tool-call loop).
    """

    def __init__(
        self,
        *,
        max_entries: int = 10,
        dedup_window_seconds: float = 30.0,
        ttl_seconds: float = 300.0,
    ) -> None:
        self._entries: list[_SentEntry] = []
        self._max_entries = max_entries
        self._dedup_window = dedup_window_seconds
        self._ttl = ttl_seconds

        # Polling backoff state (per-channel).
        self._poll_counts: dict[str, int] = {}
        self._max_poll_retries: int = 3

    def _cleanup(self) -> None:
        """Remove entries older than TTL."""
        cutoff = time.monotonic() - self._ttl
        self._entries = [e for e in self._entries if e.timestamp > cutoff]

    def was_recently_sent(
        self, content: str, recipient: str, *, window_seconds: float | None = None
    ) -> bool:
        """Check if a similar message was recently sent to the same recipient."""
        self._cleanup()
        h = _content_hash(content, recipient)
        cutoff = time.monotonic() - (window_seconds or self._dedup_window)
        return any(
            e.content_hash == h
            and e.recipient == recipient
            and e.timestamp > cutoff
            for e in self._entries
        )

    def record_sent(self, content: str, recipient: str, channel: str) -> None:
        """Record a successfully sent message."""
        self._cleanup()
        self._entries.append(_SentEntry(
            content_hash=_content_hash(content, recipient),
            recipient=recipient,
            channel=channel,
            timestamp=time.monotonic(),
        ))
        # Cap entries.
        if len(self._entries) > self._max_entries:
            self._entries = self._entries[-self._max_entries:]
        # Reset poll backoff for this channel — the agent just sent,
        # so a future check is legitimate.
        self._poll_counts.pop(channel, None)

    def record_poll(self, channel: str, found_new: bool) -> None:
        """Record a poll/check action result.

        Args:
            channel: Tool name (e.g. "telegram").
            found_new: True if the check found new messages.
        """
        if found_new:
            self._poll_counts.pop(channel, None)
        else:
            self._poll_counts[channel] = self._poll_counts.get(channel, 0) + 1

    def should_stop_polling(self, channel: str) -> bool:
        """True if the agent has polled too many times without results."""
        return self._poll_counts.get(channel, 0) >= self._max_poll_retries

    def poll_backoff_seconds(self, channel: str) -> float:
        """Exponential backoff delay for the next poll: 2, 4, 8 seconds."""
        count = self._poll_counts.get(channel, 0)
        if count <= 0:
            return 0.0
        return min(2.0 ** count, 8.0)

    def reset_poll(self, channel: str) -> None:
        """Reset poll backoff for a channel."""
        self._poll_counts.pop(channel, None)
