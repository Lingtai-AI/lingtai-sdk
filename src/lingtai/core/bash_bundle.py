"""Wrapper-side bridge: host the real ``bash`` tool through the SDK shell-execution
bundle declaration (stage 3H).

The arbitrary-shell-execution counterpart of ``lingtai.core.communication_bundle``
(the ``email`` / ``daemon`` bridge, stage 3D), ``lingtai.core.mcp_bundle`` (stage
3F), and ``lingtai.core.knowledge_bundle`` (stage 3G). The SDK module
:mod:`lingtai_sdk.bash_tools` *declares* the ``bash`` shell surface as a
:class:`~lingtai_sdk.capabilities.BundleManifest` and offers a host injection seam,
but — to respect the import boundary (the SDK must not import the wrapper, and the
kernel must never import the SDK) — it ships **no real handler**. This module is
where the wrapper, which *may* import the SDK and the wrapper capability, injects
the genuine handler and so proves the bundle-execution pattern end to end against
actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
``bash`` is a **wrapper capability** (like ``daemon``, ``mcp``, and the file
tools), not a kernel intrinsic. Its real handler is the ``handle`` method of a
:class:`~lingtai.core.bash.BashManager` built by ``lingtai.core.bash.make_handler
(agent)`` — the *same* manager/handler ``bash.setup()`` registers via
``agent.add_tool`` (single source of truth; see ``lingtai.core.bash.make_manager``
/ ``make_handler``). The bridge reuses that *same* factory — there is no second
implementation. The bundle-hosted ``bash`` tool runs byte-identical logic to the
tool ``setup()`` registers, against the same ``agent._working_dir`` sandbox and the
same resolved :class:`~lingtai.core.bash.BashPolicy` (the bridge, like
``setup()``'s default, resolves the bundled default policy via
``bash.resolve_policy``).

The bridge lives in the wrapper — not the kernel/SDK — because the dependency
direction is one-way: the wrapper *may* import the SDK (``lingtai_sdk.bash_tools``)
and the wrapper capability; the **kernel/SDK must never import the wrapper**.
Putting the SDK import here (lazily, inside the bridge functions) preserves that
one-way ``wrapper -> sdk`` edge.

The SDK ``BundleHost`` invokes its handler with keyword args; the wrapper ``bash``
handler takes a single ``args: dict``. The tiny ``_kwargs_adapter`` reconciles the
two without changing either contract — the identical adapter the file / lifecycle /
communication / mcp / knowledge bridges use.

This module does **not** change how ``Agent`` registers or dispatches the ``bash``
tool — ``bash.setup()`` remains the live path. It is an additive, observable seam:
a host (or a test) can drive the real ``bash`` tool through the declared manifest.
The declared danger posture (bundle-level ``destructive`` plus the per-action risk
table) is **not** enforced here: this bridge only hosts and runs the real handler,
and danger-based gating is the stage-17 guard bridge's job, not installed here.
Constructing the bridge host runs no command and starts no job — only an explicit
``host.invoke("bash", action="run", command=...)`` does, gated exactly as the live
path does (by the resolved ``BashPolicy`` and the working-directory sandbox).
Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

    from lingtai_sdk.capability_host import BundleHost

# The wrapper's real bash handler factory — the single source of truth shared with
# the capability ``setup()`` path (``bash.make_manager`` / ``make_handler``).
# Imported at wrapper module load (the wrapper may import its own capability
# surface); the SDK is imported lazily inside the bridge functions to preserve the
# wrapper -> sdk edge.
from . import bash as _bash


def _kwargs_adapter(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to a host's kwargs invocation.

    A host's ``invoke(tool, **kwargs)`` calls its handler with keyword args, but
    the wrapper ``bash`` handler takes a single positional ``args`` dict. This
    collects the kwargs back into that dict so the real handler runs unchanged —
    the bash mirror of the adapter ``lingtai.core.knowledge_bundle`` uses.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def bash_exec_handler(
    agent: "BaseAgent",
    policy_file: str | None = None,
    yolo: bool = False,
) -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``bash`` host seam expects.

    The handler is the wrapper's real ``bash.make_handler(agent)`` closure (the
    ``handle`` method of a :class:`~lingtai.core.bash.BashManager`) — the *same*
    handler ``bash.setup()`` registers — adapted to the host's keyword-args
    invocation contract. Bound to *agent*, so it runs commands under the
    ``agent._working_dir`` sandbox and enforces the resolved
    :class:`~lingtai.core.bash.BashPolicy` exactly as the registered tool does.
    ``policy_file`` / ``yolo`` are threaded through to ``bash.make_handler`` so the
    bridge resolves the same policy the live ``setup()`` would for those args (by
    default, the bundled denylist). Building it runs no command and starts no job.
    """
    return _kwargs_adapter(
        _bash.make_handler(agent, policy_file=policy_file, yolo=yolo)
    )


def bash_exec_bundle_host(
    agent: "BaseAgent",
    policy_file: str | None = None,
    yolo: bool = False,
) -> "BundleHost":
    """Host the real ``bash`` tool through the SDK ``bash`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``bash``, hosting
    the bundle's one declared tool with the wrapper's genuine handler
    (``bash.make_handler`` bound to *agent* with the resolved policy). The SDK is
    imported here, in the wrapper, not at SDK module load — preserving the one-way
    ``wrapper -> sdk`` direction. ``host.invoke("bash", action=..., **args)`` runs
    the real shell logic through the declared manifest without altering the agent's
    live capability registration — and constructing the host runs no command and
    starts no job.
    """
    from lingtai_sdk.bash_tools import bash_exec_host

    return bash_exec_host(bash_exec_handler(agent, policy_file=policy_file, yolo=yolo))


def bash_exec_bundle_hosts(
    agent: "BaseAgent",
    policy_file: str | None = None,
    yolo: bool = False,
) -> dict[str, "BundleHost"]:
    """Host the real ``bash`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`bash_exec_bundle_host`, parallel to
    ``communication_bundle.communication_bundle_hosts`` /
    ``knowledge_bundle.knowledge_catalog_bundle_hosts``, so the wrapper bridge
    exposes the same ``{name: host}`` shape across all stages. Builds via the SDK
    ``bash_exec_hosts`` mapping seam, which enforces the single-``bash``-handler
    contract.
    """
    from lingtai_sdk.bash_tools import BASH_TOOL_NAME, bash_exec_hosts

    return bash_exec_hosts(
        {BASH_TOOL_NAME: bash_exec_handler(agent, policy_file=policy_file, yolo=yolo)}
    )


__all__ = [
    "bash_exec_handler",
    "bash_exec_bundle_host",
    "bash_exec_bundle_hosts",
]
