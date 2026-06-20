"""Snapshot and summary persistence for the molt machinery.

Provides helpers to serialize the pre-molt ChatInterface and persist
agent-authored retrospectives. Both are best-effort — a failure here
must not block the molt itself.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


SNAPSHOT_SCHEMA_VERSION = 1


def _write_molt_summary(
    agent,
    *,
    summary: str,
    source: str,
    molt_count: int,
    before_tokens: int,
    after_tokens: int,
    session_journal_path: str | None = None,
) -> Path | None:
    """Persist the molt summary to system/summaries/ as a durable retrospective.

    Best-effort — a failed write must not block the molt. Returns the path
    on success, or None if the write failed.

    Filename: molt_<molt_count>_<unix_ts>.md — molt_count first so directory
    listings sort chronologically without parsing.

    Format: a small YAML-ish frontmatter block followed by the summary prose.
    Frontmatter is human-readable (so `cat` is useful) and machine-parseable
    (any future digest-injection layer can split on the leading `---`).

    Complementary to `history/snapshots/snapshot_<count>_<ts>.json`:
    - snapshot = frozen substrate (full ChatInterface for past-self consultation)
    - summary  = curated retrospective (agent-authored prose)
    Both share molt_count so they can be paired by index.
    """
    try:
        summaries_dir = agent._working_dir / "system" / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)

        unix_ts = int(time.time())
        path = summaries_dir / f"molt_{molt_count}_{unix_ts}.md"

        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        agent_name = getattr(agent, "agent_name", None) or ""

        journal_line = (
            f"session_journal_path: {session_journal_path}\n"
            if session_journal_path
            else ""
        )
        frontmatter = (
            "---\n"
            f"molt_count: {molt_count}\n"
            f"created_at: {created_at}\n"
            f"source: {source}\n"
            f"agent_name: {agent_name}\n"
            f"{journal_line}"
            f"before_tokens: {before_tokens}\n"
            f"after_tokens: {after_tokens}\n"
            f"tokens_shed: {max(0, before_tokens - after_tokens)}\n"
            "---\n\n"
        )

        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(frontmatter + summary, encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception as e:
        try:
            agent._log("summary_write_failed", error=str(e))
        except Exception:
            pass
        return None


def _close_orphan_tool_calls(iface) -> int:
    """Close unanswered tool_calls with synthetic failure results.

    Appends synthetic ``ToolResultBlock`` entries for any ``ToolCallBlock``
    that lacks a matching ``ToolResultBlock`` in the interface.  The
    synthetic result signals that the tool call was in-flight when the
    session ended (e.g. the agent molted or the system forced a reset).

    Operates on the **live** ``ChatInterface`` object (mutates in place)
    so callers should invoke this *before* ``to_dict()``.

    Returns the number of orphan calls closed.
    """
    from ...llm.interface import ToolCallBlock, ToolResultBlock

    if not iface.entries:
        return 0

    # Collect all tool_call ids that already have results.
    answered_ids: set[str] = set()
    all_calls: dict[str, ToolCallBlock] = {}  # id -> block

    for entry in iface.entries:
        for b in entry.content:
            if isinstance(b, ToolResultBlock):
                answered_ids.add(b.id)
            elif isinstance(b, ToolCallBlock):
                all_calls[b.id] = b

    # Find orphans — tool_calls without results.
    unanswered = [tc for tc in all_calls.values() if tc.id not in answered_ids]
    if not unanswered:
        return 0

    from ...llm.interface import _synthesized_abort_message

    refusal_blocks = [
        ToolResultBlock(
            id=tc.id,
            name=tc.name,
            content=_synthesized_abort_message(
                tc.name,
                "tool call was pending when the session ended (molt/reset)",
            ),
            synthesized=True,
        )
        for tc in unanswered
    ]

    iface.add_tool_results(refusal_blocks)
    return len(unanswered)


def _write_molt_snapshot(
    agent,
    iface_pre,
    *,
    before_tokens: int,
    summary: str,
    source: str,
    molt_count: int,
) -> Path | None:
    """Serialize the pre-molt ChatInterface to a discrete snapshot file.

    The snapshot is the substrate a future "past self" consultation can
    load — full message history at the moment the agent decided to molt.
    Unanswered tool_calls are closed with synthetic failure results so
    the snapshot is self-contained: every tool_call has a matching
    tool_result, and the LLM protocol is satisfied when the snapshot is
    later loaded as consultation substrate.

    Returns the snapshot path on success, or None if the write failed
    (best-effort — a failed snapshot must not block the molt itself).

    Filename: snapshot_<molt_count>_<unix_ts>.json — molt_count first
    so directory listings sort chronologically without parsing.

    ``agent``: may be None for standalone use; only affects metadata fields.
    """
    try:
        snapshots_dir = agent._working_dir / "history" / "snapshots" if agent else Path(".")
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Close orphan tool_calls before serializing.
        closed = _close_orphan_tool_calls(iface_pre)
        if closed > 0 and agent:
            agent._log("snapshot_orphan_tool_calls_closed", count=closed)

        entries = iface_pre.to_dict()

        unix_ts = int(time.time())
        path = snapshots_dir / f"snapshot_{molt_count}_{unix_ts}.json"

        payload = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "molt_count": molt_count,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "before_tokens": before_tokens,
            "agent_name": getattr(agent, "agent_name", None) if agent else None,
            "agent_id": getattr(agent, "_agent_id", None) if agent else None,
            "molt_summary": summary,
            "molt_source": source,
            "interface": entries,
        }

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return path
    except Exception as e:
        try:
            if agent:
                agent._log("snapshot_write_failed", error=str(e))
        except Exception:
            pass
        return None
