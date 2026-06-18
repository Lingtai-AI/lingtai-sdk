"""Wrapper-side bridge: host the real ``avatar_spawn`` / ``avatar_rules`` tools
through the SDK peer-spawn bundle declarations (stage 3I).

The independent-peer-spawning counterpart of ``lingtai.core.bash_bundle`` (the
``bash`` bridge, stage 3H), ``lingtai.core.communication_bundle`` (the ``email`` /
``daemon`` bridge, stage 3D), and the ``mcp`` / ``knowledge`` / ``skills`` bridges.
The SDK module :mod:`lingtai_sdk.avatar_tools` *declares* the ``avatar_spawn`` and
``avatar_rules`` surfaces as :class:`~lingtai_sdk.capabilities.BundleManifest`\\ s
and offers host injection seams, but — to respect the import boundary (the SDK must
not import the wrapper, and the kernel must never import the SDK) — it ships **no
real handlers**. This module is where the wrapper, which *may* import the SDK and
the wrapper capability, injects the genuine handlers and so proves the
bundle-execution pattern end to end against actual behavior.

Where the real handlers live — and why the bridge lives here
------------------------------------------------------------
``avatar`` is a **wrapper capability** (like ``daemon``, ``mcp``, ``knowledge``,
``skills``, ``bash``, and the file tools), not a kernel intrinsic. Its real
handlers are the ``handle_spawn`` / ``handle_rules`` methods of an
:class:`~lingtai.core.avatar.AvatarManager` built by
``lingtai.core.avatar.make_manager(agent)`` — the *same* manager
``avatar.setup()`` registers via ``agent.add_tool`` (single source of truth; see
``lingtai.core.avatar.make_manager`` / ``make_spawn_handler`` / ``make_rules_handler``).
The bridge reuses that *same* factory — there is no second implementation. The
bundle-hosted ``avatar_spawn`` / ``avatar_rules`` tools run byte-identical logic to
the tools ``setup()`` registers, against the same parent ``agent`` and the same
``agent._working_dir`` network root.

The bridge lives in the wrapper — not the kernel/SDK — because the dependency
direction is one-way: the wrapper *may* import the SDK
(``lingtai_sdk.avatar_tools``) and the wrapper capability; the **kernel/SDK must
never import the wrapper**. Putting the SDK import here (lazily, inside the bridge
functions) preserves that one-way ``wrapper -> sdk`` edge.

The SDK ``BundleHost`` invokes its handler with keyword args; the wrapper avatar
handlers take a single ``args: dict``. The tiny ``_kwargs_adapter`` reconciles the
two without changing either contract — the identical adapter the file / lifecycle /
communication / mcp / knowledge / skills / bash bridges use.

This module does **not** change how ``Agent`` registers or dispatches the avatar
tools — ``avatar.setup()`` remains the live path. It is an additive, observable
seam: a host (or a test) can drive the real avatar tools through the declared
manifests. The declared danger posture (bundle-level ``destructive`` plus the
per-tool / per-args risk helpers) is **not** enforced here: this bridge only hosts
and runs the real handlers, and danger-based gating is the stage-17 guard bridge's
job, not installed here. Constructing the bridge hosts spawns no process and writes
no file — only an explicit ``host.invoke("avatar_spawn", name=...)`` (gated by the
live mission-quality gate, name validation, liveness check, and ``dry_run``
short-circuit) or ``host.invoke("avatar_rules", rules_content=...)`` (gated by the
live admin check) does. Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai.agent import Agent

    from lingtai_sdk.capability_host import BundleHost

# The wrapper's real avatar capability — the single source of truth shared with the
# capability ``setup()`` path (``avatar.make_manager`` / ``make_spawn_handler`` /
# ``make_rules_handler``). Imported at wrapper module load (the wrapper may import
# its own capability surface); the SDK is imported lazily inside the bridge
# functions to preserve the wrapper -> sdk edge.
from . import avatar as _avatar


def _kwargs_adapter(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to a host's kwargs invocation.

    A host's ``invoke(tool, **kwargs)`` calls its handler with keyword args, but the
    wrapper avatar handlers take a single positional ``args`` dict. This collects
    the kwargs back into that dict so the real handler runs unchanged — the avatar
    mirror of the adapter ``lingtai.core.bash_bundle`` uses.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def avatar_spawn_handler(agent: "Agent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``avatar_spawn`` host seam expects.

    The handler is the wrapper's real ``avatar.make_spawn_handler(agent)`` closure
    (the ``handle_spawn`` method of an :class:`~lingtai.core.avatar.AvatarManager`)
    — the *same* handler ``avatar.setup()`` registers — adapted to the host's
    keyword-args invocation contract. Bound to *agent*, so it spawns avatars as
    siblings under the same ``agent._working_dir`` network root and enforces the
    same mission-quality gate, name validation, and liveness checks the registered
    tool does. Building it spawns no process and writes no file.
    """
    return _kwargs_adapter(_avatar.make_spawn_handler(agent))


