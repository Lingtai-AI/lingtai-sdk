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

from ...meta_block import formal_tool_result_visible_len


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
    """Return visible length of the formal tool-result payload only.

    Kernel/runtime metadata such as ``_meta.notifications`` and
    ``_meta.guidance`` is channel guidance/state, not the substantive result
    being summarized.
    """
    return formal_tool_result_visible_len(content)


def _summarize(agent, args: dict) -> dict:
    """Handle system(action='summarize').

    Expected args shape::

        {
          "action": "summarize",
          "items": [
            {"tool_call_id": "toolu_...", "summary": "Agent-authored text ..."},
            ...
          ]
        }

    Returns a dict with per-item results (``"items"`` list), aggregate counts
    (``"summarized"``, ``"failed"``), and the current threshold
    (``"notification_threshold_chars"``).

    Note: ``notification_threshold_chars`` is NOT accepted at runtime.  The
    threshold is set exclusively via ``manifest.summarize_notification_threshold``
    in init.json and takes effect after a refresh.  Passing this field returns
    an error so callers discover the policy change loudly.
    """
    current_threshold = getattr(agent, "_summarize_notification_threshold", 3000)

    # --- Reject runtime threshold mutation ---
    if args.get("notification_threshold_chars") is not None:
        return {
            "status": "error",
            "reason": "runtime_threshold_change_not_supported",
            "message": (
                "The summarize notification threshold cannot be changed at runtime. "
                "It is configured via manifest.summarize_notification_threshold in "
                "init.json and takes effect after system(action='refresh'). "
                "To handle pending large-result notifications without changing the "
                "threshold: summarize/digest all pending large-result cases in one "
                "deliberate batch using system(action='summarize', items=[...]), or "
                "tolerate the repeated reminders until you update the persistent "
                "config and refresh."
            ),
            "notification_threshold_chars": current_threshold,
        }

    items_arg = args.get("items")

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
    summarized_ids: list[str] = []

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
        summarized_ids.append(tool_call_id)

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
        hook = getattr(chat, "on_history_summarized", None)
        if callable(hook):
            try:
                hook(list(summarized_ids))
            except Exception as exc:  # pragma: no cover - defensive hook isolation
                agent._log(
                    "history_summarize_hook_failed",
                    error=type(exc).__name__,
                )

    # A successful summarize is the sanctioned discharge path for the
    # matching large-result reminder: clear it automatically.  Generic
    # dismiss refuses these reminders, so this is the only way they go away.
    cleared_reminder_ref_ids: list[str] = []
    if summarized_ids and getattr(agent, "_working_dir", None) is not None:
        try:
            from ...notifications import clear_large_result_reminders
            cleared_reminder_ref_ids = clear_large_result_reminders(
                agent, summarized_ids
            )
        except Exception as exc:
            # Non-fatal: summarization already applied; the rescan/dedup
            # logic will reconcile the reminder on a later turn.
            try:
                agent._log(
                    "large_result_reminder_clear_failed",
                    error=str(exc),
                )
            except Exception:
                pass

    overall_status = "ok" if failed_count == 0 else ("partial" if summarized_count > 0 else "error")
    result: dict[str, Any] = {
        "status": overall_status,
        "summarized": summarized_count,
        "failed": failed_count,
        "items": item_results,
        "cleared_reminders": cleared_reminder_ref_ids,
        "notification_threshold_chars": current_threshold,
    }

    # Reassure the agent that runtime-history bookkeeping happened while
    # provider-side context reconstruction is intentionally delayed.  The
    # chat-history block was updated and large-result reminders were cleared
    # immediately above; the active provider request may still ride the existing
    # append/continuation prefix with the old raw block until summarized history
    # is pending and context reaches the runtime reconstruction threshold (0.75
    # of the window).  This is a short, generic status, not a per-provider policy
    # object — runtimes that reconstruct on every request simply observe no
    # delay.
    if summarized_count > 0:
        result["reconstruction"] = (
            "Summary recorded in runtime history. The active provider context may still "
            "contain the old result until delayed reconstruction; when summarized history "
            "is pending, reconstruction happens automatically once context reaches 0.75 "
            "of the window. This is normal — keep working. See meta_guidance and "
            "substrate for details."
        )

    return result
