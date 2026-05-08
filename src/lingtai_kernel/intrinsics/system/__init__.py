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
    dismiss   — dismiss one or more system notifications by notif_id

Action (kernel-synthesized by default — also callable voluntarily by the agent):
    notification — returns the current state of all notification channels
                   by reading ``.notification/*.json``. The kernel also
                   synthesizes this call on the agent's behalf when changes
                   arrive during IDLE/ASLEEP states.

Identity, runtime, and stamina state surface via other channels:
    - identity prompt section — every turn, cached prefix
    - meta line `context.{system,history}_tokens` + `stamina_left_seconds`
      on every tool result and text input
    - `.status.json` — written by the kernel; read with read({".status.json"})
      when the agent wants the deep dive

Sub-modules:
    preset.py        — _preset_ref_in(), _check_context_fits(), _refresh(), _presets().
    karma.py         — _KARMA_ACTIONS, _NIRVANA_ACTIONS, _check_karma_gate(),
                       _sleep(), _lull(), _suspend(), _cpr(), _interrupt(),
                       _clear(), _nirvana().
    notification.py  — _dismiss() function.
    schema.py        — get_description(), get_schema().
"""
from __future__ import annotations

# --- Re-exports from sub-modules for backward compatibility ---

# Schema (tool registration)
from .schema import get_description, get_schema  # noqa: F401

# Notification (dismiss — cross-module import from email/manager.py)
from .notification import _dismiss  # noqa: F401

# Notification submission — the canonical helper any producer (intrinsic
# or in-process MCP) can call to surface a notification to the agent.
# Re-exported here because ``system`` owns the notification surface
# conceptually: every producer's file is aggregated into a single
# ``system(action="notification")`` wire pair by the kernel sync.  The
# function lives in ``lingtai_kernel.notifications`` (single source of
# truth, accessible to non-intrinsic call sites and external producers
# that import the module directly).
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
    """Handle system tool — runtime, lifecycle, synchronization."""
    action = args.get("action")
    # Voluntary `notification` query: returns the current state of all
    # notification channels by reading `.notification/*.json`.  The
    # kernel may also synthesize this call on the agent's behalf when a
    # change arrives during IDLE/ASLEEP — in either case the agent sees
    # the same shape: ``{"_synthesized": <bool>, "notifications": {...}}``
    # for kernel-injected calls, or the bare collection dict for
    # voluntary calls.  See discussions/notification-filesystem-redesign.md.
    if action == "notification":
        from ...notifications import collect_notifications
        return collect_notifications(agent._working_dir)
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
        "dismiss": _dismiss,
    }.get(action)
    if handler is None:
        return {"status": "error", "message": f"Unknown system action: {action}"}
    return handler(agent, args)
