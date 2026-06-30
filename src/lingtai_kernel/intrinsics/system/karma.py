"""Karma-gated lifecycle actions — sleep, lull, suspend, cpr, interrupt, clear, nirvana."""
from __future__ import annotations

from ...handshake import resolve_address


# ---------------------------------------------------------------------------
# Karma / Nirvana gate mapping
# ---------------------------------------------------------------------------

_KARMA_ACTIONS = {"interrupt", "lull", "suspend", "cpr", "clear"}
_NIRVANA_ACTIONS = {"nirvana"}


def _check_karma_gate(agent, action: str, args: dict) -> dict | None:
    from ...handshake import is_agent
    if action in _KARMA_ACTIONS and not agent._admin.get("karma"):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.karma=True)"}
    if action in _NIRVANA_ACTIONS and not (agent._admin.get("karma") and agent._admin.get("nirvana")):
        return {"error": True, "message": f"Not authorized for {action} (requires admin.karma=True AND admin.nirvana=True)"}
    address = args.get("address")
    if not address:
        return {"error": True, "message": f"{action} requires an address"}
    # Resolve relative address to absolute path
    base_dir = agent._working_dir.parent
    resolved = resolve_address(address, base_dir)
    if str(resolved) == str(agent._working_dir):
        return {"error": True, "message": f"Cannot {action} self"}
    if not is_agent(resolved):
        return {"error": True, "message": f"No agent at {address}"}
    # Store resolved path for downstream use
    args["_resolved_address"] = resolved
    return None


def _sleep(agent, args: dict) -> dict:
    """Self-sleep — any agent can put itself to sleep, no karma needed.

    Sleep is idempotent against the notification queue: if `.notification/`
    has an unprocessed payload on disk (fingerprint diverges from the
    agent's last-committed fingerprint), we refuse the transition rather
    than going ASLEEP with mail already waiting. This handles the race
    where mail arrives during the same ACTIVE turn that decides to sleep —
    the LLM's "no unread mail, sleep" decision was made against the
    pre-call snapshot, but by the time the tool fires the queue has
    changed. Without this guard the first email looks dropped to the
    human (only a SECOND email wakes the agent). See lingtai-kernel#112.

    `force=True` overrides the guard — escape hatch for the rare case
    where the agent explicitly wants to sleep anyway.
    """
    from ...i18n import t
    from ...state import AgentState
    from ...notifications import notification_fingerprint

    reason = args.get("reason", "")
    force = bool(args.get("force", False))

    pending_fp = notification_fingerprint(agent._working_dir)
    has_pending = pending_fp != agent._notification_fp

    if has_pending and not force:
        agent._log(
            "sleep_refused_pending_notifications",
            reason=reason,
            pending_fp=list(pending_fp),
            committed_fp=list(agent._notification_fp or ()),
        )
        return {
            "status": "ok",
            "message": t(
                agent._config.language,
                "system_tool.sleep_refused_pending_notifications",
            ),
        }

    if has_pending and force:
        agent._log(
            "sleep_forced_with_pending_notifications",
            reason=reason,
            pending_fp=list(pending_fp),
        )

    agent._log("self_sleep", reason=reason)
    agent._set_state(AgentState.ASLEEP, reason="self-sleep")
    agent._asleep.set()
    agent._cancel_event.set()
    return {
        "status": "ok",
        "message": t(agent._config.language, "system_tool.sleep_message"),
    }


def _lull(agent, args: dict) -> dict:
    """Lull another agent to sleep — karma-gated."""
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "lull", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running — already asleep?"}
    (resolved / ".sleep").write_text("")
    agent._log("karma_lull", target=address)
    return {"status": "asleep", "address": address}


def _suspend(agent, args: dict) -> dict:
    """Suspend another agent — karma-gated."""
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "suspend", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running — already suspended?"}
    (resolved / ".suspend").write_text("")
    agent._log("karma_suspend", target=address)
    return {"status": "suspended", "address": address}


def _cpr(agent, args: dict) -> dict:
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "cpr", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is already running"}
    resuscitated = agent._cpr_agent(str(resolved))
    if resuscitated is None:
        return {"error": True, "message": "CPR not supported — no _cpr_agent handler"}
    if isinstance(resuscitated, dict) and resuscitated.get("error"):
        agent._log("karma_cpr_failed", target=address, message=resuscitated.get("message"))
        return resuscitated
    if resuscitated is False:
        agent._log("karma_cpr_failed", target=address, message="_cpr_agent returned False")
        return {"error": True, "message": "CPR failed"}
    agent._log("karma_cpr", target=address)
    return {"status": "resuscitated", "address": address}


def _interrupt(agent, args: dict) -> dict:
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "interrupt", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running"}
    (resolved / ".interrupt").write_text("")
    agent._log("karma_interrupt", target=address)
    return {"status": "interrupted", "address": address}


def _clear(agent, args: dict) -> dict:
    """Force a full molt on another agent — karma-gated.

    Writes a .clear signal; the target's heartbeat loop picks it up and
    invokes psyche.context_forget, which archives chat history and injects
    a system-authored recovery summary pointing at pad/knowledge/inbox.
    """
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "clear", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if not is_alive(resolved):
        return {"error": True, "message": f"Agent at {address} is not running"}
    # Content of .clear becomes the `source` tag in the recovery summary.
    # Default to the calling agent's name so targets can see who forced it.
    source = (args.get("reason") or "").strip() or agent.agent_name or "admin"
    (resolved / ".clear").write_text(source)
    agent._log("karma_clear", target=address, source=source)
    return {"status": "cleared", "address": address, "source": source}


def _nirvana(agent, args: dict) -> dict:
    import shutil
    from ...handshake import is_alive
    err = _check_karma_gate(agent, "nirvana", args)
    if err:
        return err
    address = args["address"]
    resolved = args["_resolved_address"]
    if is_alive(resolved):
        (resolved / ".sleep").write_text("")
        import time as _time
        deadline = _time.time() + 10.0
        while _time.time() < deadline:
            if not is_alive(resolved):
                break
            _time.sleep(0.5)
        else:
            if is_alive(resolved):
                return {"error": True, "message": f"Agent at {address} did not sleep within timeout"}
    shutil.rmtree(resolved)
    agent._log("karma_nirvana", target=address)
    return {"status": "nirvana", "address": address}
