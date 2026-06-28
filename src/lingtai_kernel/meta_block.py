"""Unified per-turn metadata injection.

Single source of truth for "what the agent sees about its own runtime state
on every turn." Both injection sites — text-input prefix (in BaseAgent) and
tool-result stamp (in ToolExecutor) — read from here.

Curate carefully: every field added to `build_meta` ships on every text input
and every tool result.

All four tool-result metadata blocks live under a single ``_meta`` envelope on
the result dict:

- ``_meta.tool_meta`` — permanent per-result identity facts, written once by
  ``ToolExecutor._attach_tool_block`` and never moved.
- ``_meta.agent_meta`` — latest-result-only agent/current-state snapshot.
- ``_meta.guidance`` — latest-result-only lightweight ref/hook pointing at the
  resident ``meta_guidance`` system-prompt section (built by
  ``build_meta_guidance``), where the full kernel guidance sections, the
  ``_meta`` readme, and any static adapter runtime rules now live.  The full
  ordered appendix is no longer re-stamped on every tail result.
- ``_meta.notifications`` / ``_meta.notification_guidance`` — latest-result-only
  channel-owned notification payloads plus kernel safety framing.

Channel encoding:
- Tool-result channel: ``stamp_meta`` records a per-tool runtime snapshot,
  which ``attach_active_runtime`` promotes into ``_meta.agent_meta`` plus
  ``_meta.guidance`` on the *latest* result dict only (latest-only; the prior
  holder's blocks are stripped).
- Text-input channel: `render_meta` formats the same dict into a prose
  prefix line. Inbox content is NOT rendered here — it lives in the
  user-turn body, drained by ``_concat_queued_messages`` upstream.

As of 2026-05-02, the meta block no longer carries inbox-drained
notifications. System-source notifications (mail arrival, bounce, future
MCP events) are now delivered as synthetic notification(action="check")
tool-call pairs spliced by ``BaseAgent._inject_notification_pair`` (the
legacy ``tc_inbox`` splice path is dormant); see
docs/plans/2026-05-02-system-notification-as-tool-call.md.
"""
from __future__ import annotations

import json as _json
import time as _time
from collections.abc import Mapping

from .config import (
    MOLT_NOTICE_THRESHOLD,
    CONTEXT_PRESSURE_RECONSTRUCTION_RATIO,
    CONTEXT_PRESSURE_RECOVERY_TARGET,
)
from .i18n import t as _t
from .time_veil import now_iso

# ---------------------------------------------------------------------------
# The single ``_meta`` envelope key and its four nested blocks.  Every dict
# tool result carries ``result["_meta"]``; the blocks beneath it are:
#   * ``tool_meta``            — permanent, per-result (every tool result)
#   * ``agent_meta``           — latest-result-only agent/current state
#   * ``guidance``             — latest-result-only kernel guidance
#   * ``notifications`` +
#     ``notification_guidance``— latest-result-only channel payloads
# ---------------------------------------------------------------------------
META_ENVELOPE_KEY = "_meta"
TOOL_META_KEY = "tool_meta"
AGENT_META_KEY = "agent_meta"
GUIDANCE_KEY = "guidance"
NOTIFICATIONS_KEY = "notifications"
NOTIFICATION_GUIDANCE_KEY = "notification_guidance"

# Per-result machine-generated guidance nested under ``tool_meta``.  ``comment``
# is a small map of topic-keyed hints; today the only topic is ``overflow`` — a
# hint stamped on capped/large visible tool results pointing the agent at the
# preserved original and the cleanup action.  It is guidance, not a
# notification, not global guidance, and not a strict state machine: a quiet
# per-result note that rides on the permanent ``tool_meta`` block.
TOOL_META_COMMENT_KEY = "comment"
TOOL_META_COMMENT_OVERFLOW_KEY = "overflow"
TOOL_META_TOKEN_USAGE_KEY = "token_usage"
TOOL_META_TOKEN_USAGE_PENDING_KEY = "_tool_meta_token_usage"


def build_tool_meta_overflow_comment(tool_call_id: str | None) -> dict:
    """Return the ``tool_meta.comment.overflow`` hint for a capped/large result.

    Stamped only when the model-visible payload is capped or large (the caller
    decides; see :meth:`ToolExecutor._attach_tool_block`).  LingTai preserves the
    full, un-capped original in the durable runtime log, so the hint points there
    by ``tool_call_id`` rather than at any external sidecar/saved-path file.

    There is deliberately exactly one comment topic for this feature —
    ``overflow``.  All guidance (what happened, where the original is, how to
    retrieve it, what to do after consuming it) lives under this single key, not
    split across parallel ``comment.retrieval`` / ``comment.summarize`` headings.
    """
    call_id = tool_call_id or "<unknown>"
    return {
        "summary": (
            "The model-visible context for this tool result is capped or large; "
            "what you see here may be a preview or compacted form, not the full payload."
        ),
        "full_original": (
            f"The full original is preserved in logs/events.jsonl under "
            f"tool_call_id={call_id}."
        ),
        "how_to_retrieve": (
            f"Retrieve it from the durable log by tool_call_id: "
            f"grep '{call_id}' <workdir>/logs/events.jsonl, or use "
            f"`lingtai-agent log query` (see the sqlite-log-query manual). For a "
            f"broad extraction, delegate to a daemon/subagent with the "
            f"tool_call_id and the exact question instead of pulling the whole "
            f"original back into your own context."
        ),
        "after_consuming": (
            "After you have consumed what you need, call "
            "system(action=\"summarize\") for this tool_call_id to replace the "
            "visible payload with your own agent-authored summary."
        ),
    }

# Keys that are kernel/runtime scaffolding, not the formal tool-result payload.
# Summarize and large-result reminder sizing must ignore these so notification
# or guidance text is not treated as result content to be summarized.
FORMAL_TOOL_RESULT_EXCLUDED_KEYS = frozenset({
    META_ENVELOPE_KEY,
    "_runtime_pending",
    "_advisory",
    "active_turn_tool_calls",
    "active_turn_tool_call_notice",
})


def formal_tool_result_content(content):
    """Return the formal tool-result payload, excluding kernel metadata.

    The ``_meta`` envelope can contain notifications and guidance that are
    channel/runtime state, not the payload returned by the tool.  Context
    summarization and large-result reminder sizing operate on this formal body
    only, so notification contents are neither threshold-counted nor
    summarized as if they were the result.
    """
    if not isinstance(content, dict):
        return content
    return {
        key: value
        for key, value in content.items()
        if key not in FORMAL_TOOL_RESULT_EXCLUDED_KEYS
    }


def _visible_content_text(content) -> str:
    if isinstance(content, str):
        return content
    try:
        return _json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def formal_tool_result_visible_len(content) -> int:
    """Visible character length of the formal tool-result payload only."""
    return len(_visible_content_text(formal_tool_result_content(content)))


def formal_tool_result_preview(content, limit: int = 200) -> str:
    """Preview string for the formal tool-result payload only."""
    if limit <= 0:
        return ""
    return _visible_content_text(formal_tool_result_content(content))[:limit]



def _is_tool_result_block(block) -> bool:
    """Best-effort duck-typing for ToolResultBlock without a hard import cycle."""
    return block.__class__.__name__ == "ToolResultBlock" and hasattr(block, "content")


def _iter_history_tool_result_blocks(agent):
    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    interface = getattr(chat, "interface", None)
    entries = getattr(interface, "_entries", None)
    if not entries:
        return
    for entry in entries:
        for block in getattr(entry, "content", ()) or ():
            if _is_tool_result_block(block):
                yield block


def adapter_comment(agent):
    """Return an optional adapter-authored, agent-facing runtime note."""

    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "adapter_comment", None)
    if not callable(comment_fn):
        return None
    try:
        return comment_fn()
    except Exception:
        # `_meta.agent_meta` must never be made unavailable by an adapter note.
        return None


