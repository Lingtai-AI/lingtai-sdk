"""Tool surface — schemas, dispatch, inventory refresh, and tool registry.

The 2-layer tool dispatch: intrinsics (built-in) + capabilities/MCP.
"""
from __future__ import annotations

from ..intrinsics import ALL_INTRINSICS
from ..llm import FunctionSchema
from ..i18n import t as _t
from ..types import UnknownToolError


def _dispatch_tool(agent, tc) -> dict:
    """Dispatch a tool call to the appropriate handler.

    Layer 1: intrinsics (built-in tools)
    Layer 2: MCP handlers (domain tools)

    Raises UnknownToolError if the tool name is not found.
    """
    if tc.name in agent._intrinsics:
        # Inject the wire tool_use_id so intrinsics that need to locate
        # their own ToolCallBlock in the live interface (notably
        # psyche._context_molt) can find it. Intrinsics that don't care
        # simply ignore the field.
        args = dict(tc.args or {})
        args["_tc_id"] = tc.id
        return agent._intrinsics[tc.name](args)
    elif tc.name in agent._tool_handlers:
        return agent._tool_handlers[tc.name](tc.args or {})
    else:
        raise UnknownToolError(tc.name)


def _refresh_tool_inventory_section(agent) -> None:
    """Rebuild the 'tools' section from current intrinsic + schema descriptions."""
    lang = agent._config.language
    lines = []
    for name in agent._intrinsics:
        info = ALL_INTRINSICS.get(name)
        if info:
            lines.append(f"### {name}\n{info['module'].get_description(lang)}")
    for s in agent._tool_schemas:
        if s.description:
            lines.append(f"### {s.name}\n{s.description}")
    if lines:
        agent._prompt_manager.write_section(
            "tools", "\n\n".join(lines), protected=True
        )


def _build_tool_schemas(agent) -> list[FunctionSchema]:
    """Build the complete tool schema list for the LLM.

    Every tool gets a 'reasoning' parameter injected — the agent must
    explain why it's calling this tool. Reasoning is logged as part of
    the agent's diary and stripped before the handler runs.
    """
    reasoning_prop = {
        "reasoning": {
            "type": "string",
            "description": _t(agent._config.language, "tool.reasoning_description"),
        },
    }

    schemas = []

    # Intrinsic schemas
    lang = agent._config.language
    for name in agent._intrinsics:
        info = ALL_INTRINSICS.get(name)
        if info:
            params = dict(info["module"].get_schema(lang))
            props = dict(params.get("properties", {}))
            props.update(reasoning_prop)
            params["properties"] = props
            schemas.append(
                FunctionSchema(
                    name=name,
                    description=info["module"].get_description(lang),
                    parameters=params,
                )
            )

    # Capability + MCP schemas — inject reasoning into each
    for s in agent._tool_schemas:
        params = dict(s.parameters)
        props = dict(params.get("properties", {}))
        props.update(reasoning_prop)
        params["properties"] = props
        schemas.append(
            FunctionSchema(
                name=s.name,
                description=s.description,
                parameters=params,
            )
        )

    return schemas


def _add_tool(
    agent,
    name: str,
    *,
    schema: dict | None = None,
    handler=None,
    description: str = "",
    system_prompt: str = "",
) -> None:
    """Register a dynamic tool."""
    if agent._sealed:
        raise RuntimeError("Cannot modify tools after start()")
    if handler is not None:
        agent._tool_handlers[name] = handler
    if schema is not None:
        # Remove any existing schema with same name
        agent._tool_schemas = [s for s in agent._tool_schemas if s.name != name]
        agent._tool_schemas.append(
            FunctionSchema(
                name=name,
                description=description,
                parameters=schema,
                system_prompt=system_prompt,
            )
        )
    # Update the live session's tools if one exists
    if agent._chat is not None:
        agent._chat.update_tools(_build_tool_schemas(agent))
    agent._token_decomp_dirty = True


def _remove_tool(agent, name: str) -> None:
    """Unregister a dynamic tool."""
    if agent._sealed:
        raise RuntimeError("Cannot modify tools after start()")
    agent._tool_handlers.pop(name, None)
    agent._tool_schemas = [s for s in agent._tool_schemas if s.name != name]
    if agent._chat is not None:
        agent._chat.update_tools(_build_tool_schemas(agent))
    agent._token_decomp_dirty = True


def _override_intrinsic(agent, name: str):
    """Remove an intrinsic and return its handler for delegation.

    Called by capabilities that upgrade an intrinsic (e.g. email → mail).
    Must be called before start() (tool surface sealed).

    Returns the original handler so the capability can delegate to it.
    """
    if agent._sealed:
        raise RuntimeError("Cannot modify tools after start()")
    handler = agent._intrinsics.pop(name)  # raises KeyError if missing
    agent._token_decomp_dirty = True
    return handler


def _has_capability(agent, name: str) -> bool:
    """Check if a capability is registered. Subclasses override."""
    return False
