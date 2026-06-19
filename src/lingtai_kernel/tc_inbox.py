"""Involuntary tool-call inbox — queue of synthetic (call, result) pairs.

The agent's wire chat normally only contains tool calls the agent itself made.
Some events fire mechanically — soul flow on a cadence, scheduled wakeups,
periodic system pings — and the cleanest way to surface them in the agent's
history is as synthetic ``(ToolCallBlock, ToolResultBlock)`` pairs that look
like real tool calls the agent didn't initiate.

Producers (background timer threads) build fully-formed pairs and enqueue
them here. The agent's main run loop drains the queue at safe wire-chat
boundaries — when the chat tail has no unanswered tool_calls and no other
turn is mid-flight — and splices each pair into the wire chat.

Coalescing: producers can mark items ``coalesce=True`` and supply a ``source``
key. On enqueue, any existing item with the same source is replaced. Used by
soul flow so multiple firings during a busy stretch collapse to one
reflection (the latest voice wins) rather than spamming the agent with stale
back-to-back pairs when it next reaches a safe boundary.

Thread safety: the queue is a list guarded by a single Lock. Producers run
on background timer threads; the drain runs on the main agent thread. The
lock protects enqueue / drain / coalesce-replace; the drain copies the list
under the lock then splices outside it so chat-interface mutations don't
hold the lock.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm.interface import ToolCallBlock, ToolResultBlock


@dataclass
class DrainResult:
    """Result of a TCInbox.drain_into() call."""
    drained: bool  # True if drain happened or queue was empty; False if blocked
    count: int = 0
    sources: list = None  # list[str] — default_factory below

    def __post_init__(self):
        if self.sources is None:
            self.sources = []


@dataclass
class InvoluntaryToolCall:
    """One synthetic tool-call pair queued for splicing into the wire chat."""

    call: "ToolCallBlock"
    result: "ToolResultBlock"
    source: str               # e.g. "soul.flow", "system.wakeup"
    enqueued_at: float        # time.time() at enqueue
    coalesce: bool = False    # if True, replace prior item with same source
    # If True, the drain side also enforces a single-slot invariant in the
    # wire chat itself: any prior pair of the same source already spliced
    # into ChatInterface.entries is removed before this item is appended.
    # Used by soul flow to keep at most one consultation pair in history.
    replace_in_history: bool = False


class TCInbox:
    """Thread-safe queue of involuntary tool-call pairs."""

    def __init__(self) -> None:
        self._items: list[InvoluntaryToolCall] = []
        self._lock = threading.Lock()

    def enqueue(self, item: InvoluntaryToolCall) -> None:
        """Add an item.

        If ``item.coalesce`` is True, replace any existing item with the same
        ``source`` key (in place, preserving order). Otherwise append.
        """
        with self._lock:
            if item.coalesce:
                for i, existing in enumerate(self._items):
                    if existing.source == item.source:
                        self._items[i] = item
                        return
            self._items.append(item)

    def drain(self) -> list[InvoluntaryToolCall]:
        """Atomically remove and return all queued items in FIFO order."""
        with self._lock:
            items = self._items
            self._items = []
        return items

    def drain_into(
        self,
        interface,
        appendix_tracker: dict[str, str],
    ) -> DrainResult:
        """Splice queued items into the wire chat at a safe boundary.

        Returns a DrainResult indicating whether a drain happened and
        what was drained. If the interface still has pending tool-calls,
        returns DrainResult(drained=False) — caller should retry at the
        next safe boundary.

        For each drained item:
          - If item.replace_in_history is True, look up appendix_tracker
            for a prior call_id under item.source and remove that pair
            from interface.entries first.
          - Append the (call, result) pair to interface.
          - If item.replace_in_history is True, record item.call.id in
            appendix_tracker under item.source.

        Caller is responsible for save_chat_history after this returns.
        """
        if interface.has_pending_tool_calls():
            return DrainResult(drained=False)

        items = self.drain()
        if not items:
            return DrainResult(drained=True, count=0, sources=[])

        for item in items:
            if getattr(item, "replace_in_history", False):
                prior_id = appendix_tracker.get(item.source)
                if prior_id is not None:
                    interface.remove_pair_by_call_id(prior_id)
                appendix_tracker.pop(item.source, None)
            interface.add_assistant_message(content=[item.call])
            interface.add_tool_results([item.result])
            if getattr(item, "replace_in_history", False):
                appendix_tracker[item.source] = item.call.id

        return DrainResult(
            drained=True,
            count=len(items),
            sources=[i.source for i in items],
        )

    def remove_by_notif_id(self, notif_id: str) -> bool:
        """Remove a queued notification item by its ``call.args.notif_id``.

        Used by the dismiss handler to cover the race where the agent
        dismisses a notification before the kernel has spliced it into the
        wire chat (still queued here). Idempotent — returns False if no
        match.

        Only matches items whose call has ``args.get("action") ==
        "notification"`` to avoid false matches against unrelated synthetic
        pairs (e.g. soul flow).
        """
        with self._lock:
            for i, item in enumerate(self._items):
                args = getattr(item.call, "args", None) or {}
                if args.get("action") != "notification":
                    continue
                if args.get("notif_id") != notif_id:
                    continue
                del self._items[i]
                return True
        return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
