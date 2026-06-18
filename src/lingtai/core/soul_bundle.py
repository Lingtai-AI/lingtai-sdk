"""Wrapper-side bridge: host the real ``soul`` inner-voice tool through the SDK
bundle declaration (stage 3J).

The inner-voice counterpart of ``lingtai.core.system_bundle`` (the ``system``
lifecycle bridge, stage 3C) and ``lingtai.core.psyche_bundle`` (the ``psyche``
identity/context bridge). Where those inject the real ``system`` / ``psyche``
handlers into their privileged, native-only core bundles, this module injects the
**real ``soul`` handler** into the privileged, native-only ``soul`` inner-voice
bundle and so proves the high-state bundle-execution pattern end to end against
actual behavior.

Where the real handler lives — and why the bridge lives here
------------------------------------------------------------
Exactly like ``system`` / ``psyche``, ``soul`` is a **kernel intrinsic**:
``lingtai_kernel.intrinsics.soul.handle(agent, args)``. The kernel wires it live
in ``BaseAgent._wire_intrinsics`` as ``self._intrinsics["soul"] =
lambda args: soul.handle(self, args)`` — that closure is the live registration
path, and it is **left untouched** by this stage.

The bridge therefore reuses that *same* ``handle`` function — there is no second
implementation. The bundle-hosted ``soul`` tool runs byte-identical logic to the
intrinsic the kernel dispatches, against the same ``agent`` state (the
``_soul_fire_lock`` fire gate, ``_soul_delay`` cadence, the consultation pipeline,
``logs/`` ledgers, and the ``.notification/soul.json`` surface). The bridge lives
in the wrapper — not the kernel — because the dependency direction is one-way: the
wrapper *may* import the SDK (``lingtai_sdk.soul_tools``) and the kernel intrinsic;
the **kernel must never import the SDK**. Putting the SDK import here (lazily,
inside the bridge functions) preserves that.

The wrapper bundle host (``NativeBundleHost``) invokes its handler with keyword
args; the kernel intrinsic ``handle`` takes a single ``args: dict``. The tiny
``_kwargs_adapter`` reconciles the two without changing either contract — the
identical adapter ``system_bundle`` / ``psyche_bundle`` use.

This module does **not** change how ``Agent`` registers or dispatches the ``soul``
tool — ``_wire_intrinsics`` remains the live path. It is an additive, observable
seam. The declared danger posture on the ``soul`` manifest (bundle-level
``caution`` plus the per-action ``SOUL_ACTION_RISK`` grading) is **not** enforced
here: this bridge only hosts and runs the real handler; danger-based gating is the
stage-17 guard bridge's job, not installed here. Nothing here runs at ``Agent``
construction time, and constructing the bridge host runs no LLM and spawns no
consultation thread — only an explicit ``host.invoke("soul", action=...)`` would,
exactly as the live path does (and the ``flow`` fire stays gated by the kernel
intrinsic's ``agent._soul_fire_lock``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent
    from lingtai_sdk.bundles.host import NativeBundleHost

# The single source of truth for ``soul`` behavior — the kernel intrinsic the live
# ``_wire_intrinsics`` path also dispatches. Imported at wrapper module load (the
# wrapper may import the kernel intrinsic surface); the SDK is imported lazily
# inside the bridge functions to preserve the wrapper→sdk import edge.
from lingtai_kernel.intrinsics import soul as _soul


def _kwargs_adapter(
    handler: Callable[["BaseAgent", dict], Any],
    agent: "BaseAgent",
) -> Callable[..., Any]:
    """Adapt the kernel intrinsic ``handle(agent, args)`` to host kwargs invocation.

    ``NativeBundleHost.invoke(tool, **kwargs)`` calls its handler with keyword
    args, but the kernel intrinsic ``soul.handle`` takes ``(agent, args: dict)``.
    This binds *agent* and collects the kwargs back into the ``args`` dict so the
    real intrinsic runs unchanged — the inner-voice mirror of the adapter
    ``lingtai.core.system_bundle`` uses for the system handler.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(agent, dict(kwargs))

    return _adapted


def soul_voice_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``soul`` host seam expects.

    The handler is the kernel intrinsic ``soul.handle`` — the *same* function
    ``BaseAgent._wire_intrinsics`` wires live — bound to *agent* and adapted to the
    host's keyword-args invocation contract. Bound to *agent*, so it reads through
    ``agent._soul_fire_lock`` / ``agent._soul_delay`` and the consultation pipeline
    exactly as the registered intrinsic does, including the ``flow`` in-flight gate.
    """
    return _kwargs_adapter(_soul.handle, agent)


def soul_voice_bundle_host(agent: "BaseAgent") -> "NativeBundleHost":
    """Host the real ``soul`` inner-voice tool through the SDK ``soul`` bundle.

    Returns a single :class:`~lingtai_sdk.capability_host.NativeBundleHost` for
    ``soul``, hosting the bundle's one declared tool with the wrapper's genuine
    handler (the kernel intrinsic, bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction.

    ``host.invoke("soul", action=..., **args)`` then runs the real ``soul``
    inner-voice logic through the declared manifest — proving the high-state
    bundle-execution pattern against actual behavior — without altering the agent's
    live intrinsic registration. The declared danger posture (bundle-level
    ``caution`` plus the per-action ``SOUL_ACTION_RISK`` grading) is a declaration
    only: the host runs the handler unconditionally; the real ``flow`` in-flight
    gate is the kernel intrinsic's ``agent._soul_fire_lock``, and danger-based
    gating is the stage-17 guard bridge's job, neither installed here.
    """
    from lingtai_sdk.bundles.soul_tools import soul_voice_host

    return soul_voice_host(soul_voice_handler(agent))


def soul_voice_bundle_hosts(agent: "BaseAgent") -> dict[str, "NativeBundleHost"]:
    """Host the real ``soul`` tool, in the ``{name: host}`` shape the seam uses.

    The mapping mirror of :func:`soul_voice_bundle_host`, parallel to
    ``system_bundle.system_lifecycle_bundle_hosts``, so the wrapper bridge exposes
    the same ``{name: NativeBundleHost}`` shape across all stages. Builds via the
    SDK ``soul_voice_hosts`` mapping seam, which enforces the single-``soul``-handler
    contract.
    """
    from lingtai_sdk.bundles.soul_tools import SOUL_TOOL_NAME, soul_voice_hosts

    return soul_voice_hosts({SOUL_TOOL_NAME: soul_voice_handler(agent)})


__all__ = [
    "soul_voice_handler",
    "soul_voice_bundle_host",
    "soul_voice_bundle_hosts",
]
