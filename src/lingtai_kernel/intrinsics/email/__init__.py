"""Email intrinsic — filesystem-based mailbox with search and contacts.

Re-exports the full public surface of the former monolithic email.py so all
existing import sites continue to work unchanged.

Sub-modules:
    primitives.py  — Mailbox I/O, ID generation, read tracking, delivery, display.
    schema.py      — Tool registration (get_description, get_schema).
    manager.py     — EmailManager class (the core filesystem manager).

Storage layout:
    working_dir/mailbox/inbox/{uuid}/message.json     — received
    working_dir/mailbox/sent/{uuid}/message.json      — sent
    working_dir/mailbox/archive/{uuid}/message.json   — archived from inbox
    working_dir/mailbox/read.json                     — read tracking
    working_dir/mailbox/contacts.json                 — contact book

Internal:
    boot(agent) — instantiates EmailManager on agent._email_manager.
        Called from base_agent during agent construction.
    handle(agent, args) — module-level dispatcher; delegates to the manager.

Note: recurring/scheduled sends were removed in favor of cron. The email
tool is now request/response only.
"""
from __future__ import annotations


from ...notifications import register_generic_dismiss_guard

register_generic_dismiss_guard(
    "email",
    "email(action='dismiss', email_id=[...]) or email(action='read', email_id=[...])",
)

# --- Re-exports from sub-modules for backward compatibility ---

# Primitives (mailbox I/O, helpers)
from .primitives import (  # noqa: F401
    _coerce_address_list,
    _email_time,
    _inbox_dir,
    _is_self_send,
    _list_inbox,
    _load_message,
    _mailbox_dir,
    _mailman,
    _mark_read,
    _message_summary,
    _move_to_sent,
    _new_mailbox_id,
    _outbox_dir,
    _persist_to_inbox,
    _persist_to_outbox,
    _preview,
    _read_ids,
    _read_ids_path,
    _save_read_ids,
    _sent_dir,
    _summary_to_list,
    mode_field,
)

# Schema (tool registration)
from .schema import get_description, get_schema  # noqa: F401

# Manager
from .manager import EmailManager  # noqa: F401


# ---------------------------------------------------------------------------
# Module-level intrinsic protocol — handle() + boot()
# ---------------------------------------------------------------------------


def handle(agent, args: dict) -> dict:
    """Module-level dispatcher — delegates to the agent's EmailManager.

    Boot must have run first to instantiate the manager. If not (e.g. someone
    calls handle() before boot() in a test harness), return a clear error.
    """
    action = args.get("action")
    if action == "unread":
        return {
            "status": "error",
            "message": (
                "email(action='unread', ...) is reserved for kernel-"
                "synthesized unread-mail digests and cannot be invoked "
                "directly. Use email(action='check') to view your inbox."
            ),
        }
    mgr = getattr(agent, "_email_manager", None)
    if mgr is None:
        return {"error": "Internal: email manager not initialized. boot() was not called."}
    return mgr.handle(args)


def boot(agent) -> None:
    """Boot-time hook: instantiate manager and wire it onto the agent.

    The intrinsic registration (add_tool with schema/handler/description) is
    done by _wire_intrinsics + ALL_INTRINSICS — this hook does the runtime
    setup that the registry can't: create the manager and wire it into the
    agent so module-level handle() can find it.

    Idempotent on re-boot (the molt / refresh / cpr path goes through
    ``_setup_from_init`` which re-runs ``boot``): simply rebinds a fresh
    manager.
    """
    mgr = EmailManager(agent)
    agent._email_manager = mgr
    agent._mailbox_name = "email box"
    agent._mailbox_tool = "email"