def static_adapter_comment(agent):
    """Return the adapter's static/rule-like runtime note (no dynamic state).

    The static comment is the durable explanation of how the active adapter's
    continuation/caching/summarize machinery behaves; it does not change turn to
    turn.  It is rendered once into the resident ``meta_guidance`` system-prompt
    section rather than re-stamped onto every tail ``_meta``.  Adapters expose it
    via a ``static_adapter_comment`` method; adapters without one simply
    contribute nothing to ``meta_guidance``.  Prefer the service/adapter-level
    hook because the first prompt build happens before a ChatSession exists; the
    chat-level hook remains as a compatibility fallback.
    """
    service = getattr(agent, "service", None)
    comment_fn = getattr(service, "static_adapter_comment", None)
    if callable(comment_fn):
        try:
            comment = comment_fn()
        except Exception:
            comment = None
        if comment:
            return comment

    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "static_adapter_comment", None)
    if not callable(comment_fn):
        return None
    try:
        return comment_fn()
    except Exception:
        return None


def dynamic_adapter_comment(agent: AgentState) -> Mapping[str, Any] | None:
    """Return adapter-owned dynamic tail state for ``_meta.agent_meta``.

    Adapters that can separate static guidance from dynamic runtime state should
    implement ``dynamic_adapter_comment``.  For legacy adapters, fall back to the
    combined ``adapter_comment`` payload; the generic tail slimmer will only
    trim oversized structures, not guess adapter-specific static keys.
    """
    session = getattr(agent, "_session", None)
    chat = getattr(session, "chat", None)
    comment_fn = getattr(chat, "dynamic_adapter_comment", None)
    if callable(comment_fn):
        try:
            comment = comment_fn()
        except Exception:
            logger.debug("llm_dynamic_adapter_comment_failed", exc_info=True)
            comment = None
        if comment:
            if not isinstance(comment, Mapping):
                return {"note": str(comment)}
            return dict(comment)
    return adapter_comment(agent)


