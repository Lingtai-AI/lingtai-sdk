"""Wrapper-side bridge: host the real ``mcp`` tool through the SDK tool-config
bundle declaration (stage 3F).

The tool-config/catalog counterpart of ``lingtai.core.communication_bundle`` (the
high-state ``email`` / ``daemon`` bridge, stage 3D), ``lingtai.core.system_bundle``
(the lifecycle bridge, stage 3C), and ``lingtai.core.file_bundle`` (the file-tool
bridge, stages 3A/3B). The SDK module :mod:`lingtai_sdk.mcp_tools` *declares* the
``mcp`` registry-view surface as a
:class:`~lingtai_sdk.capabilities.BundleManifest` and offers a host injection
seam, but — to respect the import boundary (the SDK must not import the wrapper,
and the kernel must never import the SDK) — it ships **no real handler**. This
module is where the wrapper, which *may* import the SDK and the wrapper
capability, injects the genuine handler and so proves the tool-config
bundle-execution pattern end to end against actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
``mcp`` is a **wrapper capability** (like ``daemon`` and the file tools), not a
kernel intrinsic. Its real handler is the closure ``lingtai.core.mcp.make_handler
(agent)`` builds — the *same* closure ``mcp.setup()`` registers via
``agent.add_tool`` (single source of truth; see ``lingtai.core.mcp.make_handler``).
The bridge reuses that *same* factory — there is no second implementation. The
bundle-hosted ``mcp`` tool runs byte-identical logic to the tool ``setup()``
registers, against the same ``agent._working_dir`` and the same ``_reconcile``
(read registry → re-render the ``mcp`` system-prompt section → health snapshot)
semantics. The bridge lives in the wrapper — not the kernel/SDK — because the
dependency direction is one-way: the wrapper *may* import the SDK
(``lingtai_sdk.mcp_tools``) and the wrapper capability; the **kernel/SDK must
never import the wrapper**. Putting the SDK import here (lazily, inside the bridge
functions) preserves that one-way ``wrapper -> sdk`` edge.

The SDK ``BundleHost`` invokes its handler with keyword args; the wrapper ``mcp``
handler takes a single ``args: dict``. The tiny ``_kwargs_adapter`` reconciles the
two without changing either contract — the identical adapter the file / lifecycle
/ communication bridges use.

This module does **not** change how ``Agent`` registers or dispatches the ``mcp``
tool — ``mcp.setup()`` remains the live path. It is an additive, observable seam:
a host (or a test) can drive the real ``mcp`` registry-view tool through the
declared manifest, the tool-config migration template later stages build on.
Constructing the bridge host writes nothing to the registry and starts no MCP
server — only an explicit ``host.invoke("mcp", action="show")`` re-reconciles,
exactly as the live path does. Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

    from lingtai_sdk.capability_host import BundleHost

# The wrapper's real mcp handler factory — the single source of truth shared with
# the capability ``setup()`` path (``mcp.make_handler``). Imported at wrapper
# module load (the wrapper may import its own capability surface); the SDK is
# imported lazily inside the bridge functions to preserve the wrapper -> sdk edge.
from . import mcp as _mcp


def _kwargs_adapter(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to a host's kwargs invocation.

    A host's ``invoke(tool, **kwargs)`` calls its handler with keyword args, but
    the wrapper ``mcp`` handler takes a single positional ``args`` dict. This
    collects the kwargs back into that dict so the real handler runs unchanged —
    the mcp mirror of the adapter ``lingtai.core.file_bundle`` /
    ``lingtai.core.communication_bundle`` use.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def mcp_config_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``mcp`` host seam expects.

    The handler is the wrapper's real ``mcp.make_handler(agent)`` closure — the
    *same* handler ``mcp.setup()`` registers — adapted to the host's keyword-args
    invocation contract. Bound to *agent*, so it reads through
    ``agent._working_dir`` and re-renders the ``mcp`` system-prompt section exactly
    as the registered tool does. Building it writes nothing to the registry and
    starts no MCP server.
    """
    return _kwargs_adapter(_mcp.make_handler(agent))


def mcp_config_bundle_host(agent: "BaseAgent") -> "BundleHost":
    """Host the real ``mcp`` tool through the SDK ``mcp`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``mcp``, hosting
    the bundle's one declared tool with the wrapper's genuine handler
    (``mcp.make_handler`` bound to *agent*). The SDK is imported here, in the
    wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction. ``host.invoke("mcp", action="show")`` runs the real registry-view
    logic through the declared manifest without altering the agent's live
    capability registration — and constructing the host writes nothing and starts
    no MCP server.
    """
    from lingtai_sdk.mcp_tools import mcp_config_host

    return mcp_config_host(mcp_config_handler(agent))


def mcp_config_bundle_hosts(agent: "BaseAgent") -> dict[str, "BundleHost"]:
    """Host the real ``mcp`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`mcp_config_bundle_host`, parallel to
    ``communication_bundle.communication_bundle_hosts`` /
    ``system_bundle.system_lifecycle_bundle_hosts`` /
    ``file_bundle.file_tool_bundle_hosts``, so the wrapper bridge exposes the same
    ``{name: host}`` shape across all stages. Builds via the SDK ``mcp_config_hosts``
    mapping seam, which enforces the single-``mcp``-handler contract.
    """
    from lingtai_sdk.mcp_tools import MCP_TOOL_NAME, mcp_config_hosts

    return mcp_config_hosts({MCP_TOOL_NAME: mcp_config_handler(agent)})


__all__ = [
    "mcp_config_handler",
    "mcp_config_bundle_host",
    "mcp_config_bundle_hosts",
]
