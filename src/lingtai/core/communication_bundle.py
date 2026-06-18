"""Wrapper-side bridge: host the real ``email`` and ``daemon`` tools through the
SDK communication/execution bundle declarations (stage 3D).

The communication/execution counterpart of ``lingtai.core.system_bundle`` (the
lifecycle bridge, stage 3C) and ``lingtai.core.file_bundle`` (the file-tool
bridge, stages 3A/3B). The SDK module :mod:`lingtai_sdk.communication_tools`
*declares* the two high-state surfaces as
:class:`~lingtai_sdk.capabilities.BundleManifest` objects and offers host
injection seams, but — to respect the import boundary (the SDK must not import
the wrapper, and the kernel must never import the SDK) — it ships **no real
handler**. This module is where the wrapper, which *may* import the SDK and the
kernel intrinsic, injects the genuine handlers and so proves the high-state
bundle-execution pattern end to end against actual behavior.

Two surfaces, two carriers — matching the live wiring
-----------------------------------------------------
* ``email`` is a **kernel intrinsic**: ``lingtai_kernel.intrinsics.email.handle
  (agent, args)``, wired live by ``BaseAgent._wire_intrinsics`` as
  ``self._intrinsics["email"] = lambda args: email.handle(self, args)`` — that
  closure is the live registration path, **left untouched** by this stage. The
  bridge reuses that *same* ``handle`` function (no second implementation) and
  hosts it natively, mirroring how the intrinsic path carries it. The
  intrinsic's own internal/external routing, the reserved ``unread`` guard, and
  the EmailManager boot dependency all flow through unchanged.
* ``daemon`` is a **wrapper capability**: ``lingtai.core.daemon`` registered live
  by its ``setup()`` through ``agent.add_tool``. The bridge reuses the *same*
  ``daemon.make_handler(agent)`` closure ``setup()`` builds (single source of
  truth — see ``lingtai.core.daemon.make_manager``), and hosts it in-process,
  mirroring how the capability path carries it. Constructing the manager via the
  bridge neither spawns nor kills any process — only an explicit
  ``emanate`` / ``ask`` / ``reclaim`` invocation does.

The wrapper/kernel handlers take a single ``args: dict``; the SDK host
``invoke`` passes keyword args. The tiny ``_kwargs_adapter`` reconciles the two
without changing either contract — the identical adapter the lifecycle/file
bridges use.

This module does **not** change how ``Agent`` registers or dispatches ``email``
or ``daemon`` — ``_wire_intrinsics`` and ``daemon.setup()`` remain the live
paths. It is an additive, observable seam: a host (or a test) can drive the real
tools through the declared manifests, the high-state migration template later
stages build on. The declared danger postures (bundle-level ``destructive`` plus
the per-action risk tables) are **not** enforced here: this bridge only hosts and
runs the real handler, and danger-based gating is the stage-17 guard bridge's
job, not installed here. Nothing here runs at ``Agent`` construction time. The
SDK is imported lazily inside the bridge functions, preserving the one-way
``wrapper -> sdk`` import edge.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

    from lingtai_sdk.capability_host import BundleHost, NativeBundleHost

# The single source of truth for ``email`` behavior — the kernel intrinsic the
# live ``_wire_intrinsics`` path also dispatches. Imported at wrapper module load
# (the wrapper may import the kernel intrinsic surface); the SDK is imported
# lazily inside the bridge functions to preserve the wrapper -> sdk import edge.
from lingtai_kernel.intrinsics import email as _email

# The wrapper's real daemon handler factory — the single source of truth shared
# with the capability ``setup()`` path (``daemon.make_manager``).
from . import daemon as _daemon


def _kwargs_adapter_dict(
    handler: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Adapt an ``args: dict`` wrapper handler to a host's kwargs invocation.

    A host's ``invoke(tool, **kwargs)`` calls its handler with keyword args, but
    the wrapper ``daemon`` handler takes a single positional ``args`` dict. This
    collects the kwargs back into that dict so the real handler runs unchanged —
    the daemon mirror of the adapter ``lingtai.core.file_bundle`` uses.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(dict(kwargs))

    return _adapted


def _kwargs_adapter_agent(
    handler: Callable[["BaseAgent", dict], Any],
    agent: "BaseAgent",
) -> Callable[..., Any]:
    """Adapt a kernel intrinsic ``handle(agent, args)`` to a host's kwargs invocation.

    Binds *agent* and collects the host's kwargs back into the ``args`` dict so
    the real intrinsic runs unchanged — the email mirror of the adapter
    ``lingtai.core.system_bundle`` uses for the ``system`` intrinsic.
    """

    def _adapted(**kwargs: Any) -> Any:
        return handler(agent, dict(kwargs))

    return _adapted


def email_comm_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``email`` host seam expects.

    The handler is the kernel intrinsic ``email.handle`` — the *same* function
    ``BaseAgent._wire_intrinsics`` wires live — bound to *agent* and adapted to
    the host's keyword-args invocation contract. Bound to *agent*, so it reads
    through ``agent._email_manager`` (and the configured mail service for external
    sends) exactly as the registered intrinsic does, including the reserved
    ``unread`` guard.
    """
    return _kwargs_adapter_agent(_email.handle, agent)


