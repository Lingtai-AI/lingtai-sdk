"""Context molt — the core shed-and-reload machinery.

Contains:
    _context_molt    — agent-initiated molt
    _name_set        — set true name (immutable)
    _name_nickname   — set/change nickname (mutable)
    context_forget   — system-initiated forced molt
"""
from __future__ import annotations

import uuid
from pathlib import Path

from ...llm.interface import ToolCallBlock, ToolResultBlock


# Channel name for the post-molt reminder notification. Distinct from the
# pressure-warning ``molt`` channel owned by base_agent.turn._check_molt_pressure
# so a pressure-clear under threshold cannot sweep the reminder.
_POST_MOLT_CHANNEL = "post-molt"


def _first_nonempty_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _publish_post_molt(
    agent,
    *,
    initiator: str,
    source: str,
    molt_count: int,
    summary: str,
    reasoning: str | None,
    summary_path: Path | None,
    before_tokens: int,
    after_tokens: int,
) -> None:
    """Drop a `.notification/post-molt.json` reminder for the fresh agent.

    Best-effort — a publish failure must not block the molt return path.
    """
    try:
        from ..system import publish_notification

        reminder = (reasoning or "").strip() or _first_nonempty_line(summary)
        if initiator == "agent":
            header = f"post-molt #{molt_count} — resume work"
        else:
            header = f"post-molt #{molt_count} ({source}) — resume work"

        summary_rel = (
            str(summary_path.relative_to(agent._working_dir))
            if summary_path is not None
            else None
        )

        data = {
            "initiator": initiator,
            "source": source,
            "molt_count": molt_count,
            "reminder": reminder,
            "summary_path": summary_rel,
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
        }
        if reasoning:
            data["reasoning"] = reasoning

        instructions = (
            "You just completed a molt. Read system/pad.md, the latest "
            "summary under system/summaries/, and the most recent human-channel "
            "messages to reconstruct context, then continue the prior task. "
            "Once you have re-engaged, dismiss this reminder with "
            "system(action='dismiss', channel='post-molt')."
        )

        publish_notification(
            agent._working_dir,
            _POST_MOLT_CHANNEL,
            header=header,
            icon="🌱",
            priority="high",
            instructions=instructions,
            data=data,
        )
    except Exception as e:
        try:
            agent._log("post_molt_notification_failed", error=str(e))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Agent-initiated molt
# ---------------------------------------------------------------------------


