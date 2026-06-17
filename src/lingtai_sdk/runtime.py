"""Native runtime adapter — translate :class:`LingTaiOptions` into the inputs
the LingTai runtime expects.

Pure translation. The two entry points construct objects but perform no disk or
network I/O of their own (constructing :class:`lingtai.llm.service.LLMService`
may register adapters on import, but spawns no LLM call).

This module is where Anthropic-SDK-flavored options meet the actual
``lingtai.agent.Agent`` constructor. Keeping it isolated means the contract
types (``options``/``mcp``/``tools``) stay free of runtime imports.
"""
from __future__ import annotations

from typing import Any

from .options import LingTaiOptions


def build_llm_service(options: LingTaiOptions) -> Any | None:
    """Construct an ``LLMService`` from *options*, or ``None`` if underspecified.

    Returns ``None`` when ``provider`` or ``model`` is absent — the caller may
    then inject its own service (e.g. a mock in tests, or a shared service).
    Importing ``lingtai`` first ensures provider adapters are registered.
    """
    if not options.provider or not options.model:
        return None

    import lingtai  # noqa: F401 — ensures adapters register before service creation
    from lingtai.llm.service import LLMService

    return LLMService(
        provider=options.provider,
        model=options.model,
        api_key=options.api_key,
        base_url=options.base_url,
    )


def derive_disable_list(options: LingTaiOptions) -> list[str]:
    """Translate ``disallowed_tools`` into a runtime ``disable=`` list.

    Only names recognized as built-in capability/group names are forwarded —
    the runtime's ``disable`` channel operates on capabilities, not arbitrary
    tool names. Unrecognized names are dropped here (the SDK makes no silent
    enforcement promise about non-capability tool names; see ANATOMY caveats).
    Group names (e.g. ``"file"``) are expanded to their member capabilities.
    """
    if not options.disallowed_tools:
        return []

    from lingtai.capabilities import _BUILTIN, _GROUPS

    disable: list[str] = []
    for name in options.disallowed_tools:
        if name in _GROUPS:
            for sub in _GROUPS[name]:
                if sub not in disable:
                    disable.append(sub)
        elif name in _BUILTIN:
            if name not in disable:
                disable.append(name)
    return disable


def options_to_agent_kwargs(
    options: LingTaiOptions,
    *,
    service: Any | None = None,
) -> dict[str, Any]:
    """Build the keyword-argument dict for ``lingtai.agent.Agent(...)``.

    Pure and side-effect-free apart from optionally constructing an
    ``LLMService`` (only when *service* is not supplied and provider/model are
    set). The returned dict always carries:

    - ``service`` — the LLMService (provided, built, or ``None``)
    - ``working_dir`` / ``agent_name`` — placement & identity
    - ``capabilities`` — forwarded verbatim
    - ``disable`` — derived from ``disallowed_tools`` (capability opt-outs)
    - prompt-asset kwargs (``covenant``/``principle``/...) when set
    - ``config`` — an ``AgentConfig`` when ``context_limit``/``max_turns`` set
      (``max_turns`` is recorded for compatibility but not enforced by the
      current active tool-loop guard)
    - ``_sdk_mcp_servers`` — runtime MCP dicts, NOT a real Agent kwarg. The SDK
      surfaces these here for :meth:`LingTaiClient.create_agent` to connect
      post-construction (the runtime loads MCP from the working dir, not via a
      constructor argument), and for the future CLI to persist into init.json.
      Underscore-prefixed so it is obviously not forwarded to ``Agent``.

    ``allowed_tools`` is recorded on the returned dict under
    ``_sdk_allowed_tools`` for host inspection but is not enforced here — the
    runtime has no allowlist gate; use ``disallowed_tools`` for opt-out.
    """
    if service is None:
        service = build_llm_service(options)

    kwargs: dict[str, Any] = {"service": service}

    if options.working_dir is not None:
        kwargs["working_dir"] = options.working_dir
    if options.agent_name is not None:
        kwargs["agent_name"] = options.agent_name
    if options.capabilities is not None:
        kwargs["capabilities"] = options.capabilities

    disable = derive_disable_list(options)
    if disable:
        kwargs["disable"] = disable

    if options.system_prompt is not None:
        kwargs.update(options.system_prompt.to_kwargs())

    config = _build_config(options)
    if config is not None:
        kwargs["config"] = config

    # MCP runtime dicts (secrets intact — these feed live connect_mcp* calls).
    if options.mcp_servers:
        kwargs["_sdk_mcp_servers"] = {
            name: cfg.to_runtime_dict(redact=False)
            for name, cfg in options.mcp_servers.items()
        }

    if options.allowed_tools is not None:
        kwargs["_sdk_allowed_tools"] = list(options.allowed_tools)

    return kwargs


def _build_config(options: LingTaiOptions) -> Any | None:
    """Build an ``AgentConfig`` carrying the option fields that map onto it.

    Returns ``None`` when no config-bearing field is set, so callers don't
    construct an all-default config unnecessarily.
    """
    # AgentConfig.max_turns is preserved for API compatibility, but the
    # current runtime ignores it for active tool-loop enforcement. We still
    # record it here so future runtime/CLI adoption does not need an API break.
    if options.context_limit is None and options.max_turns is None:
        return None

    from lingtai_kernel.config import AgentConfig

    cfg_kwargs: dict[str, Any] = {}
    if options.context_limit is not None:
        cfg_kwargs["context_limit"] = options.context_limit
    if options.max_turns is not None:
        cfg_kwargs["max_turns"] = options.max_turns
    return AgentConfig(**cfg_kwargs)
