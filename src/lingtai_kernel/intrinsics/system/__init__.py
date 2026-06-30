"""System intrinsic — runtime, lifecycle, and synchronization.

Actions (voluntary, agent-callable):
    refresh   — stop, reload MCP servers and config from working dir, restart
    sleep     — self only, go to sleep (no karma needed)
    lull      — put another agent to sleep (requires karma)
    suspend   — suspend another agent (requires karma)
    cpr       — resuscitate a suspended agent (requires karma)
    interrupt — interrupt a running agent's current turn (requires karma)
    clear     — force a full molt on another agent (requires karma)
    nirvana   — permanently destroy an agent's working directory (requires nirvana)
    presets   — list available presets in the agent's library
    summarize — record an agent-authored compact replacement for a prior
                tool-result block in runtime history; a successful summarize of a
                ``large_tool_result`` tool_call_id auto-clears its reminder.

Notification verbs (``check``/``dismiss_channel``/``dismiss_event``/
``dismiss_ref``) are **not** on ``system`` — they live exclusively on the
standalone ``notification`` tool.  ``system`` no longer exposes any
notification or dismiss compatibility alias.  The kernel still *synthesizes*
a notification tool-call pair on the agent's behalf when changes arrive during
IDLE/ASLEEP states (delivery plumbing, not an agent-callable action); the agent
reads/clears via the ``notification`` tool.

Identity and runtime state surface via other channels:
    - identity prompt section — every turn, cached prefix
    - per-result `_meta.tool_meta` / latest `_meta.agent_meta` on tool results
    - `.status.json` — written by the kernel; read with read({".status.json"})
      when the agent wants the deep dive

Sub-modules:
    preset.py        — _preset_ref_in(), _check_context_fits(), _refresh(), _presets().
    karma.py         — _KARMA_ACTIONS, _NIRVANA_ACTIONS, _check_karma_gate(),
                       _sleep(), _lull(), _suspend(), _cpr(), _interrupt(),
                       _clear(), _nirvana().
    summarize.py     — _summarize() function, SUMMARIZE_MARKER.
    schema.py        — get_description(), get_schema().
"""
from __future__ import annotations

# --- Re-exports from sub-modules for backward compatibility ---

# Schema (tool registration)
from .schema import get_description, get_schema  # noqa: F401

# Summarize — agent-authored context summarization
from .summarize import _summarize, SUMMARIZE_MARKER  # noqa: F401

# Notification submission — the canonical helper any producer (intrinsic
# or in-process MCP) can call to surface a notification to the agent.
# Re-exported here for back-compat: ``system`` historically owned the
# producer-facing publish entry point, and many in-process producers still
# import ``publish_notification`` / ``clear_notification`` from here.  The
# agent-facing notification *verbs* (check/dismiss) now live exclusively on the
# standalone ``notification`` tool; ``system`` exposes none of them.  The
# functions live in ``lingtai_kernel.notifications`` (single source of truth,
# accessible to non-intrinsic call sites and external producers that import the
# module directly).
from ...notifications import (  # noqa: F401
    submit as publish_notification,
    clear as clear_notification,
)

# Preset
from .preset import _preset_ref_in, _check_context_fits, _refresh, _presets  # noqa: F401

# Karma
from .karma import (  # noqa: F401
    _KARMA_ACTIONS,
    _NIRVANA_ACTIONS,
    _check_karma_gate,
    _sleep,
    _lull,
    _suspend,
    _cpr,
    _interrupt,
    _clear,
    _nirvana,
)


# ---------------------------------------------------------------------------
# Module-level intrinsic protocol — handle()
# ---------------------------------------------------------------------------


def handle(agent, args: dict) -> dict:
    """Handle system tool — runtime, lifecycle, synchronization.

    Notification verbs (``check``/``dismiss_*``) and the old ``notification``/
    ``dismiss`` compatibility aliases are **not** handled here: they live
    exclusively on the standalone ``notification`` tool.  ``summarize`` remains
    a system action (it is a context-hygiene operation, not a notification
    verb), and a successful summarize still auto-clears the matching
    ``large_tool_result`` reminder internally.
    """
    action = args.get("action")
    handler = {
        "refresh": _refresh,
        "sleep": _sleep,
        "lull": _lull,
        "suspend": _suspend,
        "cpr": _cpr,
        "interrupt": _interrupt,
        "clear": _clear,
        "nirvana": _nirvana,
        "presets": _presets,
        "summarize": _summarize,
    }.get(action)
    if handler is None:
        return {"status": "error", "message": f"Unknown system action: {action}"}
    return handler(agent, args)
