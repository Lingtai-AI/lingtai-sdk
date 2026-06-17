"""LingTaiClient — the programmable entry point of the SDK facade.

Constructed from :class:`LingTaiOptions`. Exposes two stable primitives:

- :meth:`build_agent_kwargs` — pure, side-effect-free kwargs for ``Agent``.
- :meth:`create_agent` — constructs a live ``lingtai.agent.Agent`` (does NOT
  start its loop; the caller owns lifecycle).

The client deliberately keeps a thin surface; it does not run the agent loop.
"""
from __future__ import annotations

from typing import Any

from .options import LingTaiOptions
from .runtime import options_to_agent_kwargs
from .tools import ToolSpec


class LingTaiClient:
    """A programmable client that builds/constructs a native LingTai agent."""

    def __init__(self, options: LingTaiOptions) -> None:
        self.options = options
        self._agent: Any | None = None

    def build_agent_kwargs(self, *, service: Any | None = None) -> dict[str, Any]:
        """Return the kwarg dict for ``lingtai.agent.Agent(...)``.

        Pure and testable: with *service* supplied (e.g. a mock), this performs
        no LLM construction and no I/O. The dict includes the SDK-internal
        ``_sdk_mcp_servers`` / ``_sdk_allowed_tools`` keys (underscore-prefixed,
        never forwarded to ``Agent``); :meth:`create_agent` strips them.
        """
        return options_to_agent_kwargs(self.options, service=service)

    def create_agent(
        self,
        *,
        service: Any | None = None,
        connect_mcp: bool = False,
    ) -> Any:
        """Construct and return a live ``lingtai.agent.Agent``.

        Does NOT call ``.start()`` — the caller controls the message loop and
        lifecycle. ``working_dir`` is required (raises ``ValueError`` if absent).

        Construction still acquires the runtime's exclusive working-directory
        lock. Call ``agent.stop()`` to release it even if you never start the
        loop.

        When *connect_mcp* is true and the options declare ``mcp_servers``, each
        server is connected after construction via the agent's ``connect_mcp`` /
        ``connect_mcp_http``. Connection failures are swallowed (logged via the
        agent's own logger) rather than raised, so a single bad server does not
        abort construction. SSE/SDK transports are skipped with no-op (the
        runtime does not yet host them).
        """
        kwargs = self.build_agent_kwargs(service=service)

        if kwargs.get("working_dir") is None:
            raise ValueError(
                "create_agent requires options.working_dir (or cwd) to be set"
            )

        mcp_servers = kwargs.pop("_sdk_mcp_servers", None)
        kwargs.pop("_sdk_allowed_tools", None)

        from lingtai.agent import Agent

        agent = Agent(**kwargs)
        self._agent = agent

        if connect_mcp and mcp_servers:
            self._connect_mcp_servers(agent, mcp_servers)

        return agent

    @staticmethod
    def _connect_mcp_servers(agent: Any, mcp_servers: dict[str, dict]) -> None:
        """Best-effort connect each declared MCP server to *agent*."""
        for name, cfg in mcp_servers.items():
            transport = cfg.get("type", "stdio")
            try:
                if transport == "http":
                    if cfg.get("url"):
                        agent.connect_mcp_http(
                            url=cfg["url"], headers=cfg.get("headers")
                        )
                elif transport == "stdio":
                    if cfg.get("command"):
                        agent.connect_mcp(
                            command=cfg["command"],
                            args=cfg.get("args"),
                            env=cfg.get("env"),
                        )
                # sse / sdk: forward-compat placeholders, not yet hosted.
            except Exception:
                # The runtime logs the failure; the SDK does not abort
                # construction over one unreachable server.
                pass

    def tool_inventory(self) -> list[ToolSpec]:
        """Return the constructed agent's registered tools as :class:`ToolSpec`.

        Must be called after :meth:`create_agent`. Reflects intrinsics and
        registered tool schemas. Returns ``[]`` if no agent has been created.
        """
        agent = self._agent
        if agent is None:
            return []

        specs: list[ToolSpec] = []
        for name in getattr(agent, "_intrinsics", {}) or {}:
            specs.append(ToolSpec(name=name, source="intrinsic"))
        for schema in getattr(agent, "_tool_schemas", []) or []:
            mcp_names = getattr(agent, "_mcp_tool_names", set()) or set()
            source = "mcp" if schema.name in mcp_names else "capability"
            specs.append(
                ToolSpec(
                    name=schema.name,
                    description=getattr(schema, "description", "") or "",
                    input_schema=getattr(schema, "parameters", {}) or {},
                    source=source,
                )
            )
        return specs