def avatar_rules_handler(agent: "Agent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``avatar_rules`` host seam expects.

    The handler is the wrapper's real ``avatar.make_rules_handler(agent)`` closure
    (the ``handle_rules`` method of an :class:`~lingtai.core.avatar.AvatarManager`)
    — the *same* handler ``avatar.setup()`` registers — adapted to the host's
    keyword-args invocation contract. Bound to *agent*, so it enforces the same
    admin gate and distributes ``.rules`` across the same avatar subtree the
    registered tool does. Building it writes nothing.
    """
    return _kwargs_adapter(_avatar.make_rules_handler(agent))


def avatar_spawn_bundle_host(agent: "Agent") -> "BundleHost":
    """Host the real ``avatar_spawn`` tool through the SDK ``avatar_spawn`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``avatar_spawn``,
    hosting the bundle's one declared tool with the wrapper's genuine handler
    (``avatar.make_spawn_handler`` bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction. ``host.invoke("avatar_spawn", name=..., **args)`` runs the real spawn
    logic through the declared manifest without altering the agent's live capability
    registration — and constructing the host spawns no process.
    """
    from lingtai_sdk.avatar_tools import avatar_spawn_host

    return avatar_spawn_host(avatar_spawn_handler(agent))


def avatar_rules_bundle_host(agent: "Agent") -> "BundleHost":
    """Host the real ``avatar_rules`` tool through the SDK ``avatar_rules`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``avatar_rules``,
    hosting the bundle's one declared tool with the wrapper's genuine handler
    (``avatar.make_rules_handler`` bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load. ``host.invoke("avatar_rules",
    rules_content=...)`` runs the real (admin-gated) rules-distribution logic
    through the declared manifest without altering the agent's live capability
    registration — and constructing the host writes nothing.
    """
    from lingtai_sdk.avatar_tools import avatar_rules_host

    return avatar_rules_host(avatar_rules_handler(agent))


def avatar_bundle_hosts(agent: "Agent") -> dict[str, "BundleHost"]:
    """Host the real avatar tools, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`avatar_spawn_bundle_host` /
    :func:`avatar_rules_bundle_host`, parallel to
    ``communication_bundle.communication_bundle_hosts`` /
    ``bash_bundle.bash_exec_bundle_hosts``, so the wrapper bridge exposes the same
    ``{name: host}`` shape across all stages. Builds both from the *same*
    ``avatar.make_manager``-backed factories and routes them through the SDK
    ``avatar_tool_hosts`` mapping seam, which enforces the exactly-two-handlers
    contract.
    """
    from lingtai_sdk.avatar_tools import (
        AVATAR_RULES_TOOL_NAME,
        AVATAR_SPAWN_TOOL_NAME,
        avatar_tool_hosts,
    )

    return avatar_tool_hosts(
        {
            AVATAR_SPAWN_TOOL_NAME: avatar_spawn_handler(agent),
            AVATAR_RULES_TOOL_NAME: avatar_rules_handler(agent),
        }
    )


__all__ = [
    "avatar_spawn_handler",
    "avatar_rules_handler",
    "avatar_spawn_bundle_host",
    "avatar_rules_bundle_host",
    "avatar_bundle_hosts",
]