def slim_adapter_comment_for_tail(
    comment: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Trim dynamic adapter tail payload without guessing static keys.

    Static-vs-dynamic partitioning is owned by the adapter via
    ``static_adapter_comment`` / ``dynamic_adapter_comment``.  The kernel only
    removes verbose dynamic structures that are too heavy for every-turn tail
    metadata and adds a hook back to the resident ``meta_guidance`` section.
    """
    if not comment:
        return None
    if not isinstance(comment, Mapping):
        return {"note": str(comment)}

    slim: dict[str, Any] = dict(comment)
    ledger = slim.pop("cache_ledger", None)
    if isinstance(ledger, Mapping):
        summary = ledger.get("summary")
        if isinstance(summary, Mapping) and "cache_ledger_summary" not in slim:
            slim["cache_ledger_summary"] = dict(summary)
        last_full = ledger.get("last_full")
        if isinstance(last_full, Mapping):
            slim.setdefault("last_full_api_calls_ago", last_full.get("api_calls_ago"))
            slim.setdefault("last_full_reason", last_full.get("reason"))
        last_ws_full = ledger.get("last_ws_full")
        if isinstance(last_ws_full, Mapping):
            slim.setdefault(
                "last_ws_full_api_calls_ago",
                last_ws_full.get("api_calls_ago"),
            )
            slim.setdefault("last_ws_full_reason", last_ws_full.get("reason"))

    hint = slim.get("maintenance_hint")
    if isinstance(hint, Mapping):
        compact_hint = dict(hint)
        compact_hint.pop("reason", None)
        if compact_hint:
            slim["maintenance_hint"] = compact_hint
        else:
            slim.pop("maintenance_hint", None)

    return slim or None


TOOL_RESULT_CHARS_TOP_N = 5
TOOL_RESULT_CHARS_MIN_TOP_CHARS = 1000
TOOL_RESULT_CHARS_README = (
    "listing top 5 tool results over 1000 chars by char count "
    "(id, tool_name, chars; no preview); no need to summarize this helper "
    "(it appears only on the latest tool result _meta and older copies are "
    "stripped); these are summarize candidates, not a directive to summarize "
    "every entry: prefer summarizing prior results that are already "
    "consumed/digested and useless, irrelevant, obsolete, or no longer needed "
    "in full, weighing context pressure, recoverability from logs, and future "
    "reuse/token savings, and batch them by the listed ids/tool names; if an "
    "adapter comment is present, follow its adapter-specific summarize rules too"
)


def _tool_result_id(block) -> str:
    return str(getattr(block, "id", None) or getattr(block, "tool_call_id", None) or "")


def _tool_result_name(block) -> str:
    return str(getattr(block, "name", None) or getattr(block, "tool_name", None) or "")


def current_tool_result_chars(agent, extra_results=()) -> dict:
    """Return current context-visible formal tool-result char summary.

    The count is intentionally based on formal result payloads rather than
    runtime metadata.  ``_meta`` notifications/guidance, transient scaffolding,
    and other non-payload fields are excluded by
    ``formal_tool_result_visible_len``.  ``extra_results`` lets latest-result
    stamping include the just-created tool-result batch before those blocks are
    appended to chat history.
    """
    total = 0
    top: list[dict] = []
    seen: set[int] = set()

    def visit(block) -> None:
        nonlocal total
        seen.add(id(block))
        content = getattr(block, "content", "")
        chars = formal_tool_result_visible_len(content)
        total += chars
        if chars > TOOL_RESULT_CHARS_MIN_TOP_CHARS:
            top.append(
                {
                    "id": _tool_result_id(block),
                    "tool_name": _tool_result_name(block),
                    "chars": chars,
                }
            )

    for block in _iter_history_tool_result_blocks(agent) or ():
        visit(block)
    for block in extra_results or ():
        if not _is_tool_result_block(block) or id(block) in seen:
            continue
        visit(block)

    top.sort(key=lambda item: item["chars"], reverse=True)
    return {
        "total_chars": total,
        "top_results": top[:TOOL_RESULT_CHARS_TOP_N],
    }


def _meta_block(result: dict) -> dict:
    """Return ``result["_meta"]``, creating an empty dict if absent.

    Centralizes the envelope so the per-result ``tool_meta`` writer and the
    latest-only ``agent_meta``/``guidance``/notification movers all share one
    container.
    """
    meta = result.get(META_ENVELOPE_KEY)
    if not isinstance(meta, dict):
        meta = {}
        result[META_ENVELOPE_KEY] = meta
    return meta


def build_meta_readme() -> dict:
    """Self-describing readme for the five ``_meta`` blocks.

    This readme is rendered once into the resident ``meta_guidance``
    system-prompt section (via :func:`build_meta_guidance`), not stamped onto
    every tool result; the tail ``_meta.guidance`` carries only a lightweight
    ref back to that section.  Each entry states what the block is for and
    whether it is per-result or latest-only — no policy, just structural
    orientation.
    """
    return {
        TOOL_META_KEY: (
            "Per-result tool/call metadata (id, timestamp, char_count, "
            "elapsed_ms, optional token_usage). Present on every tool result; "
            "permanent. token_usage is a compact provider-round token/cache "
            "snapshot with keys input, cache_miss, cache_rate, context_usage, "
            "window, output, thinking — copied here so agents can inspect "
            "historical high-context summarize/rebuild costs after the latest "
            "agent_meta snapshot has moved on. May also "
            "carry a one-shot 'reconstruction' event when the runtime just "
            "performed a delayed-summarize context reconstruction: it records "
            "the before (A) and after (B) context tokens/usage, context_window, "
            "trigger_threshold (0.75) and recovery_target (0.6); if B is still "
            "at/above the recovery target it includes a natural-language molt "
            "reminder. This is permanent evidence of a past event, not current "
            "state — distinct from agent_meta.context.molt."
        ),
        AGENT_META_KEY: (
            "Agent/current-state snapshot (time, context usage, stamina, "
            "current-session token_efficiency, active_turn_tool_calls, "
            "current_tool_result_chars, optional adapter_comment). context may "
            "carry a 'molt' reminder string: a SUSTAINED-pressure warning that appears "
            "only after context has been high (>= 0.75) for several consecutive "
            "fresh provider rounds and clears when pressure drops — current "
            "state, not a permanent event (cf. tool_meta.reconstruction). Latest "
            "tool result only; older copies are stripped as newer results "
            "arrive. token_efficiency is a compact latest-only current_session token "
            "economy snapshot with api_calls, cumulative input/cached tokens, cache "
            "rate, average input tokens per API call, context tokens/window, "
            "and a guidance_ref back to meta_guidance.token_efficiency; per-round "
            "token/cache facts live permanently in tool_meta.token_usage instead. "
            "current_tool_result_chars is a compact dict with total_chars "
            "and top_results (id, tool_name, chars; no preview) for "
            "proactive summarization candidates. adapter_comment is a small "
            "provider/adapter-authored note carrying only dynamic per-turn "
            "runtime scalars; the adapter's static "
            "rules live in the system-prompt section meta_guidance."
        ),
        GUIDANCE_KEY: (
            "Lightweight ref/hook to the resident system-prompt section "
            "meta_guidance, where the full kernel guidance sections, this "
            "_meta envelope readme, and any static adapter runtime rules live. "
            "Latest tool result only; carries no full guidance body."
        ),
        NOTIFICATION_GUIDANCE_KEY: (
            "Kernel safety framing for channel notification handling. Latest "
            "tool result only."
        ),
        NOTIFICATIONS_KEY: (
            "Channel notification payloads. Static safety framing lives under "
            "notification_guidance/meta_guidance; per-channel duplicate guidance is omitted. Latest tool result only; channel-owned. "
            "Not part of the formal tool-result payload; do not summarize "
            "notification contents as the result body."
        ),
    }


def now_iso_plain() -> str:
    """Return the current UTC time as a plain ISO-8601 string (no agent needed).

    Used by ``_meta.tool_meta`` block stamping where no agent context is available.
    Always returns UTC with a Z suffix, e.g. ``2026-06-20T12:34:56Z``.
    Falls back to empty string on any error.
    """
    try:
        import datetime as _dt
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Runtime guidance catalog — prompt package resource, loaded once.
# ---------------------------------------------------------------------------

_GUIDANCE_CACHE: dict | None = None

# Allowed values for the small fixed-vocabulary fields. Kept permissive on
# purpose: the kernel must not reject a future render strategy it does not yet
# know about, only structurally malformed payloads.
_GUIDANCE_REQUIRED_TOP_KEYS = ("schema_version", "guidance_version", "priority", "render_mode", "sections")


class GuidanceSchemaError(ValueError):
    """Raised when the runtime guidance payload does not match the expected shape.

    A structural problem in the *packaged* resource is a build/authoring error,
    not a runtime condition, so this is surfaced loudly to ``validate_runtime_guidance``
    callers (and the test suite). The live loader (``build_runtime_guidance``)
    degrades to ``{}`` rather than crashing an agent on a bad ship.
    """



META_README_SECTION_ID = "meta_readme"


def build_meta_readme_section() -> Dict[str, str]:
    """Return the guidance section that explains the `_meta` envelope.

    This readme is one ordered section among the kernel guidance sections; both
    are rendered into the resident ``meta_guidance`` system-prompt section (see
    :func:`build_meta_guidance`).  The tail ``_meta.guidance`` on tool results is
    only a lightweight ref back to that section, never the full body.
    """
    readme = build_meta_readme()
    body_lines = [
        "This section explains the `_meta` envelope carried on tool results.",
        "These explanations are resident here in the `meta_guidance` system-prompt section; the tail `_meta.guidance` on each tool result carries only a lightweight ref back to this section, not the full body.",
        "",
    ]
    body_lines.extend(f"- `{key}`: {value}" for key, value in readme.items())
    return {
        "id": META_README_SECTION_ID,
        "title": "_meta envelope readme",
        "body": "\n".join(body_lines),
    }


def build_guidance_with_meta_readme(base_guidance: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Return runtime guidance with the `_meta` readme appended as a section."""
    source = build_runtime_guidance() if base_guidance is None else base_guidance
    guidance = dict(source or {})
    # Preserve packaged guidance keys when available, but keep the fallback shape
    # valid too: even if the guidance catalog cannot be loaded, guidance remains the
    # same system-prompt-like structure with a single meta_readme section.
    guidance.setdefault("schema_version", 1)
    guidance.setdefault("guidance_version", "runtime-meta-readme")
    guidance.setdefault("priority", "high")
    guidance.setdefault("render_mode", "latest_tool_result_only")
    sections = []
    for section in guidance.get("sections") or []:
        if not isinstance(section, dict):
            continue
        if section.get("id") == META_README_SECTION_ID:
            continue
        sections.append(dict(section))
    sections.append(build_meta_readme_section())
    guidance["sections"] = sections
    return guidance

# ---------------------------------------------------------------------------
# meta_guidance — resident system-prompt section.
#
# The static, rule-like content that used to ride in every tail
# ``_meta.guidance`` (the runtime guidance sections + the ``_meta`` readme) and
# in the adapter's ``adapter_comment`` (the long full-epoch/summarize prose) is
# rendered once here and appended as the final, always-resident system-prompt
# section named ``meta_guidance``.  The tail ``_meta`` then carries only a
# lightweight ref pointing back at this section.
# ---------------------------------------------------------------------------

META_GUIDANCE_SECTION_ID = "meta_guidance"


def build_meta_guidance_ref() -> dict:
    """Return the lightweight ``_meta.guidance`` hook for the latest tool result."""
    return {"ref": META_GUIDANCE_SECTION_ID}

def _render_guidance_sections_markdown(guidance: dict) -> list[str]:
    """Render guidance.sections (incl. meta_readme) as Markdown subsections."""
    lines: list[str] = []
    for section in (guidance or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = section.get("title") or section.get("id") or ""
        body = section.get("body") or ""
        if title:
            lines.append(f"### {title}")
        if body:
            lines.append(body)
        lines.append("")
    return lines


def _render_adapter_comment_markdown(comment: dict) -> list[str]:
    """Render a static adapter_comment dict as a Markdown subsection."""
    if not isinstance(comment, dict) or not comment:
        return []
    adapter = comment.get("adapter") or "adapter"
    lines = [f"### {adapter} runtime rules"]
    for key, value in comment.items():
        if key == "adapter":
            continue
        if isinstance(value, str) and value:
            lines.append(f"- `{key}`: {value}")
    lines.append("")
    return lines


def build_meta_guidance(agent) -> str:
    """Render the resident ``meta_guidance`` system-prompt section body.

    Combines the static, rule-like material that previously rode on every tail
    ``_meta``:

      * the runtime guidance sections from the Markdown guidance catalog (e.g.
        summarize/molt best practice);
      * the ``_meta`` envelope readme (which blocks exist and whether each is
        per-result or latest-only);
      * the active adapter's *static* runtime rules (from
        :func:`static_adapter_comment`), if any.

    Dynamic per-result state (tool_meta, current_time/context/stamina,
    notifications, current_tool_result_chars, adapter epoch counters, cache
    ledger summary, …) is deliberately NOT rendered here — it stays in the tail
    ``_meta`` so this section can remain a stable, cache-friendly prefix.

    Returns the Markdown body (no ``## meta_guidance`` header — the prompt
    manager adds the section header).  Returns ``""`` only if nothing renders.
    """
    guidance = build_guidance_with_meta_readme()
    lines: list[str] = [
        "Resident kernel guidance for reading runtime metadata. This is the "
        "static, rule-like material; dynamic per-turn state stays in the tail "
        "`_meta` block on tool results (which points back here via "
        "`_meta.guidance.ref`).",
        "",
    ]
    lines.extend(_render_guidance_sections_markdown(guidance))
    static_comment = static_adapter_comment(agent)
    lines.extend(_render_adapter_comment_markdown(static_comment))
    body = "\n".join(lines).strip()
    return body


def validate_runtime_guidance(data) -> dict:
    """Validate the guidance payload shape, returning it unchanged on success.

    Raises :class:`GuidanceSchemaError` on any structural violation:
      * top-level must be a dict with ``schema_version`` (int), ``guidance_version``
        (str), ``priority`` (str), ``render_mode`` (str), and ``sections`` (list);
      * each section must be a dict with non-empty string ``id``, ``title``, ``body``;
      * section ``id`` and ``title`` must each be unique across the list.

    This is intentionally strict and independently testable so a malformed
    packaged resource is caught by the test suite rather than silently shipping
    empty guidance to production agents.
    """
    if not isinstance(data, dict):
        raise GuidanceSchemaError(f"guidance must be a JSON object, got {type(data).__name__}")
    for key in _GUIDANCE_REQUIRED_TOP_KEYS:
        if key not in data:
            raise GuidanceSchemaError(f"guidance missing required key: {key!r}")
    if not isinstance(data["schema_version"], int) or isinstance(data["schema_version"], bool):
        raise GuidanceSchemaError("guidance.schema_version must be an int")
    for str_key in ("guidance_version", "priority", "render_mode"):
        if not isinstance(data[str_key], str) or not data[str_key]:
            raise GuidanceSchemaError(f"guidance.{str_key} must be a non-empty string")
    sections = data["sections"]
    if not isinstance(sections, list) or not sections:
        raise GuidanceSchemaError("guidance.sections must be a non-empty list")

    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for idx, section in enumerate(sections):
        if not isinstance(section, dict):
            raise GuidanceSchemaError(f"guidance.sections[{idx}] must be an object")
        for field in ("id", "title", "body"):
            value = section.get(field)
            if not isinstance(value, str) or not value:
                raise GuidanceSchemaError(
                    f"guidance.sections[{idx}].{field} must be a non-empty string"
                )
        sid = section["id"]
        stitle = section["title"]
        if sid in seen_ids:
            raise GuidanceSchemaError(f"duplicate guidance section id: {sid!r}")
        if stitle in seen_titles:
            raise GuidanceSchemaError(f"duplicate guidance section title: {stitle!r}")
        seen_ids.add(sid)
        seen_titles.add(stitle)
    return data


def build_runtime_guidance() -> dict:
    """Load, validate, and return the runtime guidance payload.

    Sourced from the skill-style Markdown catalog under
    ``lingtai/prompts/guidance/`` (``INDEX.md`` + one ``<id>.md`` per section),
    assembled by :func:`lingtai_kernel.prompt_catalog.load_guidance_catalog` into
    the same dict shape the kernel has always consumed (``schema_version`` int,
    ordered ``sections`` with stable ``id``/``title``/``body``). The return type
    stays a ``dict`` so it can both feed ``build_meta_guidance`` and back the
    derived ``system/guidance.json`` mirror the TUI/Portal read.

    Cached after first successful load. The assembled payload is schema-checked
    via :func:`validate_runtime_guidance`; on a missing/unreadable catalog, a
    malformed file, or a schema violation the loader returns an empty dict so a
    live agent degrades (no guidance) rather than crashing. Tests should call
    :func:`validate_runtime_guidance` directly to assert the *packaged* catalog
    is well-formed — that path raises, this one does not.
    """
    global _GUIDANCE_CACHE
    if _GUIDANCE_CACHE is not None:
        return _GUIDANCE_CACHE
    try:
        from .prompt_catalog import load_guidance_catalog

        parsed = load_guidance_catalog()
        validate_runtime_guidance(parsed)
        _GUIDANCE_CACHE = parsed
        return parsed
    except Exception:
        return {}



def _format_ratio_percent(value: float | int | str | None) -> str:
    """Return a human-readable percentage for context-pressure reminders."""
    try:
        pct = float(value) * 100
    except Exception:
        return "an unknown amount"
    if pct < 0:
        return "an unknown amount"
    if abs(pct - round(pct)) < 0.05:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def build_molt_context(agent, usage: float) -> str | None:
    """Return `_meta.agent_meta.context.molt` as a natural-language reminder.

    This is current-state guidance (latest tool result only), not a permanent
    event — it belongs in ``agent_meta``. The corrected contract (channel B)
    replaces the old immediate ``usage >= 0.60`` trip-wire with a
    *sustained-pressure* signal: the reminder appears only once context has been
    high (>= the 0.75 reconstruction ratio) for
    ``CONTEXT_PRESSURE_WARN_AFTER_ROUNDS`` consecutive *fresh provider rounds*,
    tracked by ``SessionManager.note_context_pressure_round``. The first two
    high rounds are the window in which the automatic delayed-summarize
    reconstruction (and any agent summarize) is expected to relieve pressure; a
    drop below the threshold resets the streak and clears the reminder.

    Keep this agent-facing value sentence-like. The agent needs a clear reminder
    about why it appeared and what to do, not a tag soup of ``stage`` /
    ``threshold`` / ``action`` fields.
    """
    if "psyche" not in getattr(agent, "_intrinsics", set()):
        return None

    session = getattr(agent, "_session", None)
    if session is None:
        return None
    if not getattr(session, "context_pressure_warning_active", False):
        return None

    try:
        pressure = float(usage)
    except Exception:
        pressure = -1.0

    streak = int(getattr(session, "context_pressure_streak", 0))
    usage_text = _format_ratio_percent(pressure)
    recovery_text = _format_ratio_percent(CONTEXT_PRESSURE_RECOVERY_TARGET)
    return (
        f"Context has stayed high across {streak} consecutive fresh model calls "
        f"(currently {usage_text} of the context window). This is a context-pressure "
        "reminder, not an immediate command: when continuing, batch tool results "
        "you have already digested before summarizing. Repeated summarize calls "
        "while context stays above 75% substantially hurt token efficiency. "
        f"The recovery target is {recovery_text}, but if a batched summarize/"
        "reconstruction pass still leaves context above 75%, stop repeating "
        "summarize, tend durable stores, and molt deliberately. See psyche-manual."
    )


def build_reconstruction_tool_meta(agent) -> dict | None:
    """Build the one-shot delayed-summarize reconstruction event (channel A).

    Permanent per-result evidence, destined for ``_meta.tool_meta.reconstruction``.
    Distinct from :func:`build_molt_context` (channel B, latest-only current
    state): this records a *historical event* — the runtime actually rebuilt the
    provider context around the compacted history when context crossed the 0.75
    reconstruction threshold.

    The adapter supplies the before-context (A) and fixed trigger/recovery
    metadata via ``session.chat.take_pending_reconstruction_event()`` (one-shot:
    the adapter clears it on read). This function fills the after-context (B).

    Call order makes B honest: ``SessionManager.send`` runs ``_track_usage``
    (which sets ``_latest_input_tokens`` from the post-reconstruction provider
    request's reported input) BEFORE the resulting tool calls reach the
    ToolExecutor that stamps this event. So at attach time ``_latest_input_tokens``
    already holds the provider-reported size of the rebuilt context. B therefore
    **prefers** ``_latest_input_tokens / context_window`` (``source:
    provider_input_tokens``) and only falls back to the local compacted-history
    estimate (``source: local_estimate``) when the provider input is unavailable
    (0, e.g. a provider that returned no usage). The delayed-reconstruction
    threshold is itself provider-input based, so this keeps B on the same ruler.

    If B is still at/above the 0.6 recovery target, a natural-language molt
    reminder is attached saying summarize/reconstruction was attempted and
    pressure remains above the recovery target, so consider molt. If B < 0.6,
    the A->B event is returned without a reminder.

    Returns ``None`` when no reconstruction is pending (the common case).
    """
    session = getattr(agent, "_session", None)
    if session is None:
        return None
    chat = getattr(session, "chat", None)
    take = getattr(chat, "take_pending_reconstruction_event", None)
    if not callable(take):
        # Fall back to a session-level hook if the adapter exposes it there.
        take = getattr(session, "take_pending_reconstruction_event", None)
    if not callable(take):
        return None
    raw = take()
    if not raw:
        return None

    # Context window: prefer the value the adapter captured at reconstruction
    # time; fall back to the configured/live window so B can be computed even if
    # the event omitted it.
    ctx_window = 0
    try:
        ctx_window = int(raw.get("context_window") or 0)
    except Exception:
        ctx_window = 0
    if ctx_window <= 0:
        ctx_window = _fallback_context_window(agent)
        if ctx_window <= 0:
            ctx_window = 0

    # After-context (B): prefer the provider-reported input from the
    # post-reconstruction request; fall back to the local compacted-history
    # estimate only when that is unavailable.
    after_tokens = None
    after_usage = -1.0
    after_source = None
    try:
        provider_input = int(getattr(session, "_latest_input_tokens", 0) or 0)
    except Exception:
        provider_input = 0
    if provider_input > 0 and ctx_window > 0:
        after_tokens = provider_input
        after_usage = provider_input / ctx_window
        after_source = "provider_input_tokens"
    else:
        # Local fallback: build_meta owns the (system + history) / window math
        # and the sentinel handling; its usage uses the interface estimate when
        # _latest_input_tokens is 0.
        try:
            local_usage = float(build_meta(agent).get("context", {}).get("usage", -1.0))
        except Exception:
            local_usage = -1.0
        if local_usage >= 0:
            after_usage = local_usage
            after_source = "local_estimate"
            if ctx_window > 0:
                after_tokens = int(round(local_usage * ctx_window))

    event = {
        "type": raw.get("type", "delayed_summarize_reconstruction"),
        "reason": raw.get("reason", "delayed_summarize_reconstruction"),
        "trigger_threshold": raw.get(
            "trigger_threshold", CONTEXT_PRESSURE_RECONSTRUCTION_RATIO
        ),
        "recovery_target": raw.get("recovery_target", CONTEXT_PRESSURE_RECOVERY_TARGET),
        "context_window": raw.get("context_window"),
        "before": raw.get("before", {}),
        "after": {
            "context_tokens": after_tokens,
            "usage": round(after_usage, 5) if after_usage >= 0 else after_usage,
            "source": after_source,
        },
    }

    recovery_target = event["recovery_target"]
    try:
        above_recovery = after_usage >= float(recovery_target)
    except Exception:
        above_recovery = False
    if above_recovery:
        after_text = _format_ratio_percent(after_usage)
        recovery_text = _format_ratio_percent(recovery_target)
        if after_usage >= CONTEXT_PRESSURE_RECONSTRUCTION_RATIO:
            event["molt"] = (
                "The runtime already rebuilt the provider context after summarization, "
                f"but the rebuilt context is still at {after_text} of the context "
                "window, above the 75% high-context threshold. Repeated summarize "
                "calls while context stays above 75% substantially hurt token "
                "efficiency; stop repeating summarize, tend durable stores, and molt "
                "deliberately. See psyche-manual."
            )
        else:
            event["molt"] = (
                "The runtime already rebuilt the provider context after summarization, "
                f"but the rebuilt context is still at {after_text} of the context "
                f"window, at or above the {recovery_text} recovery target. "
                "If more digested tool results can be summarized, do that as one "
                "batch; otherwise tend durable stores and molt deliberately. See "
                "psyche-manual."
            )
    return event


def build_tool_meta_token_usage(agent) -> dict | None:
    """Return a compact provider-round token/cache snapshot for permanent tool_meta.

    ``SessionManager.latest_token_usage_snapshot()`` keeps the full provider-round
    record (scope, api-call index/id, cached/context tokens, estimated flag, ...) for
    internal logging. The object injected into ``_meta.tool_meta.token_usage`` is
    deliberately compact: only the per-result evidence agents need —
    ``input``/``cache_miss``/``cache_rate``/``context_usage``/``window``/``output``/
    ``thinking`` — mapped from the snapshot's long field names. Missing fields are
    dropped rather than invented; existing numeric zero/sentinel values are preserved.
    """
    session = getattr(agent, "_session", None)
    snapshot_fn = getattr(session, "latest_token_usage_snapshot", None)
    if callable(snapshot_fn):
        try:
            snapshot = snapshot_fn()
        except Exception:
            snapshot = None
    else:
        snapshot = getattr(session, "_latest_token_usage_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return None
    # Map full snapshot field names -> compact injected keys. Only emit a key
    # when the source field is present, so the injected object stays robust to
    # partial snapshots without inventing values.
    field_map = (
        ("input", "input_tokens"),
        ("cache_miss", "cache_miss_tokens"),
        ("cache_rate", "cache_rate"),
        ("context_usage", "context_usage"),
        ("window", "context_window"),
        ("output", "output_tokens"),
        ("thinking", "thinking_tokens"),
    )
    compact = {
        out_key: snapshot[src_key]
        for out_key, src_key in field_map
        if src_key in snapshot
    }
    return compact or None

def build_meta(agent) -> dict:
    """Return the current meta-data snapshot for the agent.

    Respects ``agent._config.time_awareness`` / ``timezone_awareness``
    internally; callers never need to special-case those flags.

    Shape::

        {
            "current_time": "<iso>",         # absent when time-blind
            "context": {
                "system_tokens": int,        # sys prompt + tools schema
                "history_tokens": int,       # conversation history
                "usage": float,              # fraction of context window used
                "molt": str,                 # optional natural-language sustained-pressure reminder
            },
            "stamina_left_seconds": float,   # session time remaining; -1 if unstarted
            "token_efficiency": dict,        # optional current_session token economy
            "current_tool_result_chars": dict, # total + top formal tool results >1000 chars
        }

    Sentinel handling: when token decomposition has not yet run, the
    ``context`` sub-object is still emitted but with ``-1`` / ``-1.0``
    values so callers can render "unknown" without ambiguity. Same
    convention for ``stamina_left_seconds`` — ``-1`` means the agent
    hasn't called ``start()`` yet (no uptime anchor).
    """
    meta: dict = {}
    ts = now_iso(agent)
    if ts:
        meta["current_time"] = ts

    # Context-window decomposition. The decomposition needs the agent's
    # system prompt, tool schemas, and context section — all of which
    # are available via the builder callbacks without needing any LLM
    # call to have happened. If the cached values are dirty, refresh them
    # eagerly so the text-input prefix reports real numbers on the very
    # first call of the turn instead of "unknown".
    session = getattr(agent, "_session", None)
    chat_obj = getattr(session, "chat", None) if session is not None else None

    if session is not None and session._token_decomp_dirty:
        try:
            session._update_token_decomposition()
        except Exception:
            pass  # leave dirty; sentinels below

    decomp_ran = session is not None and not session._token_decomp_dirty
    context_tokens_for_efficiency = -1
    context_window_for_efficiency = -1

    if decomp_ran:
        sys_prompt = session._system_prompt_tokens
        tools = session._tools_tokens
        # "history" = in-memory turns (wire chat).
        # Derived from the server-reported wire count when available
        # (_latest_input_tokens - sys_prompt - tools). Before the first
        # LLM call of a session (e.g. right after start() rehydrates the
        # ChatInterface from chat_history.jsonl on cold start or refresh),
        # _latest_input_tokens is still 0, which would report "对话 0"
        # even though the wire chat has been restored. Fall back to the
        # interface's local estimate so the meta-line reflects the
        # restored history from turn 1.
        if session._latest_input_tokens > 0:
            history = max(
                0,
                session._latest_input_tokens - sys_prompt - tools,
            )
        elif chat_obj is not None:
            # interface.estimate_context_tokens() returns system + tools +
            # conversation. Subtract system + tools to isolate the history
            # portion — otherwise history_tokens would double-count them
            # when system_tokens is added back in the usage calculation,
            # diverging from session.get_context_pressure().
            try:
                history = max(
                    0,
                    chat_obj.interface.estimate_context_tokens() - sys_prompt - tools,
                )
            except Exception:
                history = 0
        else:
            history = 0

        system_tokens = sys_prompt + tools
        history_tokens = history

        # context_window comes from the live chat if available; otherwise
        # fall back to the agent's configured limit. On the very first
        # call of a turn (before ensure_session runs) chat_obj is None;
        # we still want real system/context tokens, just usage% may be
        # a sentinel if no limit is configured.
        if chat_obj is not None:
            limit = agent._config.context_limit or chat_obj.context_window()
        else:
            limit = agent._config.context_limit or 0
        context_tokens_for_efficiency = system_tokens + history_tokens
        context_window_for_efficiency = limit if limit > 0 else -1
        usage = (system_tokens + history_tokens) / limit if limit > 0 else -1.0

        meta["context"] = {
            "system_tokens": system_tokens,
            "history_tokens": history_tokens,
            "usage": usage,
        }
    else:
        meta["context"] = {
            "system_tokens": -1,
            "history_tokens": -1,
            "usage": -1.0,
        }

    molt = build_molt_context(agent, meta["context"].get("usage", -1.0))
    if molt:
        meta["context"]["molt"] = molt

    token_efficiency = build_token_efficiency(
        agent,
        context_tokens=context_tokens_for_efficiency,
        context_window=context_window_for_efficiency,
    )
    if token_efficiency:
        meta["token_efficiency"] = token_efficiency

    tool_meta_token_usage = build_tool_meta_token_usage(agent)
    if tool_meta_token_usage:
        meta[TOOL_META_TOKEN_USAGE_PENDING_KEY] = tool_meta_token_usage

    # Stamina — transient runtime resource, can't sit in the cached system
    # prompt. Surface here so the agent sees how much session time it has
    # left on every tool result, alongside context.usage. Sentinel -1 when
    # the agent hasn't started yet (uptime_anchor unset).
    uptime_anchor = getattr(agent, "_uptime_anchor", None)
    stamina = getattr(getattr(agent, "_config", None), "stamina", None)
    if uptime_anchor is not None and stamina is not None:
        uptime = _time.monotonic() - uptime_anchor
        meta["stamina_left_seconds"] = round(max(0.0, stamina - uptime), 1)
    else:
        meta["stamina_left_seconds"] = -1

    meta["current_tool_result_chars"] = current_tool_result_chars(agent)

    comment = dynamic_adapter_comment(agent)
    if comment:
        # Only the slim dynamic view rides on the tail; the static adapter rules
        # are resident in the ``meta_guidance`` system-prompt section.
        meta["adapter_comment"] = slim_adapter_comment_for_tail(comment)

    # Notifications are deliberately NOT included here. Active-state
    # notification payload is a moving single-slot block that lives on the
    # latest tool-call result only — see ``attach_active_notifications``.
    # Putting it in ``build_meta`` would stamp it onto every tool result
    # and accumulate forever in history. The IDLE-state synthesized
    # notification pair and the ACTIVE-state tool-result holder both use the
    # same canonical ``notifications`` payload shape instead.

    return meta


# ---------------------------------------------------------------------------
# Active-state notification stamping — moving canonical payload, latest result only.
# ---------------------------------------------------------------------------


def build_notification_payload(notifications: dict) -> dict:
    """Return active notification payload plus a compact guidance hook.

    Producers own the per-channel envelope under ``notifications``.  Static
    safety/provenance framing lives in resident
    ``meta_guidance.notification_handling``, so the per-result ``_meta`` block
    carries only active sources and channel-owned dynamic payloads.
    """
    sources = [str(source) for source in notifications.keys()]
    payloads: dict = {}
    for source, payload in notifications.items():
        if isinstance(payload, dict):
            payload_for_wire = dict(payload)
        else:
            payload_for_wire = {"data": payload}
        payload_for_wire.pop(NOTIFICATION_GUIDANCE_KEY, None)
        payloads[str(source)] = payload_for_wire

    return {
        NOTIFICATION_GUIDANCE_KEY: {
            "ref": "meta_guidance.notification_handling",
            "sources": sources,
        },
        NOTIFICATIONS_KEY: payloads,
    }


def build_synthetic_tool_meta(
    call_id: str,
    *,
    char_count: int = 0,
    elapsed_ms: int = 0,
) -> dict:
    """Return a minimal synthetic ``tool_meta`` block for the IDLE/ASLEEP pair.

    The synthesized ``notification(action="check")`` pair has no real tool
    execution, so :class:`ToolExecutor._attach_tool_block` never stamps a
    ``_meta.tool_meta`` block on it.  The ``/notification`` history view still
    wants a ``tool_meta`` block to render, so this builds a parallel one carrying
    the same identity fields a real ``tool_meta`` has (id/timestamp/char_count/
    elapsed_ms) plus a ``synthetic: True`` marker that distinguishes it from a
    real tool result's permanent block.
    """
    return {
        "id": call_id or "<unknown>",
        "timestamp": now_iso_plain(),
        "char_count": int(char_count),
        "elapsed_ms": int(elapsed_ms),
        "synthetic": True,
    }


def build_synthetic_meta_envelope(
    agent,
    notification_payload: dict,
    *,
    call_id: str,
) -> dict:
    """Assemble the full ``_meta`` envelope for a synthesized notification pair.

    Produces the same ``_meta`` envelope an ACTIVE tool result persists:

      * ``tool_meta``            — synthetic identity (see
        :func:`build_synthetic_tool_meta`)
      * ``agent_meta``           — current ``build_meta`` snapshot
      * ``guidance``             — lightweight ref to the resident
        ``meta_guidance`` system-prompt section (see
        :func:`build_meta_guidance_ref`)
      * ``notifications`` +
        ``notification_guidance``— from ``notification_payload`` (the dict
        returned by :func:`build_notification_payload`)

    Used only for the durable ``notification_block_injected`` snapshot so the TUI
    ``/notification`` view shows the same ``_meta.*`` blocks for synthesized
    pairs as for ACTIVE tool results.  The live wire body keeps its own
    (notification-only) ``_meta`` — this is a logging-side reconstruction.
    """
    try:
        agent_meta = build_meta(agent)
        agent_meta.pop(TOOL_META_TOKEN_USAGE_PENDING_KEY, None)
    except (AttributeError, TypeError):
        agent_meta = {}

    envelope: dict = {
        TOOL_META_KEY: build_synthetic_tool_meta(call_id),
        AGENT_META_KEY: agent_meta,
        GUIDANCE_KEY: build_meta_guidance_ref(),
    }
    # notifications + notification_guidance from the canonical payload.
    envelope.update(notification_payload)
    return envelope


def _collect_active_notifications_payload(agent) -> dict | None:
    """Return the canonical notification payload for the latest tool result.

    Reads ``.notification/*.json`` via :func:`collect_notifications` and wraps
    it with the same guidance fields used by the synthesized notification pair.
    Returns ``None`` when there are no active channels (or anything goes wrong);
    callers treat ``None`` as "do not stamp."

    """
    try:
        from .notifications import collect_notifications
        from pathlib import Path
        from .notifications import notification_fingerprint

        working_dir = getattr(agent, "_working_dir", None)
        if working_dir is None:
            return None
        notifications = collect_notifications(Path(working_dir))
        if not notifications:
            return None
        return build_notification_payload(notifications)
    except Exception:
        return None


def _last_dict_result(tool_results: list) -> dict | None:
    """Return the dict carried by the latest tool-result block in ``tool_results``.

    Adapter-built ToolResultBlocks store the tool's return value in
    ``.content``. The notification stamp is only meaningful when that content
    is a dict (the JSON shape the agent already parses); other shapes
    (e.g. a string from a tool that returned text) are skipped. Walks
    backward from the tail so the freshest dict result wins even when
    later tools returned non-dicts.
    """
    for block in reversed(tool_results):
        content = getattr(block, "content", None)
        if isinstance(content, dict):
            return content
    return None


# Skeleton content placed in a synthesized pair's result dict once its live
# notification payload has been moved away or cleared.  Keeps the pair in
# history (preserving conversation structure) while making it clear to the
# LLM — and to future introspective code — that the live data is elsewhere.
_NOTIFICATION_SKELETON: dict = {
    "_synthesized": True,
    "_notification_placeholder": True,
    "message": (
        "This was a kernel-synthesized notification(action=check) tool-call pair. "
        "The live notification payload that was here has been moved to a newer tool "
        "result metadata block or cleared."
    ),
}


def skeletonize_notification_holder(agent) -> None:
    """Strip live notification payload from the current live holder and replace
    it with a skeleton placeholder; drop the holder reference.

    The live holder (``agent._notification_live_holder``) may point to:
    * A normal tool-result content dict — strip the canonical notification
      payload keys (``notifications`` and ``notification_guidance``) from the
      ``_meta`` envelope, leaving the other ``_meta`` blocks intact.
    * A synthesized pair's content dict — replace ALL keys with the skeleton
      so the pair stays in history but carries no live payload.

    Synthesized pairs are identified by the presence of ``_synthesized: True``
    in the holder dict.  Normal tool-result dicts never carry that key.

    After this call ``agent._notification_live_holder`` is ``None``.
    Called by:
    * The IDLE/ASLEEP inject path before stamping the new synthesized pair.
    * The ACTIVE path in ``attach_active_notifications`` when moving payload
      to a newer normal tool result (via ``prior_holder`` arg).
    * The notifications-cleared path so no holder carries stale payload.
    """
    holder = getattr(agent, "_notification_live_holder", None)
    if isinstance(holder, dict):
        if holder.get("_synthesized"):
            # Synthesized pair — replace entire content with skeleton.
            holder.clear()
            holder.update(_NOTIFICATION_SKELETON)
        else:
            # Normal tool result dict — strip notification keys from _meta,
            # preserving the other _meta blocks (tool_meta/agent_meta/guidance).
            meta = holder.get(META_ENVELOPE_KEY)
            if isinstance(meta, dict):
                meta.pop(NOTIFICATIONS_KEY, None)
                meta.pop(NOTIFICATION_GUIDANCE_KEY, None)
                if not meta:
                    holder.pop(META_ENVELOPE_KEY, None)
    agent._notification_live_holder = None


# Keep the old name as an alias so external callers (if any) don't break.
# Internal code should prefer skeletonize_notification_holder.
def clear_active_notification_holder(agent) -> None:
    """Legacy alias for :func:`skeletonize_notification_holder`.

    Maintained for backward compatibility.  New code should call
    ``skeletonize_notification_holder`` directly.
    """
    skeletonize_notification_holder(agent)


def attach_active_notifications(
    agent,
    tool_results: list,
    *,
    prior_holder: dict | None = None,
) -> dict | None:
    """Move the canonical notification payload to the latest tool result only.

    Contract:
        * Skeletonize ``prior_holder`` if it exists — for a normal tool
          result dict this strips notification payload keys; for a synthesized
          pair's content dict this replaces all content with the skeleton
          placeholder.  Either way the prior holder is cleared from
          ``agent._notification_live_holder`` before the new holder is
          registered.
        * If active notifications exist, stamp the same ``notifications`` +
          ``notification_guidance`` payload shape used by the synthesized
          notification pair under ``_meta`` on the latest dict-shaped tool
          result, commit the
          current filesystem fingerprint onto ``agent._notification_fp`` so the
          IDLE-path synthesized pair will not later re-deliver the same
          unchanged state, and return that dict as the new holder.
        * If there are no active notifications, no stamping happens,
          ``_notification_fp`` is left untouched, and ``None`` is returned
          (callers should also clear their holder).

    ``post-molt`` is intentionally not special-cased here.  The dangerous race
    is narrower: the ``psyche.molt`` tool call writes ``post-molt.json`` before
    returning, so only that same molt-result batch must skip active stamping.
    Later ACTIVE batches may consume the post-molt notification normally; if no
    later ACTIVE batch happens, the IDLE/ASLEEP sync path wakes the agent.

    ``tool_results`` is the list of ToolResultBlock objects returned from
    ToolExecutor; their ``.content`` is shared by reference with the canonical
    ChatInterface entries that the adapters append, so mutating the dict here
    propagates to history without a separate write.

    Active-state delivery only: the IDLE-path synthesized notification pair is
    built by ``_inject_notification_pair`` directly, but both paths call
    ``build_notification_payload`` so the live notification payload shape stays
    identical. Committing ``_notification_fp`` here is the bridge that prevents
    the same notification state from being delivered twice (once via tool-result
    meta, again via the synthesized pair).
    """
    payload = _collect_active_notifications_payload(agent)
    if not payload:
        # Underlying notification files are gone/empty. The prior holder is
        # now stale, so skeletonize it and report that no live holder remains.
        if prior_holder is not None:
            agent._notification_live_holder = prior_holder
            skeletonize_notification_holder(agent)
        return None

    target = _last_dict_result(tool_results)
    if target is None:
        # Active notifications exist, but this batch has no dict-shaped
        # result to receive the moving payload. Keep the prior live holder
        # (if any) intact and leave _notification_fp uncommitted so the
        # state can still be delivered later via another tool result or
        # the IDLE synthesized-pair path.
        return prior_holder

    # We have both live notifications and a new target. Only now is it safe
    # to strip/skeletonize the previous holder.
    if prior_holder is not None:
        agent._notification_live_holder = prior_holder
        skeletonize_notification_holder(agent)

    # Nest the canonical notification payload under the result's _meta
    # envelope (alongside any tool_meta/agent_meta/guidance blocks).
    _meta_block(target).update(payload)
    # Register this dict as the new live holder.
    agent._notification_live_holder = target

    # Commit the fingerprint so the IDLE-path `_sync_notifications` will
    # see fp == agent._notification_fp and skip the synthesized pair for
    # this same unchanged state.  Best-effort: a fingerprint failure must
    # not break the (already successful) stamping.
    try:
        from pathlib import Path
        from .notifications import notification_fingerprint

        working_dir = getattr(agent, "_working_dir", None)
        if working_dir is not None and hasattr(agent, "_notification_fp"):
            agent._notification_fp = notification_fingerprint(Path(working_dir))
    except Exception:
        pass

    return target



def render_meta(agent, meta: dict) -> str:
    """Render the meta dict as the line prepended to text input.

    Returns '' when the meta dict is empty — callers should treat '' as
    "no prefix" and skip concatenation.

    Composes the existing ``system.current_time`` template plus a context
    fragment via ``system.context_breakdown`` (or ``system.context_unknown``
    when the session has not yet computed its token decomposition).
    """
    if not meta:
        return ""

    time_val = meta.get("current_time", "")
    ctx_val = _render_context_fragment(agent, meta)

    if time_val == "" and ctx_val == "":
        return ""

    return _t(
        agent._config.language,
        "system.current_time",
        time=time_val,
        ctx=ctx_val,
    )


def _render_context_fragment(agent, meta: dict) -> str:
    """Render the context sub-fragment for the text-input prefix.

    Returns:
        - '' if `context` is not present in ``meta``
        - the locale-specific "unknown" word when the sentinel (-1) is seen
        - the composed "{pct} (sys {sys} + ctx {ctx})" fragment otherwise
    """
    ctx = meta.get("context")
    if not ctx:
        return ""
    usage = ctx.get("usage", -1.0)
    if usage < 0:
        return _t(agent._config.language, "system.context_unknown")
    return _t(
        agent._config.language,
        "system.context_breakdown",
        pct=f"{usage * 100:.1f}%",
        sys=ctx.get("system_tokens", 0),
        ctx=ctx.get("history_tokens", 0),
    )


def stamp_meta(result: dict, meta: dict, elapsed_ms: int) -> dict:
    """Record per-tool runtime ``meta`` on the result for the boundary holder.

    ``_meta.agent_meta`` / ``_meta.guidance`` are **latest-only** blocks: only
    the freshest provider-visible tool result carries them.  Stamping them on
    every result (the old behaviour) would leave stale snapshots in history, so
    this function records the per-tool ``meta`` snapshot and measured
    ``elapsed_ms`` under a transient ``_runtime_pending`` key, which
    :func:`attach_active_runtime` consumes at the tool-batch boundary
    (analogous to the notification holder) and then deletes.

    When ``meta`` is empty nothing is recorded — matching the pre-existing
    time-blind behaviour where no timing signal appears.

    ``_runtime_pending`` is internal scaffolding and never reaches the wire: the
    boundary holder strips it from every result it inspects.  The
    ``_meta.tool_meta`` block written by ``ToolExecutor._attach_tool_block`` is
    separate and permanent; ``stamp_meta`` does not touch it.
    """
    if not meta:
        return result
    pending: dict = dict(meta)
    pending["elapsed_ms"] = elapsed_ms
    result["_runtime_pending"] = pending
    return result


# ---------------------------------------------------------------------------
# agent_meta / guidance blocks — latest-result-only moving holder under _meta,
# mirrors the notification payload pattern in ``attach_active_notifications``.
# ---------------------------------------------------------------------------


def _strip_runtime_pending(tool_results: list) -> None:
    """Remove the transient ``_runtime_pending`` scaffolding from every result.

    ``stamp_meta`` records a per-tool ``_runtime_pending`` snapshot on each
    dict result; only the latest result's snapshot is promoted into the real
    ``_meta.agent_meta`` / ``_meta.guidance`` blocks.  This clears the
    scaffolding from the rest so it never reaches the wire or lingers in
    history.
    """
    for block in tool_results:
        content = getattr(block, "content", None)
        if isinstance(content, dict):
            content.pop("_runtime_pending", None)


def _strip_agent_meta_and_guidance(holder: dict) -> None:
    """Strip the latest-only ``agent_meta``/``guidance`` blocks from a holder.

    Notification keys and the permanent ``tool_meta`` are left intact; the
    ``_meta`` envelope is dropped entirely only if it becomes empty.
    """
    meta = holder.get(META_ENVELOPE_KEY)
    if isinstance(meta, dict):
        meta.pop(AGENT_META_KEY, None)
        meta.pop(GUIDANCE_KEY, None)
        if not meta:
            holder.pop(META_ENVELOPE_KEY, None)


def attach_active_runtime(
    agent,
    tool_results: list,
    *,
    prior_holder: dict | None = None,
) -> dict | None:
    """Move the live ``agent_meta``/``guidance`` blocks to the latest result only.

    Mirrors :func:`attach_active_notifications`:

      * Strip ``_meta.agent_meta`` / ``_meta.guidance`` from ``prior_holder``
        (the previous live holder) so stale snapshots do not accumulate in
        history.  The prior holder keeps its permanent ``_meta.tool_meta`` and
        any notification keys.
      * Promote the latest dict-shaped result's per-tool ``_runtime_pending``
        snapshot (recorded by :func:`stamp_meta`) into ``_meta.agent_meta``
        (kernel runtime state including ``token_efficiency``, plus
        ``elapsed_ms`` + ``active_turn_tool_calls``)
        and ``_meta.guidance`` (a lightweight ref to resident ``meta_guidance``,
        whose body is built from the guidance catalog, generated ``meta_readme``,
        and static adapter rules).
      * Strip the transient ``_runtime_pending`` scaffolding from *all* results.
      * Return the new holder dict (or ``None`` when no live runtime applies).

    ``active_turn_tool_calls`` is read from the agent's executor guard so the
    counter lives under ``_meta.agent_meta`` (latest-only) rather than being
    repeated on every result.  ``elapsed_ms`` comes from the latest result's
    own ``_runtime_pending`` snapshot.

    No live runtime is produced (and the prior holder is still cleared) when the
    batch has no dict-shaped target or the latest target carried no pending
    snapshot (e.g. a time-blind agent whose ``meta`` is empty).
    """
    # The prior holder always loses its latest-only blocks — at most one live
    # holder carries agent_meta/guidance.
    if prior_holder is not None:
        _strip_agent_meta_and_guidance(prior_holder)

    target = _last_dict_result(tool_results)
    pending = target.pop("_runtime_pending", None) if target is not None else None

    # Clear scaffolding from every other result regardless of outcome.
    _strip_runtime_pending(tool_results)

    if target is None or not isinstance(pending, dict) or not pending:
        return None

    agent_meta: dict = dict(pending)
    agent_meta.pop(TOOL_META_TOKEN_USAGE_PENDING_KEY, None)
    calls = _active_turn_tool_calls(agent)
    if calls is not None:
        agent_meta["active_turn_tool_calls"] = calls
    agent_meta["current_tool_result_chars"] = current_tool_result_chars(
        agent, extra_results=tool_results
    )
    # The adapter_comment carries both dynamic per-turn scalars and static
    # rule-like prose plus a long cache ledger.  The static content is resident
    # in the ``meta_guidance`` system-prompt section, so the tail keeps only the
    # slim dynamic view plus a ref back to that section.
    comment = dynamic_adapter_comment(agent)
    if comment:
        agent_meta["adapter_comment"] = slim_adapter_comment_for_tail(comment)

    # Guidance is resident in the ``meta_guidance`` system-prompt section; the
    # tail only carries a lightweight ref/hook rather than re-stamping the whole
    # ordered appendix (sections + the ``_meta`` readme) on every result.
    meta = _meta_block(target)
    meta[AGENT_META_KEY] = agent_meta
    meta[GUIDANCE_KEY] = build_meta_guidance_ref()
    return target


def _active_turn_tool_calls(agent) -> int | None:
    """Best-effort read of the ACTIVE-turn tool-call counter from the guard.

    Returns ``None`` (counter omitted) if the agent has no executor/guard or
    the attribute is unavailable, so a missing counter never breaks stamping.
    """
    try:
        guard = getattr(getattr(agent, "_executor", None), "guard", None)
        total = getattr(guard, "total_calls", None)
        return int(total) if total is not None else None
    except Exception:
        return None


def _non_negative_int(value, *, default: int = 0) -> int:
    """Best-effort conversion for agent-facing token counters."""
    try:
        if isinstance(value, bool):
            raise TypeError
        ivalue = int(value)
    except Exception:
        return default
    return ivalue if ivalue >= 0 else default


def _fallback_context_window(agent) -> int:
    """Return a best-effort context window for token_efficiency."""
    try:
        config_limit = int(getattr(getattr(agent, "_config", None), "context_limit", 0) or 0)
    except Exception:
        config_limit = 0
    if config_limit > 0:
        return config_limit
    try:
        session = getattr(agent, "_session", None)
        chat = getattr(session, "chat", None)
        if chat is None:
            chat = getattr(session, "_chat", None)
        window_fn = getattr(chat, "context_window", None)
        if callable(window_fn):
            window = _non_negative_int(window_fn(), default=-1)
            return window if window > 0 else -1
    except Exception:
        pass
    return -1


def build_token_efficiency(
    agent,
    *,
    context_tokens: int = -1,
    context_window: int = -1,
) -> dict | None:
    """Return a compact current-session token economy snapshot for agent_meta.

    The snapshot intentionally mirrors the TUI-facing token counters the agent
    can act on now.  It reads the live SessionManager through
    ``agent.get_token_usage()`` and does not reread durable ledgers.
    """
    usage_fn = getattr(agent, "get_token_usage", None)
    if not callable(usage_fn):
        return None
    try:
        usage = usage_fn()
    except Exception:
        return None
    if not isinstance(usage, Mapping):
        return None

    api_calls = _non_negative_int(usage.get("api_calls"))
    input_tokens = _non_negative_int(usage.get("input_tokens"))
    cached_tokens = _non_negative_int(usage.get("cached_tokens"))

    context_tokens_value = _non_negative_int(context_tokens, default=-1)
    if context_tokens_value < 0:
        context_tokens_value = _non_negative_int(usage.get("ctx_total_tokens"), default=-1)
    context_window_value = _non_negative_int(context_window, default=-1)
    if context_window_value <= 0:
        context_window_value = _fallback_context_window(agent)

    avg_input = int(round(input_tokens / api_calls)) if api_calls > 0 else 0
    cache_rate = (
        round(min(cached_tokens / input_tokens, 1.0), 5)
        if input_tokens > 0
        else 0.0
    )

    return {
        "scope": "current_session",
        "api_calls": api_calls,
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_rate": cache_rate,
        "avg_input_tokens_per_api_call": avg_input,
        "context_tokens": context_tokens_value,
        "context_window": context_window_value,
        "guidance_ref": "meta_guidance.token_efficiency",
    }
