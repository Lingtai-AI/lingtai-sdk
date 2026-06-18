"""Wrapper-side bridge: host the real ``psyche`` identity/context tool through the
SDK bundle declaration (stage 3J).

The identity/context counterpart of ``lingtai.core.system_bundle`` (the ``system``
lifecycle bridge, stage 3C). Where ``system_bundle`` injects the real ``system``
handler into the privileged, native-only ``system`` lifecycle bundle, this module
injects the **real ``psyche`` handler** into the privileged, native-only ``psyche``
identity bundle and so proves the high-state bundle-execution pattern end to end
against actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
Exactly like ``system``, ``psyche`` is a **kernel intrinsic**:
``lingtai_kernel.intrinsics.psyche.handle(agent, args)``. The kernel wires it live
in ``BaseAgent._wire_intrinsics`` as ``self._intrinsics["psyche"] =
lambda args: psyche.handle(self, args)`` — that closure is the live registration
path, and it is **left untouched** by this stage.

The bridge therefore reuses that *same* ``handle`` function — there is no second
implementation. The bundle-hosted ``psyche`` tool runs byte-identical logic to the
intrinsic the kernel dispatches, against the same ``agent`` state (``system/``
files, ``history/`` snapshots, the molt machinery, the notification surface). The
bridge lives in the wrapper — not the kernel — because the dependency direction is
one-way: the wrapper *may* import the SDK (``lingtai_sdk.psyche_tools``) and the
kernel intrinsic; the **kernel must never import the SDK**. Putting the SDK import
here (lazily, inside the bridge functions) preserves that.

The wrapper bundle host (``NativeBundleHost``) invokes its handler with keyword
args; the kernel intrinsic ``handle`` takes a single ``args: dict``. The tiny
``_kwargs_adapter`` reconciles the two without changing either contract — the
identical adapter ``system_bundle`` uses.

This module does **not** change how ``Agent`` registers or dispatches the
``psyche`` tool — ``_wire_intrinsics`` remains the live path. It is an additive,
observable seam. The declared danger posture on the ``psyche`` manifest
(bundle-level ``destructive`` plus the per-(object, action) ``PSYCHE_OBJECT_ACTION_RISK``
grading) is **not** enforced here: this bridge only hosts and runs the real handler;
danger-based gating is the stage-17 guard bridge's job, not installed here. Nothing
here runs at ``Agent`` construction time, and constructing the bridge host writes no
file and sheds no context — only an explicit ``host.invoke("psyche", object=...,
action=...)`` would, exactly as the live path does.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_sdk.bundles.host import NativeBundleHost

# The single source of truth for ``psyche`` behavior — the kernel intrinsic the
# live ``_wire_intrinsics`` path also dispatches. Imported at wrapper module load
# (the wrapper may import the kernel intrinsic surface); the SDK is imported
# lazily inside the bridge functions to preserve the wrapper→sdk import edge.
from lingtai_kernel.intrinsics import psyche as _psyche


def _kwargs_adapter(
    handler: Callable[["BaseAgent", dict], Any],
    agent: "BaseAgent",
) -> Callable[..., Any]:
    """Adapt the kernel intrinsic ``handle(agent, args)`` to host kwargs invocation.

    ``NativeBundleHost.invoke(tool, **kwargs)`` calls its handler with keyword
    args, but the kernel intrinsic ``psyche.handle`` takes ``(agent, args: dict)``.
    This binds *agent* and collects the kwargs back into the ``args`` dict so the
    real intrinsic runs unchanged — the identity/context mirror of the adapter
    ``lingtai.core.system_bundle`` uses for the system handler.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(agent, dict(kwargs))

    return _adapted


def psyche_identity_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``psyche`` host seam expects.

    The handler is the kernel intrinsic ``psyche.handle`` — the *same* function
    ``BaseAgent._wire_intrinsics`` wires live — bound to *agent* and adapted to the
    host's keyword-args invocation contract. Bound to *agent*, so it reads/writes
    through ``agent._working_dir`` (``system/`` + ``history/``) and the molt /
    notification machinery exactly as the registered intrinsic does.
    """
    return _kwargs_adapter(_psyche.handle, agent)


def psyche_identity_bundle_host(agent: "BaseAgent") -> "NativeBundleHost":
    """Host the real ``psyche`` identity/context tool through the SDK ``psyche`` bundle.

    Returns a single :class:`~lingtai_sdk.capability_host.NativeBundleHost` for
    ``psyche``, hosting the bundle's one declared tool with the wrapper's genuine
    handler (the kernel intrinsic, bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction.

    ``host.invoke("psyche", object=..., action=..., **args)`` then runs the real
    ``psyche`` identity/context logic through the declared manifest — proving the
    high-state bundle-execution pattern against actual behavior — without altering
    the agent's live intrinsic registration. The declared danger posture
    (bundle-level ``destructive`` plus the per-(object, action) ``PSYCHE_OBJECT_ACTION_RISK``
    grading) is a declaration only: the host runs the handler unconditionally;
    danger-based gating is the stage-17 guard bridge's job, not installed here.
    """
    from lingtai_sdk.bundles.psyche_tools import psyche_identity_host

    return psyche_identity_host(psyche_identity_handler(agent))


def psyche_identity_bundle_hosts(agent: "BaseAgent") -> dict[str, "NativeBundleHost"]:
    """Host the real ``psyche`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`psyche_identity_bundle_host`, parallel to
    ``system_bundle.system_lifecycle_bundle_hosts``, so the wrapper bridge exposes
    the same ``{name: NativeBundleHost}`` shape across all stages. Builds via the
    SDK ``psyche_identity_hosts`` mapping seam, which enforces the
    single-``psyche``-handler contract.
    """
    from lingtai_sdk.bundles.psyche_tools import PSYCHE_TOOL_NAME, psyche_identity_hosts

    return psyche_identity_hosts(
        {PSYCHE_TOOL_NAME: psyche_identity_handler(agent)}
    )


__all__ = [
    "psyche_identity_handler",
    "psyche_identity_bundle_host",
    "psyche_identity_bundle_hosts",
]