def _context_molt(agent, args: dict) -> dict:
    """Agent molt: replay the molt's own tool_call as the opening assistant
    entry of the fresh session, return a "faint memory" result.

    The agent's summary lives in ``args.summary`` of its own ToolCallBlock.
    After the wipe we replay that ToolCallBlock into the fresh interface,
    so on the next turn the agent reads its own briefing exactly as it
    reads any past tool_use it has made. The dict returned by this function
    becomes the matching ToolResultBlock's content (paired by the standard
    return path: ToolExecutor.make_tool_result → session.send → adapter
    appends user-role tool_result to the fresh interface). The result is
    deliberately spare — counts and archive pointer, the faint shape of
    "you just woke up; the dream is gone but the briefing you wrote stands."

    ``_tc_id`` is injected by ``base_agent._dispatch_tool`` and carries the
    wire tool_use_id of the molt call. We use it to locate the original
    ToolCallBlock in the pre-molt interface so the replayed assistant entry
    keeps the agent's verbatim args (summary, keep_tool_calls, reasoning).

    Optional ``keep_tool_calls`` is a list of LingTai-issued tool-call ids
    (the ``_tool_call_id`` field stamped into every tool-result content by
    LLMService.make_tool_result). Each named pair survives the wipe and is
    replayed BEFORE the molt's own assistant entry, so chronologically the
    fresh interface reads: kept pairs (older) → molt call (just made) →
    faint-memory result (returned by this fn). Validation runs BEFORE any
    mutation: if any id is unknown the molt is refused and the molt count
    is not incremented.
    """
    summary = args.get("summary")
    if summary is None:
        return {"error": "summary is required — write a briefing to your future self."}
    if not summary.strip():
        return {"error": "summary cannot be empty — write what you need to remember."}

    if agent._chat is None:
        return {"error": "No active chat session to molt."}

    tc_id = args.get("_tc_id")
    if not tc_id:
        # Should never happen for an agent-initiated molt — base_agent always
        # injects _tc_id. Refuse without consuming a molt.
        return {
            "error": (
                "Internal: missing _tc_id for molt. The molt could not be "
                "replayed as a real tool pair into the fresh session. "
                "Molt refused; molt count unchanged."
            ),
        }

    keep_tool_calls = args.get("keep_tool_calls") or []
    if keep_tool_calls and not isinstance(keep_tool_calls, list):
        return {"error": "keep_tool_calls must be a list of LingTai tool-call ids (strings)."}

    iface_pre = agent._chat.interface

    # Locate the molt's own ToolCallBlock in the pre-molt interface so we
    # can replay it verbatim into the fresh session. Walk in reverse — the
    # molt was just emitted, it's in the tail assistant entry.
    molt_call_block = None
    for entry in reversed(iface_pre.entries):
        if entry.role != "assistant":
            continue
        for block in entry.content:
            if isinstance(block, ToolCallBlock) and block.id == tc_id:
                molt_call_block = block
                break
        if molt_call_block is not None:
            break
    if molt_call_block is None:
        return {
            "error": (
                "Internal: could not find the molt's own tool_call in the "
                "live interface. Molt refused; molt count unchanged."
            ),
        }

    # Validate keep-list BEFORE any state mutation so a typo doesn't
    # consume a molt. Walk the live interface, harvest LingTai-issued ids
    # from tool_result content, and confirm every requested id is present.
    keep_pairs: list[tuple] = []  # list of (call_block, result_block) in agent-listed order
    if keep_tool_calls:
        requested = set(keep_tool_calls)
        provider_id_for_lingtai: dict[str, str] = {}
        result_for_provider_id: dict[str, object] = {}
        for entry in iface_pre.entries:
            for block in entry.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                content = block.content
                if not isinstance(content, dict):
                    continue
                lt_id = content.get("_tool_call_id")
                if lt_id in requested:
                    provider_id_for_lingtai[lt_id] = block.id
                    result_for_provider_id[block.id] = block
        unmatched = [tid for tid in keep_tool_calls if tid not in provider_id_for_lingtai]
        if unmatched:
            return {
                "error": (
                    "Some keep_tool_calls ids were not found in the current "
                    "chat history. Molt refused; molt count unchanged. "
                    "Retry with a corrected list."
                ),
                "unmatched_ids": unmatched,
                "matched_count": len(provider_id_for_lingtai),
            }
        call_for_provider_id: dict[str, object] = {}
        for entry in iface_pre.entries:
            for block in entry.content:
                if isinstance(block, ToolCallBlock) and block.id in result_for_provider_id:
                    call_for_provider_id[block.id] = block
        missing_calls = [
            lt_id for lt_id in keep_tool_calls
            if call_for_provider_id.get(provider_id_for_lingtai[lt_id]) is None
        ]
        if missing_calls:
            return {
                "error": (
                    "Some keep_tool_calls ids have a tool_result in history "
                    "but no matching tool_call (the call block was likely "
                    "stripped). Molt refused; molt count unchanged."
                ),
                "missing_call_ids": missing_calls,
            }
        for lt_id in keep_tool_calls:
            pid = provider_id_for_lingtai[lt_id]
            keep_pairs.append((call_for_provider_id[pid], result_for_provider_id[pid]))

    # Parse keep_last — number of trailing entries to preserve across the molt.
    # Default is 20: every molt automatically keeps the last 20 conversation
    # entries unless the agent explicitly passes 0 or a different value.
    _KEEP_LAST_DEFAULT = 20
    keep_last_raw = args.get("keep_last")
    keep_last: int | None = None
    if keep_last_raw is not None:
        try:
            keep_last = int(keep_last_raw)
        except (TypeError, ValueError):
            return {"error": "keep_last must be an integer."}
        if keep_last < 0:
            return {"error": "keep_last must be non-negative."}
        if keep_last == 0:
            keep_last = None  # 0 explicitly disables keep_last
    else:
        keep_last = _KEEP_LAST_DEFAULT

    before_tokens = iface_pre.estimate_context_tokens()

    # Capture keep_last entries from the pre-molt interface BEFORE the
    # snapshot (which mutates iface_pre by closing orphan tool calls) and
    # BEFORE the wipe. These are the last N non-system entries that will
    # be replayed into the fresh session so the post-molt self retains
    # recent conversational context.
    # Exclude the molt call's own entry — it is replayed separately.
    keep_last_entries: list = []
    if keep_last is not None:
        non_system = [
            e for e in iface_pre.entries
            if e.role != "system"
            and not any(isinstance(b, ToolCallBlock) and b.id == tc_id for b in e.content)
        ]
        keep_last_entries = non_system[-keep_last:] if keep_last <= len(non_system) else non_system[:]

    # Deduplicate: when both keep_last and keep_tool_calls are used, remove
    # any keep_last entries whose ToolCallBlocks or ToolResultBlocks are
    # already captured in keep_pairs, so the same tool call doesn't appear
    # twice in the post-molt context.
    if keep_last_entries and keep_pairs:
        kept_wire_ids = set()
        for call_block, result_block in keep_pairs:
            kept_wire_ids.add(call_block.id)
            kept_wire_ids.add(result_block.id)

        def _entry_overlaps_keep_pairs(entry) -> bool:
            for block in entry.content:
                if isinstance(block, ToolCallBlock) and block.id in kept_wire_ids:
                    return True
                if isinstance(block, ToolResultBlock) and block.id in kept_wire_ids:
                    return True
            return False

        keep_last_entries = [
            e for e in keep_last_entries if not _entry_overlaps_keep_pairs(e)
        ]


    # Snapshot the pre-molt interface to a discrete file so future
    # past-self consultation can load it as cached substrate. Best-effort.
    # Orphan tool_calls (including the molt's own) are closed with
    # synthetic failure results inside _write_molt_snapshot.
    from . import _write_molt_snapshot
    _write_molt_snapshot(
        agent, iface_pre,
        before_tokens=before_tokens,
        summary=summary,
        source="agent",
        molt_count=agent._molt_count + 1,
    )

    # Wipe context
    agent._session._chat = None
    agent._session._interaction_id = None

    # Track molt count and persist to manifest
    agent._molt_count += 1
    agent._workdir.write_manifest(agent._build_manifest())

    # Archive the pre-molt chat history.
    history_dir = agent._working_dir / "history"
    history_dir.mkdir(exist_ok=True)
    current_path = history_dir / "chat_history.jsonl"
    archive_path = history_dir / "chat_history_archive.jsonl"
    try:
        if current_path.is_file():
            with open(archive_path, "a") as archive:
                archive.write(current_path.read_text(encoding="utf-8"))
            current_path.unlink()
    except OSError:
        pass

    # Drop appendix tracking — the wire chat is rebuilt from scratch
    # below, so any prior soul.flow pair indexed by call_id is gone.
    # Next consultation fire will append a fresh pair without trying to
    # remove a stale one.
    if hasattr(agent, "_appendix_ids_by_source"):
        agent._appendix_ids_by_source.clear()
    # Pre-molt tc_inbox items don't survive the wire rebuild — drain so
    # they don't leak into the post-molt wire.
    if hasattr(agent, "_tc_inbox"):
        agent._tc_inbox.drain()

    # Notification files (.notification/) survive molt — they are system
    # state, not conversation memory.  Only reset in-memory tracking so
    # the next sync re-reads from disk cleanly.
    if hasattr(agent, "_notification_fp"):
        agent._notification_fp = ()
    if hasattr(agent, "_notification_block_id"):
        agent._notification_block_id = None
    if hasattr(agent, "_notification_live_holder"):
        agent._notification_live_holder = None
    if hasattr(agent, "_pending_notification_meta"):
        agent._pending_notification_meta = None
    if hasattr(agent, "_pending_notification_fp"):
        agent._pending_notification_fp = None

    # Post-molt hooks — reload character/pad into prompt manager BEFORE new session
    for cb in getattr(agent, "_post_molt_hooks", []):
        try:
            cb()
        except Exception:
            pass

    # Now create fresh session with updated prompt manager
    agent._session.ensure_session()

    iface = agent._session._chat.interface

    # Replay keep_last entries first (oldest context).
    for entry in keep_last_entries:
        if entry.role == "assistant":
            iface.add_assistant_message(content=entry.content)
        elif entry.role == "user":
            # User entries may contain ToolResultBlocks (tool results are
            # user-role). Use add_tool_results for those, add_user_blocks
            # for everything else.
            tool_results = [b for b in entry.content if isinstance(b, ToolResultBlock)]
            if tool_results and all(isinstance(b, ToolResultBlock) for b in entry.content):
                iface.add_tool_results(tool_results)
            else:
                iface.add_user_blocks(entry.content)

    # Replay kept tool-call pairs next (older than the molt itself).
    for call_block, result_block in keep_pairs:
        iface.add_assistant_message(content=[call_block])
        iface.add_tool_results([result_block])

    # Replay the molt's own tool_call as the LAST assistant entry. The
    # matching tool_result will be appended by the standard return path.
    iface.add_assistant_message(content=[molt_call_block])

    after_tokens = iface.estimate_context_tokens()

    agent._log(
        "psyche_molt",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        molt_count=agent._molt_count,
        kept_tool_calls=len(keep_pairs),
        kept_last=len(keep_last_entries),
    )

    # Persist the agent's retrospective to system/summaries/. Best-effort —
    # a failed write surfaces as summary_path=None but does not block the molt.
    from . import _write_molt_summary
    summary_path = _write_molt_summary(
        agent,
        summary=summary,
        source="agent",
        molt_count=agent._molt_count,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )

    # Post-molt reminder. ToolExecutor strips visible ``reasoning`` and
    # injects ``_reasoning``; accept the plain key too so direct callers
    # (tests, in-process invocations) behave the same.
    reasoning = args.get("_reasoning") or args.get("reasoning")
    _publish_post_molt(
        agent,
        initiator="agent",
        source="agent",
        molt_count=agent._molt_count,
        summary=summary,
        reasoning=reasoning,
        summary_path=summary_path,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )

    # The faint-memory result.
    from ...i18n import t
    lang = agent._config.language
    return {
        "status": "ok",
        "note": t(lang, "psyche.molt_result_note"),
        "molt_count": agent._molt_count,
        "tokens_before": before_tokens,
        "tokens_after": after_tokens,
        "tokens_shed": max(0, before_tokens - after_tokens),
        "kept_tool_calls": len(keep_pairs),
        "kept_last": len(keep_last_entries),
        "archive_path": str(archive_path.relative_to(agent._working_dir))
            if archive_path.exists() else None,
        "summary_path": str(summary_path.relative_to(agent._working_dir))
            if summary_path is not None else None,
    }


