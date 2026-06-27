"""Tool-result artifact store — preventive spill + retroactive history compaction.

Two related concerns live here:

1. **Preventive spill** (``spill_oversized_result``) — called by ``ToolExecutor``
   on every newly-built tool result.  If serialized content exceeds
   ``PREVENTIVE_MAX_CHARS`` (200_000 hard ceiling), the full original is written to
   ``<workdir>/tmp/tool-results/<…>.{json,txt}`` and a compact manifest dict
   (``status="spilled"``, ``artifact="lingtai_tool_result_spill"``) replaces
   the wire-bound content.

2. **Retroactive compaction** (``compact_oversized_history``) — called by the
   AED retry path in ``base_agent/turn.py`` *before* the LLM retry/replay
   happens.  Walks the live ``ChatInterface._entries`` and rewrites any
   ``ToolResultBlock.content`` that already grew past ``RETROACTIVE_MAX_CHARS``
   (5_000) into the same manifest shape.  Entry order, role, ids,
   ``tool_call``/``tool_result`` pairing, and ``synthesized`` flags are
   untouched — only ``ToolResultBlock.content`` is mutated.

Both paths produce the same manifest dict, recognised by
``is_spill_manifest``.  The retroactive helper uses that recogniser to skip
content that is already a manifest, making compaction idempotent across
repeated AED retries.

The 200K hard cap on the live wire vs. the 5K cap on history is deliberate: a
freshly-built result has room for stamp_meta and small reserved warnings,
while a result already sitting in history is a sunk cost we want to shrink
hard before retry to free up provider tokens.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import re as _re
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Stable, namespaced literal stamped into every manifest produced by this
# module.  Detectors require it for new manifests; older manifests that
# pre-date this field are still accepted by ``is_spill_manifest`` via the
# legacy-shape branch so persisted history from earlier turns stays
# readable.
ARTIFACT_MARKER = "lingtai_tool_result_spill"

# Top-level reserved fields that ``ToolExecutor`` attaches to dict-shaped
# primary results before they reach the wire.  When the primary result
# itself is oversized and gets spilled, replacing the whole dict with the
# manifest would silently drop provider-visible advisory metadata.  The
# advisory payload is small by construction.
#
# Deliberately tight allowlist.  Arbitrary business-level top-level keys
# (e.g. a tool returning ``{"data": [...]}``) are NOT hoisted — that's
# what the artifact file is for.  ``_meta`` is also intentionally
# omitted: it's stamped by ``stamp_meta`` and a copy lives in the
# artifact; agents that want timing/context can read the sidecar.
_HOISTED_RESERVED_FIELDS = ("_advisory",)

# Preventive cap — applied by ToolExecutor on every freshly built tool
# result, before it reaches the LLM wire. This is the non-configurable hard
# ceiling for provider-visible tool results.
PREVENTIVE_MAX_CHARS = 200_000

# Retroactive cap — applied by the AED recovery path to results already
# committed to the chat interface.  Tighter than the preventive cap so the
# pre-retry compaction actually frees space.
RETROACTIVE_MAX_CHARS = 5_000

# Filename slugging — keep tool/call-id readable but filesystem-safe.
_FILENAME_SAFE_RE = _re.compile(r"[^A-Za-z0-9_.-]+")


def _serialized_len(value: Any) -> int:
    """Return the JSON-serialized character length used for the cap check."""
    if isinstance(value, str):
        return len(value)
    try:
        return len(_json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _slug(value: str, *, limit: int = 40) -> str:
    cleaned = _FILENAME_SAFE_RE.sub("_", value).strip("_") or "tool"
    return cleaned[:limit]


def is_spill_manifest(value: Any) -> bool:
    """Return True iff ``value`` is a manifest produced by ``spill_oversized_result``.

    Detection is conservative.  The preferred shape carries the explicit
    namespaced marker ``artifact == ARTIFACT_MARKER`` *and* the required
    structural fields (``status="spilled"``, ``spill_path`` key,
    ``cap_chars``, ``original_char_count``) — this matches everything
    produced by the current implementation and refuses arbitrary business
    dicts that happen to use ``status`` + ``spill_path`` independently.

    Backward-compatible legacy branch: dicts without the marker are still
    accepted as manifests when *all four* structural fields are present
    with the right types.  This preserves recognition of any persisted
    history from earlier turns of this same patch (which produced manifests
    before the marker was added) but rejects unrelated dicts that happen to
    share one or two keys.
    """
    if not isinstance(value, dict):
        return False
    if value.get("status") != "spilled":
        return False
    if "spill_path" not in value:
        return False
    if value.get("artifact") == ARTIFACT_MARKER:
        return True
    # Legacy-shape fallback: require the full structural quadruple.
    return (
        "cap_chars" in value
        and "original_char_count" in value
        and isinstance(value.get("cap_chars"), int)
        and isinstance(value.get("original_char_count"), int)
    )


def spill_oversized_result(
    result: Any,
    *,
    max_chars: int,
    tool_name: str | None,
    tool_call_id: str | None,
    working_dir: Path | str | None,
    source: str = "preventive",
) -> Any:
    """Spill a too-large tool result to a sidecar file; return a compact manifest.

    If ``result`` already is a spill manifest, returns it unchanged (the
    history may have been compacted in a previous pass).  If the serialized
    length is ``<= max_chars``, returns ``result`` unchanged.

    Otherwise writes the *full* canonical serialization to
    ``<working_dir>/tmp/tool-results/<timestamp>-<tool>-<id>-<uuid>.<ext>``
    and returns a small dict containing a warning, the artifact paths (both
    workdir-relative and absolute), original size, cap, tool/call metadata,
    a UTC timestamp, a short preview, and a ``source`` field marking which
    code path produced the spill (for example ``"preventive"``,
    ``"retroactive"``, or ``"recovered_from_events"``).

    When the original is a dict, reserved provider-visible fields listed in
    ``_HOISTED_RESERVED_FIELDS`` (currently ``_advisory``) are
    copied verbatim from the original onto the manifest so they survive the
    wire replacement.  The artifact file always holds the complete
    post-dispatch original, including those fields, so nothing is lost — the
    hoist only makes advisory metadata visible on the wire-bound copy.

    When ``working_dir`` is None or the write fails, returns the manifest
    with ``spill_path`` / ``spill_path_abs`` set to None and a
    ``spill_error`` field — the wire is still safe (compact and capped),
    but the full content is unreachable.  Callers must provide a writable
    workdir to guarantee the "artifact contains the full original"
    invariant.
    """
    if max_chars <= 0:
        return result
    if is_spill_manifest(result):
        return result

    original_chars = _serialized_len(result)
    if original_chars <= max_chars:
        return result

    # Compute byte size (UTF-8) of the canonical serialization for the manifest.
    if isinstance(result, str):
        serialized_text = result
    else:
        try:
            serialized_text = _json.dumps(result, ensure_ascii=False, default=str, indent=2)
        except (TypeError, ValueError):
            serialized_text = str(result)
    original_bytes = len(serialized_text.encode("utf-8"))

    timestamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    iso_timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    tool_slug = _slug(tool_name or "tool")
    id_slug = _slug(tool_call_id) if tool_call_id else _uuid.uuid4().hex[:8]
    ext = "txt" if isinstance(result, str) else "json"
    # Append a short uuid to defuse intra-second collisions across parallel
    # calls that share or lack a tool_call_id.
    unique = _uuid.uuid4().hex[:6]
    filename = f"{timestamp}-{tool_slug}-{id_slug}-{unique}.{ext}"

    spill_path_str: str | None = None
    spill_path_abs: str | None = None
    spill_failed: str | None = None
    if working_dir is not None:
        wd = Path(working_dir)
        spill_dir = wd / "tmp" / "tool-results"
        try:
            spill_dir.mkdir(parents=True, exist_ok=True)
            spill_path = spill_dir / filename
            spill_path.write_text(serialized_text, encoding="utf-8")
            spill_path_str = str(spill_path.relative_to(wd))
            spill_path_abs = str(spill_path.resolve())
        except OSError as exc:
            spill_failed = f"{type(exc).__name__}: {exc}"

    # Build the compact manifest.  Preview is a head of the canonical text,
    # bounded so the manifest itself stays comfortably under the cap even
    # after `stamp_meta` adds ~200-400 chars.
    preview_budget = max(0, max_chars - 1500)
    preview_budget = min(preview_budget, 2000)
    preview = serialized_text[:preview_budget]
    if len(serialized_text) > preview_budget:
        preview += f"\n[... {len(serialized_text) - preview_budget} more chars in artifact ...]"

    warning = (
        f"Tool result was too large ({original_chars} chars, cap {max_chars}) "
        "and was written to a sidecar file under tmp/tool-results/ which is "
        "ephemeral and may be cleaned up. If the file is missing, the full "
        "content is gone — use the preview below or rerun the source tool."
    )

    manifest: dict[str, Any] = {
        "artifact": ARTIFACT_MARKER,
        "status": "spilled",
        "source": source,
        "warning": warning,
        "spill_path": spill_path_str,
        "spill_path_abs": spill_path_abs,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "original_char_count": original_chars,
        "original_byte_count": original_bytes,
        "cap_chars": max_chars,
        "timestamp": iso_timestamp,
        "preview": preview,
        "artifact_lifetime": "ephemeral_tmp",
        "artifact_state": "available",
    }
    if spill_failed is not None:
        manifest["spill_error"] = spill_failed
    if working_dir is None:
        manifest["spill_error"] = manifest.get("spill_error") or "no working_dir configured"

    # When the sidecar could not be written (no working_dir or write
    # failure), the full content is unreachable — mark as unavailable.
    if spill_path_str is None:
        manifest["artifact_state"] = "unavailable"

    # Hoist a small allowlist of provider-visible reserved fields from a
    # dict-shaped original onto the manifest, so loop-guard duplicate
    # warnings reach the wire even when the primary payload was too large to
    # inline.  The allowlist is deliberately tight
    # (`_HOISTED_RESERVED_FIELDS`); arbitrary business keys live in the
    # artifact only.  Hoisting runs BEFORE the defensive trim loop so the
    # preview-trim accounts for the hoisted bytes.
    if isinstance(result, dict):
        for key in _HOISTED_RESERVED_FIELDS:
            if key in result:
                manifest[key] = result[key]

    # Defensive: if the manifest itself somehow exceeds the cap (e.g. an
    # absurd tool_call_id or unexpectedly large reserved warning), trim the
    # preview further.  Loop is bounded; this is defence-in-depth, not a
    # routine code path.
    for _ in range(4):
        if _serialized_len(manifest) <= max_chars:
            break
        preview = manifest["preview"]
        if not preview:
            break
        manifest["preview"] = preview[: max(0, len(preview) // 2)]
    return manifest


@dataclass
class CompactionStats:
    """Summary of a single ``compact_oversized_history`` pass.

    Bounded shape — safe to log verbatim without blowing up the event log
    even on large transcripts.  ``artifact_paths`` is capped at
    ``_MAX_LOGGED_ARTIFACT_PATHS`` (16) entries to keep individual log
    lines small while still letting an operator see what was spilled.
    """

    scanned_blocks: int = 0
    compacted_blocks: int = 0
    original_chars_total: int = 0
    replacement_chars_total: int = 0
    artifact_paths: list[str] = field(default_factory=list)

    def to_log_fields(self) -> dict[str, Any]:
        return {
            "scanned_blocks": self.scanned_blocks,
            "compacted_blocks": self.compacted_blocks,
            "original_chars_total": self.original_chars_total,
            "replacement_chars_total": self.replacement_chars_total,
            "artifact_paths": list(self.artifact_paths),
        }


_MAX_LOGGED_ARTIFACT_PATHS = 16


def compact_oversized_history(
    interface: Any,
    *,
    working_dir: Path | str | None,
    max_chars: int = RETROACTIVE_MAX_CHARS,
    logger_fn: Callable[..., None] | None = None,
) -> CompactionStats:
    """Rewrite oversized tool-result content in the live chat interface in place.

    Walks ``interface._entries`` and replaces ``ToolResultBlock.content``
    whose serialized length exceeds ``max_chars`` with the same compact
    manifest produced by ``spill_oversized_result``.  Entries are never
    reordered, never deleted, and no field other than ``ToolResultBlock.content``
    is touched — id / name / synthesized / pairing with ``ToolCallBlock``
    remain intact.

    Idempotent: content that is already a spill manifest (detected by
    ``is_spill_manifest``) is skipped, so repeated invocations across
    successive AED retries do not duplicate artifacts.

    Returns a ``CompactionStats`` summary.  Designed to be a safe no-op
    when ``interface`` is None, lacks ``_entries`` / ``entries``, or when
    ``working_dir`` is None — callers can invoke it without pre-checks
    and rely on ``stats.compacted_blocks`` to decide whether downstream
    persistence (save chat history) is needed.
    """
    stats = CompactionStats()
    if interface is None:
        return stats
    # Late import to avoid a circular import at module load time —
    # ``llm.interface`` imports nothing from this module today, but the
    # base_agent package pulls both transitively.
    try:
        from .llm.interface import ToolResultBlock
    except ImportError:
        return stats

    entries = getattr(interface, "_entries", None)
    if entries is None:
        entries = getattr(interface, "entries", None)
    if not entries:
        return stats

    for entry in entries:
        content = getattr(entry, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, ToolResultBlock):
                continue
            stats.scanned_blocks += 1
            if is_spill_manifest(block.content):
                continue
            current_chars = _serialized_len(block.content)
            if current_chars <= max_chars:
                continue
            manifest = spill_oversized_result(
                block.content,
                max_chars=max_chars,
                tool_name=block.name,
                tool_call_id=block.id,
                working_dir=working_dir,
                source="retroactive",
            )
            # Only mutate the content field — pairing / id / name /
            # synthesized must survive untouched so the wire alternation
            # and tool_use/tool_result correlation stay valid.
            block.content = manifest
            stats.compacted_blocks += 1
            stats.original_chars_total += current_chars
            stats.replacement_chars_total += _serialized_len(manifest)
            spill_path = manifest.get("spill_path") if isinstance(manifest, dict) else None
            if spill_path and len(stats.artifact_paths) < _MAX_LOGGED_ARTIFACT_PATHS:
                stats.artifact_paths.append(spill_path)
            if logger_fn is not None:
                try:
                    logger_fn(
                        "tool_result_compacted_retroactively",
                        tool_name=block.name,
                        tool_call_id=block.id,
                        original_char_count=manifest.get("original_char_count"),
                        spill_path=spill_path,
                    )
                except Exception:
                    pass
    return stats


def mark_expired_spill_manifests(working_dir: Path | str) -> int:
    """Scan persisted chat history for spill manifests whose sidecar files are gone.

    Walks ``<working_dir>/history/chat_history.jsonl`` and, for every JSON
    line that contains a spill manifest (recognised by ``is_spill_manifest``),
    checks whether the ``spill_path`` file still exists on disk.

    * If **missing**: sets ``artifact_state="expired"`` and stamps
      ``artifact_expired_at`` with the current UTC ISO timestamp.
    * If **present**: ensures ``artifact_state="available"`` (no timestamp).

    The file is rewritten **only if** at least one manifest was mutated, so
    the function is idempotent and cheap when nothing changed.

    Returns the count of manifests whose sidecar was missing (i.e. now
    marked ``"expired"``).
    """
    wd = Path(working_dir)
    history_path = wd / "history" / "chat_history.jsonl"
    if not history_path.is_file():
        return 0

    lines = history_path.read_text(encoding="utf-8").splitlines()
    changed = False
    expired_count = 0
    now_iso: str | None = None  # lazy — only generated if needed

    def _mark_manifest(manifest: dict) -> None:
        nonlocal changed, expired_count, now_iso
        spill_path = manifest.get("spill_path")
        if not spill_path:
            return
        # Backfill artifact_lifetime for legacy manifests that predate #192.
        if "artifact_lifetime" not in manifest:
            manifest["artifact_lifetime"] = "ephemeral_tmp"
            changed = True
        abs_path = wd / spill_path
        if abs_path.is_file():
            # Sidecar still on disk — ensure available state.
            if manifest.get("artifact_state") != "available":
                manifest["artifact_state"] = "available"
                manifest.pop("artifact_expired_at", None)
                changed = True
        else:
            # Sidecar gone — mark expired.
            expired_warning = (
                "EXPIRED: This tool result was stored in a temporary "
                "sidecar file that no longer exists. The full content is "
                "unavailable. Use the preview below if sufficient, or "
                "rerun the source tool to regenerate the result."
            )
            if manifest.get("artifact_state") != "expired":
                if now_iso is None:
                    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(
                        timespec="seconds"
                    )
                manifest["artifact_state"] = "expired"
                manifest["artifact_expired_at"] = now_iso
                manifest["warning"] = expired_warning
                changed = True
                expired_count += 1
            elif "artifact_expired_at" not in manifest:
                # Already expired but missing timestamp — backfill.
                if now_iso is None:
                    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(
                        timespec="seconds"
                    )
                manifest["artifact_expired_at"] = now_iso
                changed = True
            # Always overwrite stale warning text on expired manifests.
            if manifest.get("warning") != expired_warning:
                manifest["warning"] = expired_warning
                changed = True

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            if is_spill_manifest(value):
                _mark_manifest(value)
            else:
                for v in value.values():
                    _walk(v)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    new_lines: list[str] = []
    for line in lines:
        if not line.strip():
            new_lines.append(line)
            continue
        try:
            entry = _json.loads(line)
        except (_json.JSONDecodeError, ValueError):
            new_lines.append(line)
            continue
        _walk(entry)
        new_lines.append(_json.dumps(entry, ensure_ascii=False, default=str))

    if changed:
        history_path.write_text(
            "\n".join(new_lines) + ("\n" if new_lines else ""),
            encoding="utf-8",
        )
    return expired_count
