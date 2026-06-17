"""Declarative options for constructing a LingTai agent.

``LingTaiOptions`` is the SDK's single config object — typed, forward-compatible,
and secret-safe. It uses LingTai-native vocabulary (``working_dir``,
``capabilities``, ``covenant``/``principle``) organized in the same spirit as the
Anthropic Agent SDK's ``ClaudeAgentOptions``. A ``cwd`` alias is accepted for
ergonomics.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcp import MCPServerConfig

_REDACTED = "***"


@dataclass
class SystemPromptAssets:
    """Lightweight holder for the runtime's prompt-asset slots.

    These map one-to-one onto ``lingtai.agent.Agent`` / ``BaseAgent`` constructor
    string arguments. The SDK passes them through verbatim — it does NOT
    auto-assemble a product prompt. Empty strings are treated as "unset".
    """

    covenant: str = ""
    principle: str = ""
    substrate: str = ""
    procedures: str = ""
    brief: str = ""
    pad: str = ""
    comment: str = ""

    def to_kwargs(self) -> dict[str, str]:
        """Return only the non-empty asset slots as Agent constructor kwargs."""
        return {
            k: v
            for k, v in dataclasses.asdict(self).items()
            if isinstance(v, str) and v
        }

    def is_empty(self) -> bool:
        return not self.to_kwargs()


def _redact_env(env: dict[str, str] | None) -> dict[str, str] | None:
    """Return env with values redacted, preserving key names for diagnostics."""
    if not env:
        return None
    return {k: _REDACTED for k in env}


@dataclass
class LingTaiOptions:
    """Declarative configuration for building/constructing a LingTai agent.

    All fields are optional so partial configs are valid building blocks. The
    SDK validates only at the point of constructing a real agent (e.g.
    ``working_dir`` is required by :meth:`LingTaiClient.create_agent`).

    Secret safety: ``api_key`` is never shown in ``repr`` or in ``to_dict()``
    when ``redact=True`` (the default). Top-level ``env`` values and MCP
    headers/env values are redacted by default.
    """

    # LLM
    model: str | None = None
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None

    # Identity / placement
    working_dir: str | Path | None = None
    agent_name: str | None = None

    # Capabilities & tools
    capabilities: list[str] | dict[str, dict] | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None

    # MCP
    mcp_servers: dict[str, MCPServerConfig] | None = None

    # Prompt assets (not auto-assembled — passed through)
    system_prompt: SystemPromptAssets | None = None

    # Budget / loop
    # ``max_turns`` is recorded in AgentConfig for forward compatibility, but
    # the current runtime deliberately ignores AgentConfig.max_turns for the
    # active tool-loop guard. Do not treat it as an enforced limit yet.
    max_turns: int | None = None
    context_limit: int | None = None

    # Environment / extra dirs (recorded; not yet wired — see ANATOMY)
    env: dict[str, str] | None = None
    add_dirs: list[str | Path] | None = None

    # Forward-compat: recorded but not yet enforced by the runtime.
    permission_mode: str | None = None

    # ``cwd`` is an ergonomic alias for ``working_dir`` (Anthropic-SDK style).
    # It is resolved into ``working_dir`` in ``__post_init__`` and then cleared,
    # so it never participates in serialization or equality on its own.
    cwd: dataclasses.InitVar[str | Path | None] = None

    def __post_init__(self, cwd: str | Path | None) -> None:
        if cwd is not None and self.working_dir is None:
            self.working_dir = cwd

    def replace(self, **changes: Any) -> "LingTaiOptions":
        """Return a copy of these options with *changes* applied."""
        return dataclasses.replace(self, **changes)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Return a JSON-friendly dict.

        ``api_key`` is redacted to ``"***"`` when ``redact=True`` (default).
        ``mcp_servers`` are serialized via each config's ``to_runtime_dict``
        (also honoring ``redact``). ``working_dir`` / ``add_dirs`` paths are
        stringified. ``system_prompt`` becomes its non-empty asset kwargs.
        """
        out: dict[str, Any] = {
            "model": self.model,
            "provider": self.provider,
            "api_key": (_REDACTED if (self.api_key and redact) else self.api_key),
            "base_url": self.base_url,
            "working_dir": (str(self.working_dir) if self.working_dir is not None else None),
            "agent_name": self.agent_name,
            "capabilities": self.capabilities,
            "allowed_tools": self.allowed_tools,
            "disallowed_tools": self.disallowed_tools,
            "max_turns": self.max_turns,
            "context_limit": self.context_limit,
            "env": (_redact_env(self.env) if (self.env and redact) else (dict(self.env) if self.env else None)),
            "add_dirs": ([str(d) for d in self.add_dirs] if self.add_dirs else None),
            "permission_mode": self.permission_mode,
        }
        if self.mcp_servers:
            out["mcp_servers"] = {
                name: cfg.to_runtime_dict(redact=redact)
                for name, cfg in self.mcp_servers.items()
            }
        else:
            out["mcp_servers"] = None
        if self.system_prompt is not None:
            out["system_prompt"] = self.system_prompt.to_kwargs()
        else:
            out["system_prompt"] = None
        return out

    def __repr__(self) -> str:
        key_state = "set" if self.api_key else None
        return (
            f"LingTaiOptions(provider={self.provider!r}, model={self.model!r}, "
            f"working_dir={str(self.working_dir)!r}, agent_name={self.agent_name!r}, "
            f"capabilities={self.capabilities!r}, api_key={key_state!r})"
        )