# ---------------------------------------------------------------------------
# Name actions
# ---------------------------------------------------------------------------


def _name_set(agent, args: dict) -> dict:
    """Set the agent's true name."""
    name = args.get("content", "").strip()
    if not name:
        return {"error": "Name cannot be empty. Provide your chosen name in 'content'."}
    try:
        agent.set_name(name)
    except RuntimeError as e:
        return {"error": str(e)}
    return {"status": "ok", "name": name}


def _name_nickname(agent, args: dict) -> dict:
    """Set or change the agent's nickname (别名). Mutable."""
    nickname = args.get("content", "").strip()
    agent.set_nickname(nickname)
    return {"status": "ok", "nickname": nickname or None}


# ---------------------------------------------------------------------------
# System-initiated molt
# ---------------------------------------------------------------------------


def context_forget(agent, *, source: str = "warning_ladder", attempts: int = 0,
                    keep_last: int | None = None) -> dict:
    """Forced molt with a system-authored summary.

    Called by base_agent from three paths:
      - source="warning_ladder" (default): post-molt-warning exhaustion
      - source="aed": after max AED retries, before declaring ASLEEP
      - source=<name>: a .forget signal file dropped externally (karma-gated)

    Same archive-and-rebuild machinery as agent-called molt, but the molt
    pair is synthesized end-to-end here: we mint a wire id, build a
    ToolCallBlock whose args carry the system-authored summary, and append
    BOTH the call entry and its matching result entry into the fresh
    interface directly (there is no executor following us). On the next
    turn the agent reads this synthesized pair the same way it reads any
    of its own past tool calls — surface honesty about the molt being
    system-initiated lives in the args (``_initiator: "system"``) and the
    result note.

    Optional ``keep_last`` preserves the last N non-system entries from
    the pre-molt interface into the fresh session, giving the post-molt
    self recent conversational context without relying on pad.md.
    """
    from ...i18n import t

    lang = agent._config.language
    if source == "warning_ladder":
        summary = t(lang, "psyche.context_forget_summary")
    elif source == "aed":
        summary = t(lang, "psyche.context_forget_summary_aed").replace("{attempts}", str(attempts))
    else:
        summary = t(lang, "psyche.context_forget_summary_signal").replace("{source}", source)

    if agent._chat is None:
        return {"error": "No active chat session to molt."}

    synth_id = f"toolu_synth_{uuid.uuid4().hex[:16]}"
    tool_name = "psyche"
    synth_call = ToolCallBlock(
        id=synth_id,
        name=tool_name,
        args={
            "object": "context",
            "action": "molt",
            "summary": summary,
            "_initiator": "system",
            "_source": source,
        },
    )

    iface_pre = agent._chat.interface
    before_tokens = iface_pre.estimate_context_tokens()

    # Capture keep_last entries from the pre-molt interface BEFORE wiping.
    keep_last_entries: list = []
    if keep_last is not None and keep_last > 0:
        non_system = [e for e in iface_pre.entries if e.role != "system"]
        keep_last_entries = non_system[-keep_last:] if keep_last <= len(non_system) else non_system[:]

    from . import _write_molt_snapshot
    _write_molt_snapshot(
        agent, iface_pre,
        before_tokens=before_tokens,
        summary=summary,
        source=source,
        molt_count=agent._molt_count + 1,
    )

    # Wipe context
    agent._session._chat = None
    agent._session._interaction_id = None

    agent._molt_count += 1
    agent._workdir.write_manifest(agent._build_manifest())

    history_dir = agent._working_dir / "history"
    history_dir.mkdir(exist_ok=True)
    current_path = history_dir / "chat_history.jsonl"
    archive_path = history_dir / "chat_history_archive.jsonl"
    try:
        if current_path.is_file():
            with open(archive_path, "a") as archive:
                archive.write(current_path.read_text(encoding="utf-8"))
            current_path.unlink()
    except OSError:
        pass

    if hasattr(agent, "_appendix_ids_by_source"):
        agent._appendix_ids_by_source.clear()
    # Pre-molt tc_inbox items don't survive the wire rebuild — drain so
    # they don't leak into the post-molt wire.
    if hasattr(agent, "_tc_inbox"):
        agent._tc_inbox.drain()

    # Notification files (.notification/) survive molt — they are system
    # state, not conversation memory.  Only reset in-memory tracking so
    # the next sync re-reads from disk cleanly.
    if hasattr(agent, "_notification_fp"):
        agent._notification_fp = ()
    if hasattr(agent, "_notification_block_id"):
        agent._notification_block_id = None
    if hasattr(agent, "_notification_live_holder"):
        agent._notification_live_holder = None
    if hasattr(agent, "_pending_notification_meta"):
        agent._pending_notification_meta = None
    if hasattr(agent, "_pending_notification_fp"):
        agent._pending_notification_fp = None

    for cb in getattr(agent, "_post_molt_hooks", []):
        try:
            cb()
        except Exception:
            pass

    agent._session.ensure_session()
    iface = agent._session._chat.interface

    # Replay keep_last entries first (oldest context).
    for entry in keep_last_entries:
        if entry.role == "assistant":
            iface.add_assistant_message(content=entry.content)
        elif entry.role == "user":
            tool_results = [b for b in entry.content if isinstance(b, ToolResultBlock)]
            if tool_results and all(isinstance(b, ToolResultBlock) for b in entry.content):
                iface.add_tool_results(tool_results)
            else:
                iface.add_user_blocks(entry.content)

    iface.add_assistant_message(content=[synth_call])

    after_tokens = iface.estimate_context_tokens()

    # Persist the system-authored summary to system/summaries/. Best-effort —
    # source field captures origin (warning_ladder / aed / signal name) so
    # readers can filter out non-agent-authored entries.
    from . import _write_molt_summary
    summary_path = _write_molt_summary(
        agent,
        summary=summary,
        source=source,
        molt_count=agent._molt_count,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )

    # Post-molt reminder — the system-authored summary itself is the
    # reminder string; reasoning is absent because the agent did not author
    # this molt.
    _publish_post_molt(
        agent,
        initiator="system",
        source=source,
        molt_count=agent._molt_count,
        summary=summary,
        reasoning=None,
        summary_path=summary_path,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )

    result_dict = {
        "status": "ok",
        "note": t(lang, "psyche.molt_result_note"),
        "molt_count": agent._molt_count,
        "tokens_before": before_tokens,
        "tokens_after": after_tokens,
        "tokens_shed": max(0, before_tokens - after_tokens),
        "kept_tool_calls": 0,
        "kept_last": len(keep_last_entries),
        "archive_path": str(archive_path.relative_to(agent._working_dir))
            if archive_path.exists() else None,
        "summary_path": str(summary_path.relative_to(agent._working_dir))
            if summary_path is not None else None,
        "_initiator": "system",
        "_source": source,
    }
    iface.add_tool_results([
        ToolResultBlock(id=synth_id, name=tool_name, content=result_dict)
    ])

    agent._log(
        "psyche_molt",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        molt_count=agent._molt_count,
        kept_tool_calls=0,
        kept_last=len(keep_last_entries),
        initiator="system",
        source=source,
    )

    return result_dict