def daemon_exec_handler(agent: "BaseAgent") -> Callable[..., Any]:
    """Build the kwargs-handler the SDK ``daemon`` host seam expects.

    The handler is the wrapper's real ``daemon.make_handler(agent)`` closure —
    the *same* ``DaemonManager.handle`` ``setup()`` registers (see
    ``daemon.make_manager``) — adapted to the host's keyword-args invocation
    contract. Building it constructs a ``DaemonManager`` bound to *agent* but
    spawns / kills nothing until an explicit ``emanate`` / ``ask`` / ``reclaim``
    invocation.
    """
    return _kwargs_adapter_dict(_daemon.make_handler(agent))


def email_comm_bundle_host(agent: "BaseAgent") -> "NativeBundleHost":
    """Host the real ``email`` tool through the SDK ``email`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.NativeBundleHost` for
    ``email``, hosting the bundle's one declared tool with the wrapper's genuine
    handler (the kernel intrinsic, bound to *agent*). The SDK is imported here, in
    the wrapper, not at SDK module load — preserving the one-way ``wrapper -> sdk``
    direction. ``host.invoke("email", action=..., **args)`` runs the real mail
    logic through the declared manifest without altering the agent's live
    intrinsic registration.
    """
    from lingtai_sdk.communication_tools import email_comm_host

    return email_comm_host(email_comm_handler(agent))


def daemon_exec_bundle_host(agent: "BaseAgent") -> "BundleHost":
    """Host the real ``daemon`` tool through the SDK ``daemon`` bundle.

    Returns a :class:`~lingtai_sdk.capability_host.BundleHost` for ``daemon``,
    hosting the bundle's one declared tool with the wrapper's genuine handler
    (``DaemonManager.handle`` bound to *agent*). The SDK is imported here, in the
    wrapper, not at SDK module load. ``host.invoke("daemon", action=..., **args)``
    runs the real subagent-dispatch logic through the declared manifest without
    altering the agent's live capability registration — and constructing the host
    spawns / kills nothing.
    """
    from lingtai_sdk.communication_tools import daemon_exec_host

    return daemon_exec_host(daemon_exec_handler(agent))


def communication_bundle_hosts(
    agent: "BaseAgent",
) -> dict[str, "BundleHost | NativeBundleHost"]:
    """Host the real ``email`` and ``daemon`` tools in the ``{name: host}`` shape.

    The mapping mirror of the per-bundle bridges, parallel to
    ``system_bundle.system_lifecycle_bundle_hosts`` /
    ``file_bundle.file_tool_bundle_hosts``, so the wrapper bridge exposes the same
    ``{name: host}`` shape across all stages. Builds via the SDK
    ``communication_tool_hosts`` mapping seam, which enforces the single-handler-
    per-declared-surface contract. ``email`` is hosted natively and ``daemon``
    in-process, each matching its live carrier.
    """
    from lingtai_sdk.communication_tools import (
        DAEMON_TOOL_NAME,
        EMAIL_TOOL_NAME,
        communication_tool_hosts,
    )

    return communication_tool_hosts(
        {
            EMAIL_TOOL_NAME: email_comm_handler(agent),
            DAEMON_TOOL_NAME: daemon_exec_handler(agent),
        }
    )


__all__ = [
    "email_comm_handler",
    "daemon_exec_handler",
    "email_comm_bundle_host",
    "daemon_exec_bundle_host",
    "communication_bundle_hosts",
]
