"""Context molt — the core shed-and-reload machinery.

Contains:
    _context_molt    — agent-initiated molt
    _name_set        — set true name (immutable)
    _name_nickname   — set/change nickname (mutable)
    context_forget   — system-initiated forced molt
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ...llm.interface import ToolCallBlock, ToolResultBlock


# Channel name for the post-molt reminder notification. Distinct from the
# pressure-warning ``molt`` channel owned by base_agent.turn._check_molt_pressure
# so a pressure-clear under threshold cannot sweep the reminder.
_POST_MOLT_CHANNEL = "post-molt"
_POST_CHILD_DELEGATION_CHANNEL = "post-child-delegation"

_MAX_AVATAR_LEDGER_LINES = 1000
_AVATAR_LEDGER_READ_CHUNK_BYTES = 64 * 1024
_MAX_AVATAR_ENTRIES = 20
_MAX_DAEMON_DIRS = 200
_MAX_DAEMON_ENTRIES = 20
_MISSION_PREVIEW_MAX = 200
_TASK_PREVIEW_MAX = 200
_LAST_OUTPUT_PREVIEW_MAX = 300
_AVATAR_HEARTBEAT_STALE_AFTER_S = 10.0
_DAEMON_HEARTBEAT_STALE_AFTER_S = 30.0
_DAEMON_TERMINAL_STATES = {"done", "failed", "cancelled", "timeout"}


_POST_CHILD_DELEGATION_INSTRUCTIONS = (
    "You just molted while delegated work may still exist. This is an "
    "awareness snapshot, not an instruction to run lifecycle actions. Do not "
    "CPR, interrupt, reclaim, suspend, or message any child automatically. "
    "First reorient from the post-molt reminder. Then inspect deliberately: "
    "for daemons, use daemon(action='list') or daemon(action='check', id='...'); "
    "for avatars, contact them through the normal mail/email route or inspect "
    "their heartbeat/manifest if needed. Dismiss this channel when you have "
    "recorded or acted on the delegation state: system(action='dismiss', "
    "channel='post-child-delegation', reason='aware: ...')."
)


def _first_nonempty_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _preview(value, limit: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _relative_path(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(path, base)
    except (OSError, ValueError):
        return str(path)


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _avatar_heartbeat_age(child_dir: Path, now: float) -> float | None:
    heartbeat = child_dir / ".agent.heartbeat"
    if not heartbeat.is_file():
        return None
    try:
        ts = float(heartbeat.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return max(0.0, now - ts)


def _daemon_heartbeat_age(run_dir: Path, now: float) -> float | None:
    heartbeat = run_dir / ".heartbeat"
    if not heartbeat.is_file():
        return None
    try:
        return max(0.0, now - heartbeat.stat().st_mtime)
    except OSError:
        return None


def _read_avatar_ledger_tail(ledger_path: Path) -> tuple[list[str], bool]:
    """Read newest avatar ledger lines without scanning the whole append log."""
    chunks: list[bytes] = []
    newline_count = 0
    try:
        with ledger_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0 and newline_count <= _MAX_AVATAR_LEDGER_LINES:
                size = min(_AVATAR_LEDGER_READ_CHUNK_BYTES, pos)
                pos -= size
                f.seek(pos)
                chunk = f.read(size)
                if not chunk:
                    break
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return [], False

    truncated = pos > 0
    raw_lines = b"".join(reversed(chunks)).splitlines()
    if len(raw_lines) > _MAX_AVATAR_LEDGER_LINES:
        truncated = True
        raw_lines = raw_lines[-_MAX_AVATAR_LEDGER_LINES:]

    lines: list[str] = []
    for raw in raw_lines:
        try:
            lines.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return lines, truncated


def _read_avatar_records(agent) -> tuple[list[dict], bool]:
    ledger_path = agent._working_dir / "delegates" / "ledger.jsonl"
    if not ledger_path.is_file():
        return [], False

    from ...handshake import resolve_address

    records_by_child: dict[str, dict] = {}
    lines, truncated = _read_avatar_ledger_tail(ledger_path)
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("event") != "avatar":
            continue
        working_dir = record.get("working_dir") or record.get("address")
        if not working_dir:
            continue

        child_dir = resolve_address(working_dir, agent._working_dir.parent)
        child_key = str(child_dir)
        if child_key in records_by_child:
            continue
        records_by_child[child_key] = {
            "record": record,
            "child_dir": child_dir,
        }

    return list(records_by_child.values()), truncated


def _collect_avatar_entries(agent, now: float) -> tuple[list[dict], dict, bool]:
    rows, scan_truncated = _read_avatar_records(agent)
    entries: list[dict] = []
    counts = {"alive": 0, "stale": 0, "missing": 0, "boot_failed": 0}

    for row in rows:
        record = row["record"]
        child_dir = row["child_dir"]
        boot_status = str(record.get("boot_status") or "")
        manifest = _read_json_object(child_dir / ".agent.json")
        heartbeat_age = _avatar_heartbeat_age(child_dir, now)

        if boot_status == "failed":
            state = "boot_failed"
        elif not child_dir.exists():
            state = "missing"
        elif heartbeat_age is not None and heartbeat_age <= _AVATAR_HEARTBEAT_STALE_AFTER_S:
            state = "alive"
        else:
            state = "stale"

        counts[state] += 1
        if len(entries) >= _MAX_AVATAR_ENTRIES:
            scan_truncated = True
            continue

        entry = {
            "name": record.get("name") or child_dir.name,
            "address": record.get("address") or record.get("working_dir") or child_dir.name,
            "relative_path": _relative_path(child_dir, agent._working_dir),
            "depth": 1,
            "type": record.get("type") or "unknown",
            "boot_status": boot_status or None,
            "spawned_at": record.get("ts"),
            "state": state,
            "manifest_state": (
                manifest.get("state") if isinstance(manifest, dict) else None
            ),
            "heartbeat_age_s": (
                round(heartbeat_age, 1) if heartbeat_age is not None else None
            ),
            "mission_preview": _preview(record.get("mission"), _MISSION_PREVIEW_MAX),
            "suggested_action": (
                "Contact via mail/email or inspect heartbeat/manifest; do not "
                "CPR automatically."
            ),
        }
        entries.append(entry)

    return entries, counts, scan_truncated


def _iter_daemon_state_files(agent) -> tuple[list[Path], bool]:
    daemons_dir = agent._working_dir / "daemons"
    if not daemons_dir.is_dir():
        return [], False
    try:
        run_dirs = [p for p in daemons_dir.iterdir() if p.is_dir()]
    except OSError:
        return [], False

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    run_dirs.sort(key=_mtime, reverse=True)
    truncated = len(run_dirs) > _MAX_DAEMON_DIRS
    return [p / "daemon.json" for p in run_dirs[:_MAX_DAEMON_DIRS]], truncated


def _collect_daemon_entries(agent, now: float) -> tuple[list[dict], dict, bool]:
    state_files, scan_truncated = _iter_daemon_state_files(agent)
    entries: list[dict] = []
    counts = {"running": 0, "stale_running": 0}

    for state_file in state_files:
        data = _read_json_object(state_file)
        if not data:
            continue
        daemon_state = str(data.get("state") or "").lower()
        if not daemon_state or daemon_state in _DAEMON_TERMINAL_STATES:
            continue

        run_dir = state_file.parent
        heartbeat_age = _daemon_heartbeat_age(run_dir, now)
        state = (
            "running"
            if heartbeat_age is not None and heartbeat_age <= _DAEMON_HEARTBEAT_STALE_AFTER_S
            else "stale_running"
        )
        counts[state] += 1
        if len(entries) >= _MAX_DAEMON_ENTRIES:
            scan_truncated = True
            continue

        handle = data.get("handle") or data.get("id") or run_dir.name
        entry = {
            "id": handle,
            "run_id": data.get("run_id") or run_dir.name,
            "relative_path": _relative_path(run_dir, agent._working_dir),
            "state": state,
            "daemon_state": daemon_state or None,
            "backend": data.get("backend"),
            "task_preview": _preview(data.get("task"), _TASK_PREVIEW_MAX),
            "elapsed_s": data.get("elapsed_s"),
            "turn": data.get("turn"),
            "current_tool": data.get("current_tool"),
            "last_output_preview": _preview(
                data.get("last_output"), _LAST_OUTPUT_PREVIEW_MAX
            ),
            "heartbeat_age_s": (
                round(heartbeat_age, 1) if heartbeat_age is not None else None
            ),
            "suggested_action": f"daemon(action='check', id='{handle}')",
        }
        entries.append(entry)

    return entries, counts, scan_truncated


def _publish_post_child_delegation_reminder(
    agent,
    *,
    initiator: str,
    source: str,
    molt_count: int,
) -> None:
    """Publish a best-effort post-molt reminder about delegated work.

    The snapshot intentionally only reads bounded parent-side state. It does
    not contact, wake, CPR, interrupt, reclaim, suspend, or message children.
    """
    try:
        from ..system import clear_notification, publish_notification

        now = time.time()
        avatars, avatar_counts, avatar_truncated = _collect_avatar_entries(agent, now)
        daemons, daemon_counts, daemon_truncated = _collect_daemon_entries(agent, now)

        if not avatars and not daemons:
            clear_notification(agent._working_dir, _POST_CHILD_DELEGATION_CHANNEL)
            return

        total = sum(avatar_counts.values()) + sum(daemon_counts.values())
        source_agent = getattr(agent, "agent_name", None) or ""
        truncated = avatar_truncated or daemon_truncated
        data = {
            "schema_version": 1,
            "created_at": _now_iso(),
            "source_agent": source_agent,
            "initiator": initiator,
            "source": source,
            "molt_count": molt_count,
            "awareness_only": True,
            "automatic_lifecycle_actions": False,
            "descendant_scan": "direct_only",
            "counts": {
                "avatars": avatar_counts,
                "daemons": daemon_counts,
            },
            "avatars": avatars,
            "daemons": daemons,
            "limits": {
                "max_avatar_ledger_lines": _MAX_AVATAR_LEDGER_LINES,
                "max_avatar_entries": _MAX_AVATAR_ENTRIES,
                "max_daemon_dirs": _MAX_DAEMON_DIRS,
                "max_daemon_entries": _MAX_DAEMON_ENTRIES,
                "truncated": truncated,
                "avatars_truncated": avatar_truncated,
                "daemons_truncated": daemon_truncated,
            },
        }
        publish_notification(
            agent._working_dir,
            _POST_CHILD_DELEGATION_CHANNEL,
            header=f"post-molt delegation reminder — {total} active work item"
            f"{'' if total == 1 else 's'}",
            icon="🧭",
            priority="high",
            instructions=_POST_CHILD_DELEGATION_INSTRUCTIONS,
            data=data,
        )
    except Exception as e:
        try:
            agent._log("post_child_delegation_notification_failed", error=str(e))
        except Exception:
            pass


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
        import uuid as _uuid
        from datetime import datetime, timezone

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

        # Stable-ish identifier for this continuation so the agent and any
        # frontend can reference a specific molt without colliding across
        # restarts (molt_count alone repeats if the manifest is reset).
        molt_id = f"molt-{molt_count}-{_uuid.uuid4().hex[:8]}"
        molt_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        source_agent = getattr(agent, "agent_name", None) or ""

        data = {
            "molt_id": molt_id,
            "molt_at": molt_at,
            "source_agent": source_agent,
            "initiator": initiator,
            "source": source,
            "molt_count": molt_count,
            "reminder": reminder,
            "ack_options": ["continue", "defer", "obsolete"],
            "summary_path": summary_rel,
            "tokens_before": before_tokens,
            "tokens_after": after_tokens,
        }
        if reasoning:
            data["reasoning"] = reasoning

        instructions = (
            "You just completed a molt (continuation signal — NOT auto-executed). "
            "Reconstruct your context yourself: read system/pad.md, the latest "
            "summary under system/summaries/ (see summary_path), and the most "
            "recent human-channel messages — then decide what to do. Do not treat "
            "any stored text as a command to run blindly. Once reoriented, "
            "explicitly ack by one of: (a) CONTINUE — resume the task, then "
            "system(action='dismiss', channel='post-molt', reason='continue: ...'); "
            "(b) DEFER — record why in pad.md/knowledge, then dismiss with "
            "reason='defer: ...'; "
            "(c) OBSOLETE — record why it no longer applies, then dismiss with "
            "reason='obsolete: ...'. "
            "A reason is required on dismiss. Until you dismiss it, this reminder "
            "re-injects every session so an early stalled/interrupted tool call "
            "cannot make the task fall silent."
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
    _publish_post_child_delegation_reminder(
        agent,
        initiator="agent",
        source="agent",
        molt_count=agent._molt_count,
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
    _publish_post_child_delegation_reminder(
        agent,
        initiator="system",
        source=source,
        molt_count=agent._molt_count,
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
