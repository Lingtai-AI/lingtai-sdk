"""Wrapper-side bridge: host the real ``system`` lifecycle tool through the SDK
bundle declaration (stage 3C).

The high-state counterpart of ``lingtai.core.file_bundle`` (which bridges the
low-state file tools). Where ``file_bundle`` injects the wrapper's real
``read``/``glob``/``grep`` and ``write``/``edit`` handlers into the non-native
file-tool bundles, this module injects the **real ``system`` handler** into the
privileged, native-only ``system`` lifecycle bundle and so proves the
high-state bundle-execution pattern end to end against actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
Unlike the file tools (wrapper capabilities with a ``make_handler(agent)`` factory
in ``lingtai.core.{read,write,...}``), ``system`` is a **kernel intrinsic**:
``lingtai_kernel.intrinsics.system.handle(agent, args)``. The kernel wires it live
in ``BaseAgent._wire_intrinsics`` as ``self._intrinsics["system"] =
lambda args: system.handle(self, args)`` — that closure is the live registration
path, and it is **left untouched** by this stage.

The bridge therefore reuses that *same* ``handle`` function — there is no second
implementation. The bundle-hosted ``system`` tool runs byte-identical logic to the
intrinsic the kernel dispatches, against the same ``agent`` state (``agent._admin``
karma/nirvana authority, ``agent._working_dir``, ``agent._cpr_agent``, the
notification surface, …) and the same per-action authority gates
(``intrinsics.system.karma._check_karma_gate``). The bridge lives in the wrapper —
not the kernel — because the dependency direction is one-way: the wrapper *may*
import the SDK (``lingtai_sdk.lifecycle_tools``) and the kernel intrinsic; the
**kernel must never import the SDK**. Putting the SDK import here preserves that.

Import direction is the same wrapper→sdk edge ``file_bundle`` uses; the SDK is
imported lazily inside the bridge function, not at module load, so importing this
module does not eagerly pull the SDK.

The wrapper bundle host (``NativeBundleHost``) invokes its handler with keyword
args; the kernel intrinsic ``handle`` takes a single ``args: dict``. The tiny
``_kwargs_adapter`` reconciles the two without changing either contract — the
identical adapter ``file_bundle`` uses.

This module does **not** change how ``Agent`` registers or dispatches the
``system`` tool — ``_wire_intrinsics`` remains the live path. It is an additive,
observable seam: a host (or a test) can drive the real ``system`` lifecycle tool
through the declared manifest, which is the high-state migration template later
stages build on. In particular, the declared danger posture on the ``system``
manifest (bundle-level ``destructive`` plus the per-action ``SYSTEM_ACTION_RISK``
grading) is **not** enforced here: this bridge only hosts and runs the real
handler, and the real karma/nirvana authority gate is the kernel intrinsic's, not
this bridge's. Nothing here runs at ``Agent`` construction time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_sdk.capability_host import NativeBundleHost

# The single source of truth for ``system`` behavior — the kernel intrinsic the
# live ``_wire_intrinsics`` path also dispatches. Imported at wrapper module load
# (the wrapper may import the kernel intrinsic surface); the SDK is imported
# lazily inside the bridge function to preserve the wrapper→sdk import edge.
from lingtai_kernel.intrinsics import system as _system


def _kwargs_adapter(
    handler: Callable[["BaseAgent", dict], Any],
    agent: "BaseAgent",
) -> Callable[..., Any]:
    """Adapt the kernel intrinsic ``handle(agent, args)`` to host kwargs invocation.

    ``NativeBundleHost.invoke(tool, **kwargs)`` calls its handler with keyword
    args, but the kernel intrinsic ``system.handle`` takes ``(agent, args: dict)``.
    This binds *agent* and collects the kwargs back into the ``args`` dict so the
    real intrinsic runs unchanged — the high-state mirror of the adapter
    ``lingtai.core.file_bundle`` uses for the file handlers.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(agent, dict(kwargs))

    return _adapted


def system_lifecycle_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``system`` host seam expects.

    The handler is the kernel intrinsic ``system.handle`` — the *same* function
    ``BaseAgent._wire_intrinsics`` wires live — bound to *agent* and adapted to the
    host's keyword-args invocation contract. Bound to *agent*, so it reads through
    ``agent._admin`` / ``agent._working_dir`` / ``agent._cpr_agent`` and the
    notification surface exactly as the registered intrinsic does, including the
    per-action karma/nirvana authority gate.
    """
    return _kwargs_adapter(_system.handle, agent)


def system_lifecycle_bundle_host(agent: "BaseAgent") -> "NativeBundleHost":
    """Host the real ``system`` lifecycle tool through the SDK ``system`` bundle.

    Returns a single :class:`~lingtai_sdk.capability_host.NativeBundleHost` for
    ``system``, hosting the bundle's one declared tool with the wrapper's genuine
    handler (the kernel intrinsic, bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction.

    ``host.invoke("system", action=..., **args)`` then runs the real ``system``
    lifecycle logic through the declared manifest — proving the high-state
    bundle-execution pattern against actual behavior — without altering the
    agent's live intrinsic registration. The declared danger posture
    (bundle-level ``destructive`` plus the per-action ``SYSTEM_ACTION_RISK``
    grading) is a declaration only: the host runs the handler unconditionally;
    the real karma/nirvana authority gate is the kernel intrinsic's, and
    danger-based gating is the stage-17 guard bridge's job, neither installed
    here.
    """
    from lingtai_sdk.lifecycle_tools import system_lifecycle_host

    return system_lifecycle_host(system_lifecycle_handler(agent))


def system_lifecycle_bundle_hosts(agent: "BaseAgent") -> dict[str, "NativeBundleHost"]:
    """Host the real ``system`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`system_lifecycle_bundle_host`, parallel to
    ``file_bundle.file_tool_bundle_hosts`` / ``file_mutation_tool_bundle_hosts``,
    so the wrapper bridge exposes the same ``{name: NativeBundleHost}`` shape
    across all stages. Builds via the SDK ``system_lifecycle_hosts`` mapping seam,
    which enforces the single-``system``-handler contract.
    """
    from lingtai_sdk.lifecycle_tools import SYSTEM_TOOL_NAME, system_lifecycle_hosts

    return system_lifecycle_hosts(
        {SYSTEM_TOOL_NAME: system_lifecycle_handler(agent)}
    )


__all__ = [
    "system_lifecycle_handler",
    "system_lifecycle_bundle_host",
    "system_lifecycle_bundle_hosts",
]
