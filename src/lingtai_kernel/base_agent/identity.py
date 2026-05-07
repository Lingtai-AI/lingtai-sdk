"""Identity — naming, manifest building, and status reporting.

Everything the agent knows about itself: how it presents, how it
serializes to disk, and how it reports runtime status.
"""
from __future__ import annotations

import time


def _set_name(agent, name: str) -> None:
    """Set the agent's true name (真名). Immutable once set."""
    if not name:
        raise ValueError("Agent name cannot be empty.")
    if agent.agent_name is not None:
        raise RuntimeError(
            f"True name already set ({agent.agent_name!r}). "
            f"True names are immutable. Use set_nickname() instead."
        )
    agent.agent_name = name
    _update_identity(agent)


def _set_nickname(agent, nickname: str) -> None:
    """Set or change the agent's nickname (别名). Mutable."""
    agent.nickname = nickname or None
    _update_identity(agent)


def _update_identity(agent) -> None:
    """Write manifest and update identity section in system prompt.

    The system-prompt section excludes runtime-transient fields (`state`)
    to preserve prompt-cache stability. The disk manifest keeps them.
    """
    from . import _build_identity_section

    manifest_data = _build_manifest(agent)
    agent._workdir.write_manifest(manifest_data)
    agent._prompt_manager.write_section(
        "identity",
        _build_identity_section(
            manifest_data,
            mailbox_name=getattr(agent, "_mailbox_name", None),
        ),
        protected=True,
    )


def _build_manifest(agent) -> dict:
    """Build the manifest dict for .agent.json.

    Subclasses override to add fields (e.g. capabilities).
    Contains everything the agent knows about itself.
    address is always the current working_dir (hot-refreshed on every write).
    Must not depend on _session or _chat — called during __init__.
    """
    data = {
        "agent_id": agent._agent_id,
        "agent_name": agent.agent_name,
        "nickname": agent.nickname,
        "address": agent._working_dir.name,
        "created_at": agent._created_at,
        "started_at": agent._started_at,
        "admin": agent._admin,
        "language": agent._config.language,
        "stamina": agent._config.stamina,
        "state": agent._state.value,
        "soul_delay": agent._soul_delay,
        "soul_voice": getattr(agent._config, "soul_voice", "inner"),
        "molt_count": agent._molt_count,
    }
    # Custom voice prompt is only meaningful when voice == "custom".
    # Surface it so /kanban (and any consumer reading .agent.json)
    # can show the active prompt without calling soul(action='voice').
    if data["soul_voice"] == "custom":
        data["soul_voice_prompt"] = getattr(agent._config, "soul_voice_prompt", "") or ""
    # Subconscious config — surface enabled state + TTL in the manifest
    # so the TUI / portal can show it without calling soul(action='config').
    sub_enabled = getattr(agent._config, "subconscious_enabled", False)
    data["subconscious_enabled"] = sub_enabled
    if sub_enabled:
        data["subconscious_ttl_seconds"] = getattr(agent._config, "subconscious_ttl_seconds", 1800.0)
    if agent._mail_service is not None and agent._mail_service.address:
        data["address"] = agent._mail_service.address
    return data


def _status(agent) -> dict:
    """Return live runtime status — written to .status.json on each turn for TUI/portal.

    Contains identity, runtime metrics, and token/context usage.
    Must only be called after _session exists (not during __init__).
    """
    from datetime import datetime, timezone
    from ..time_veil import now_iso, scrub_time_fields

    mail_addr = None
    if agent._mail_service is not None and agent._mail_service.address:
        mail_addr = agent._mail_service.address

    uptime = time.monotonic() - agent._uptime_anchor if agent._uptime_anchor is not None else 0.0
    stamina_left = max(0.0, agent._config.stamina - uptime) if agent._uptime_anchor is not None else None

    usage = agent.get_token_usage()

    window_size = None
    usage_pct = None
    if agent._chat is not None:
        try:
            # Use configured context_limit if set, otherwise model default
            window_size = agent._config.context_limit or agent._chat.context_window()
            ctx_total = usage["ctx_total_tokens"]
            usage_pct = round(ctx_total / window_size * 100, 1) if window_size else 0.0
        except Exception:
            pass

    return {
        "identity": {
            "address": str(agent._working_dir),
            "agent_name": agent.agent_name,
            "mail_address": mail_addr,
        },
        "runtime": scrub_time_fields(
            agent,
            {
                "current_time": now_iso(agent),
                "started_at": agent._started_at,
                "uptime_seconds": round(uptime, 1),
                "stamina": agent._config.stamina,
                "stamina_left": round(stamina_left, 1) if stamina_left is not None else None,
                "state": agent._state.value,
            },
            keys=("current_time", "started_at", "uptime_seconds", "stamina", "stamina_left"),
        ),
        "tokens": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "thinking_tokens": usage["thinking_tokens"],
            "cached_tokens": usage["cached_tokens"],
            "total_tokens": usage["total_tokens"],
            "api_calls": usage["api_calls"],
            "estimated": agent._session._token_fallback_warned,
            "context": {
                "system_tokens": usage["ctx_system_tokens"],
                "tools_tokens": usage["ctx_tools_tokens"],
                "history_tokens": usage["ctx_history_tokens"],
                "total_tokens": usage["ctx_total_tokens"],
                "window_size": window_size,
                "usage_pct": usage_pct,
                # Meta-line decomposition (matches build_meta's buckets)
                "fixed_tokens": usage["ctx_system_tokens"] + usage["ctx_tools_tokens"],
                "growing_tokens": usage["ctx_history_tokens"],
            },
        },
    }
