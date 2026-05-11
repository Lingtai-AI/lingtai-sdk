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


#: Whitelisted (non-secret) keys we surface from ``service``/``llm`` configs.
#: Safelist is more robust than denylist here — a single leaked credential
#: field is enough to break the contract, so anything outside this set is
#: dropped silently. ``base_url`` is included because operators rely on it
#: to disambiguate self-hosted endpoints from upstream providers.
_LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")


def _build_manifest(agent) -> dict:
    """Build the manifest dict for .agent.json.

    Subclasses override to add fields (e.g. capabilities, preset block).
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
    if agent._mail_service is not None and agent._mail_service.address:
        data["address"] = agent._mail_service.address

    # LLM identity — provider/model/base_url surfaced from the live service.
    # The endpoint is read from the explicit service `_base_url` when present
    # and falls back to provider-default `base_url`; api keys live behind the
    # adapter and are not on `service`.
    llm = _safe_llm_from_service(agent)
    if llm:
        data["llm"] = llm

    return data


def _safe_llm_from_service(agent) -> dict:
    """Extract a sanitized ``llm`` block from the live LLMService.

    Returns a safelisted public block (provider/model/base_url plus optional
    api_compat/context_limit) with only string/int values. Empty values, None,
    and non-scalars are dropped. Returns ``{}`` on any unexpected service shape
    (mocks in tests, future adapter rewrites). Never raises.
    """
    service = getattr(agent, "service", None)
    if service is None:
        return {}
    llm: dict = {}
    for key, attr in (
        ("provider", "provider"),
        ("model", "model"),
    ):
        try:
            val = getattr(service, attr, None)
        except Exception:
            val = None
        if isinstance(val, str) and val:
            llm[key] = val
        elif isinstance(val, int) and not isinstance(val, bool):
            llm[key] = val

    base_url = _effective_base_url_from_service(service)
    if isinstance(base_url, str) and base_url:
        llm["base_url"] = base_url

    context_limit = _safe_int_attr(service, "_context_window")
    if context_limit is not None:
        llm["context_limit"] = context_limit

    api_compat = _provider_default_from_service(service, "api_compat")
    if isinstance(api_compat, str) and api_compat:
        llm["api_compat"] = api_compat

    return llm


def _effective_base_url_from_service(service) -> str | None:
    """Return explicit or provider-default base URL from an LLMService-like object."""
    try:
        base_url = getattr(service, "_base_url", None)
    except Exception:
        base_url = None
    if isinstance(base_url, str) and base_url:
        return base_url

    val = _provider_default_from_service(service, "base_url")
    if isinstance(val, str) and val:
        return val
    return None


def _provider_default_from_service(service, key: str):
    """Read a scalar provider default from an LLMService-like object."""
    try:
        provider = getattr(service, "provider", None)
        defaults = getattr(service, "_provider_defaults", None)
    except Exception:
        return None
    if not isinstance(provider, str) or not isinstance(defaults, dict):
        return None
    provider_defaults = defaults.get(provider.lower())
    if not isinstance(provider_defaults, dict):
        return None
    return provider_defaults.get(key)


def _safe_int_attr(service, attr: str) -> int | None:
    try:
        val = getattr(service, attr, None)
    except Exception:
        return None
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    return None


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
