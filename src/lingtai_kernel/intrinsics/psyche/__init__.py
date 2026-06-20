"""Psyche intrinsic — bare essentials of agent self.

Objects:
    pad — edit/load system/pad.md (agent's working notes), append pinned files
    context — molt (shed context, keep a briefing)
    name — set true name (once), set/clear nickname
    lingtai — update/load system/lingtai.md (self-authored identity → `character` section)

Sub-modules:
    _snapshots.py — Snapshot and summary persistence for the molt machinery.
    _pad.py       — Pad CRUD and append-file management.
    _lingtai.py   — Lingtai identity/character management.
    _molt.py      — Context molt core, name handlers, system-initiated molt.

Internal:
    boot — boot-time hook: load lingtai + pad into prompt, register post-molt
        reload. Called from base_agent.__init__ after intrinsics are wired.
"""
from __future__ import annotations

# --- Re-exports from sub-modules for backward compatibility ---

# Snapshots (used by consultation, inquiry, etc.)
from ._snapshots import SNAPSHOT_SCHEMA_VERSION, _write_molt_snapshot, _write_molt_summary  # noqa: F401

# Pad (used by boot, and cross-referenced by lingtai/append)
from ._pad import _pad_edit, _pad_load, _pad_append  # noqa: F401

# Lingtai (used by boot, and cross-referenced by pad)
from ._lingtai import _lingtai_update, _lingtai_load  # noqa: F401

# Molt (the public surface)
from ._molt import _context_molt, _name_set, _name_nickname, context_forget  # noqa: F401


# ---------------------------------------------------------------------------
# Schema / description
# ---------------------------------------------------------------------------


def get_description(lang: str = "en") -> str:
    from ...i18n import t
    return t(lang, "psyche.description")


def get_schema(lang: str = "en") -> dict:
    from ...i18n import t
    return {
        "type": "object",
        "properties": {
            "object": {
                "type": "string",
                "enum": ["pad", "context", "name", "lingtai"],
                "description": t(lang, "psyche.object_description"),
            },
            "action": {
                "type": "string",
                "description": t(lang, "psyche.action_description"),
            },
            "content": {
                "type": "string",
                "description": t(lang, "psyche.content_description"),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "psyche.files_description"),
            },
            "summary": {
                "type": "string",
                "description": t(lang, "psyche.summary_description"),
            },
            "keep_tool_calls": {
                "type": "array",
                "items": {"type": "string"},
                "description": t(lang, "psyche.keep_tool_calls_description"),
            },
            "keep_last": {
                "type": "integer",
                "description": t(lang, "psyche.keep_last_description"),
            },
            "session_journal_path": {
                "type": "string",
                "description": t(lang, "psyche.session_journal_path_description"),
            },
        },
        "required": ["object", "action"],
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_VALID_ACTIONS: dict[str, set[str]] = {
    "lingtai": {"update", "load"},
    "pad": {"edit", "load", "append"},
    "context": {"molt"},
    "name": {"set", "nickname"},
}

# Explicit dispatch table — replaces the old globals().get(method_name) pattern
# so it works across sub-modules.
_DISPATCH: dict[str, dict[str, object]] = {
    "lingtai": {"update": _lingtai_update, "load": _lingtai_load},
    "pad": {"edit": _pad_edit, "load": _pad_load, "append": _pad_append},
    "context": {"molt": _context_molt},
    "name": {"set": _name_set, "nickname": _name_nickname},
}


def handle(agent, args: dict) -> dict:
    """Handle psyche tool — dispatch to (object, action) handler."""
    obj = args.get("object", "")
    action = args.get("action", "")

    valid = _VALID_ACTIONS.get(obj)
    if valid is None:
        return {
            "error": f"Unknown object: {obj!r}. "
                     f"Must be one of: {', '.join(sorted(_VALID_ACTIONS))}."
        }
    if action not in valid:
        return {
            "error": f"Invalid action {action!r} for {obj}. "
                     f"Valid actions: {', '.join(sorted(valid))}."
        }

    handler = _DISPATCH.get(obj, {}).get(action)
    if handler is None:
        return {"error": f"Internal: handler for ({obj}, {action}) not found."}
    return handler(agent, args)


# ---------------------------------------------------------------------------
# Boot hook
# ---------------------------------------------------------------------------


def boot(agent) -> None:
    """Boot-time hook: load lingtai + pad into the prompt, register post-molt
    reload. Called from base_agent.__init__ after intrinsics are wired."""
    _pad_load(agent, {})
    _lingtai_load(agent, {})
    if not hasattr(agent, "_post_molt_hooks"):
        agent._post_molt_hooks = []
    agent._post_molt_hooks.append(
        lambda: (_lingtai_load(agent, {}), _pad_load(agent, {}))
    )
