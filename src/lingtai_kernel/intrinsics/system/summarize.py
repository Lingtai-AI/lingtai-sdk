"""system(action='summarize') — agent-authored context summarization.

Replaces the context-visible content of prior main-agent tool-result blocks
with a compact agent-authored summary, while preserving the original payload
in the durable event log (events.jsonl) for later retrieval by tool_call_id.

This is purely a context-budget operation: the agent says "I have digested
this result; replace the active version with my summary, keep the full
original traceable."  It does NOT delete or rewrite event traces.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
from typing import Any


# Stable marker stamped on every summarized replacement block so future
# passes (and idempotency checks) can detect them without heuristics.
SUMMARIZE_MARKER = "lingtai_agent_summarized_result"


def _is_already_summarized(content: Any) -> bool:
    """Return True iff *content* is a summarize replacement produced here."""
    return isinstance(content, dict) and content.get("artifact") == SUMMARIZE_MARKER


def _find_tool_result_block(agent, tool_call_id: str):
    """Walk live chat history and return the ToolResultBlock for *tool_call_id*.

    Returns ``(entry, block_index, block)`` or ``(None, -1, None)`` when not found.
    Excludes blocks already carrying a synthesized heal placeholder.
    """
    from ...llm.interface import ToolResultBlock  # local import — no circular dep

    chat = getattr(agent, "_chat", None)
    if chat is None:
        return None, -1, None
    iface = getattr(chat, "interface", None)
    if iface is None:
        return None, -1, None
    entries = getattr(iface, "_entries", [])
    for entry in entries:
        if entry.role != "user":
            continue
        for idx, block in enumerate(entry.content):
            if isinstance(block, ToolResultBlock) and block.id == tool_call_id:
                return entry, idx, block
    return None, -1, None


def _visible_len(content: Any) -> int:
    """Return the context-visible character length of a tool-result content."""
    if isinstance(content, str):
        return len(content)
    try:
        return len(_json.dumps(content, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(content))


def _summarize(agent, args: dict) -> dict:
    """Handle system(action='summarize').

    Expected args shape::

        {
          "action": "summarize",
          "items": [
            {"tool_call_id": "toolu_...", "summary": "Agent-authored text ..."},
            ...
          ],
          "notification_threshold_chars": 20000  # optional — adjust threshold
        }

    ``notification_threshold_chars`` is optional.  When present it must be a
    non-negative integer; 0 disables notifications entirely.  It may be combined
    with ``items`` or used alone (with an empty / absent items list) just to
    adjust the threshold.

    Returns a dict with per-item results (``"items"`` list), aggregate counts
    (``"summarized"``, ``"failed"``), and the current threshold after any
    adjustment (``"notification_threshold_chars"``).
    """
    # --- Adjust notification threshold if requested ---
    notification_threshold_chars = args.get("notification_threshold_chars")
    threshold_error: str | None = None
    if notification_threshold_chars is not None:
        if not isinstance(notification_threshold_chars, int) or isinstance(notification_threshold_chars, bool):
            threshold_error = (
                f"notification_threshold_chars must be a non-negative integer, "
                f"got {notification_threshold_chars!r}."
            )
        elif notification_threshold_chars < 0:
            threshold_error = (
                f"notification_threshold_chars must be >= 0 (0 = disabled), "
                f"got {notification_threshold_chars!r}."
            )
        else:
            agent._summarize_notification_threshold = notification_threshold_chars

    current_threshold = getattr(agent, "_summarize_notification_threshold", 5000)

    if threshold_error:
        return {
            "status": "error",
            "reason": "invalid_notification_threshold",
            "message": threshold_error,
            "notification_threshold_chars": current_threshold,
        }

    items_arg = args.get("items")

    # Threshold-only call (no items or empty items) is valid.
    if notification_threshold_chars is not None and (not items_arg):
        return {
            "status": "ok",
            "summarized": 0,
            "failed": 0,
            "items": [],
            "notification_threshold_chars": current_threshold,
            "message": (
                f"Notification threshold updated to {current_threshold} chars. "
                f"No items to summarize."
            ),
        }

    if not isinstance(items_arg, list) or len(items_arg) == 0:
        return {
            "status": "error",
            "reason": "missing_items",
            "message": (
                "system(action='summarize') requires a non-empty 'items' list, "
                "each with 'tool_call_id' and 'summary'."
            ),
            "notification_threshold_chars": current_threshold,
        }

    item_results: list[dict] = []
    summarized_count = 0
    failed_count = 0

    now_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    chat = getattr(agent, "_chat", None)

    for item in items_arg:
        if not isinstance(item, dict):
            item_results.append({
                "status": "error",
                "reason": "invalid_item",
                "message": "Each item must be a dict with 'tool_call_id' and 'summary'.",
                "item": repr(item)[:200],
            })
            failed_count += 1
            continue

        tool_call_id = item.get("tool_call_id")
        summary = item.get("summary")

        if not tool_call_id or not isinstance(tool_call_id, str):
            item_results.append({
                "status": "error",
                "reason": "missing_tool_call_id",
                "message": "Item is missing 'tool_call_id'.",
            })
            failed_count += 1
            continue

        if summary is None or not isinstance(summary, str):
            item_results.append({
                "status": "error",
                "reason": "missing_summary",
                "tool_call_id": tool_call_id,
                "message": "Item is missing 'summary' (must be a string).",
            })
            failed_count += 1
            continue

        if chat is None:
            item_results.append({
                "status": "error",
                "reason": "no_chat_session",
                "tool_call_id": tool_call_id,
                "message": "No active chat session — cannot mutate history.",
            })
            failed_count += 1
            continue

        entry, idx, block = _find_tool_result_block(agent, tool_call_id)

        if block is None:
            item_results.append({
                "status": "error",
                "reason": "not_found",
                "tool_call_id": tool_call_id,
                "message": (
                    f"No main-agent tool-result block found for tool_call_id={tool_call_id!r}. "
                    "Daemon results, unknown ids, and ids from previous sessions "
                    "cannot be summarized."
                ),
            })
            failed_count += 1
            continue

        if _is_already_summarized(block.content):
            item_results.append({
                "status": "error",
                "reason": "already_summarized",
                "tool_call_id": tool_call_id,
                "message": (
                    f"tool_call_id={tool_call_id!r} has already been summarized. "
                    "Re-summarization is blocked for now to preserve idempotency; "
                    "keep the existing summary or retrieve the original from logs/events."
                ),
            })
            failed_count += 1
            continue

        # Capture original visible length before replacing.
        original_visible_len = _visible_len(block.content)

        # Build the replacement — visible in context, not a secret.
        replacement: dict[str, Any] = {
            "artifact": SUMMARIZE_MARKER,
            "tool_call_id": tool_call_id,
            "tool_name": block.name,
            "agent_summary": summary,
            "summarized_at": now_utc,
            "summary_chars": len(summary),
            "original_visible_chars": original_visible_len,
            "retrieval_hint": (
                f"This is your own agent-authored summary of the original tool result. "
                f"The summary is NOT canonical — it reflects your understanding at the "
                f"time of summarization and may be incomplete or inaccurate. "
                f"To retrieve the full original, grep events.jsonl by tool_call_id:\n"
                f"  grep '{tool_call_id}' <workdir>/logs/events.jsonl\n"
                f"  # or use: lingtai-agent log query (see sqlite-log-query manual)"
            ),
        }

        # Mutate the block content in place — pairing, id, name, synthesized
        # flag are untouched so provider wire alternation stays valid.
        entry.content[idx].content = replacement

        agent._log(
            "tool_result_summarized",
            tool_call_id=tool_call_id,
            tool_name=block.name,
            summary_chars=len(summary),
            original_visible_chars=original_visible_len,
        )

        item_results.append({
            "status": "ok",
            "tool_call_id": tool_call_id,
            "tool_name": block.name,
            "summary_chars": len(summary),
            "original_visible_chars": original_visible_len,
        })
        summarized_count += 1

    # Persist history so summarization survives refresh/molt.
    if summarized_count > 0:
        save_fn = getattr(agent, "_save_chat_history", None)
        if callable(save_fn):
            try:
                save_fn(ledger_source="summarize")
            except Exception as exc:
                # Non-fatal: summarization already applied in memory.
                agent._log(
                    "tool_result_summarize_save_failed",
                    error=str(exc),
                )

    overall_status = "ok" if failed_count == 0 else ("partial" if summarized_count > 0 else "error")
    return {
        "status": overall_status,
        "summarized": summarized_count,
        "failed": failed_count,
        "items": item_results,
        "notification_threshold_chars": current_threshold,
    }
