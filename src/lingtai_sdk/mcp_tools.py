"""The ``mcp`` tool-config/catalog bundle declaration + an in-process host seam (stage 3F).

The **tool-config / catalog** counterpart of the file/lifecycle/communication
bundle templates. Where :mod:`lingtai_sdk.communication_tools` declares the
high-state ``email`` / ``daemon`` surfaces (external / process side effects), this
module declares the single low-side-effect ``mcp`` surface — the agent's
**per-agent MCP-server registry view**.

Why ``mcp`` is here now (it was excluded in stage 3D)
-----------------------------------------------------
At stage 3D ``mcp`` was *excluded* on two grounds: its live handler was an inline
closure inside ``setup()`` with no stable extractable seam, and its single
``show`` action is read-only presentation. Stage 3F resolves the first: the
wrapper now exposes a stable ``lingtai.core.mcp.make_handler(agent)`` factory (the
single source of truth ``setup()`` itself registers), so the surface has an
extractable seam exactly like ``daemon.make_handler``. The read-only nature is
preserved here — it is precisely *why* this is the lowest-risk high-altitude
surface to bridge, and why the bundle declares ``SecurityDanger.SAFE``.

Carrier and host class — consistent with the live wiring
--------------------------------------------------------
``mcp`` is a **wrapper capability** registered live by its ``setup()`` through
``agent.add_tool`` — the *same* non-native, in-process registration path the file
tools and ``daemon`` use, **not** a kernel intrinsic. So, like ``daemon`` (and
unlike the native-carried ``email`` / ``system``), it declares ``in_process``
transport + ``privileged=False`` and is hosted by the non-native
:class:`~lingtai_sdk.capability_host.BundleHost`, mirroring how the live ``setup()``
path carries it.

Read-only, but **agent-state-sensitive** — why an in-process host, not an
out-of-process stdio/http transport: the ``show`` action does more than return
static catalog text. It re-reads the on-disk ``mcp_registry.jsonl`` and
**re-renders the agent's ``mcp`` system-prompt section** (``_reconcile`` calls
``agent.update_system_prompt``) and reports a runtime health snapshot bound to
``agent._working_dir``. That state coupling is why it rides the in-process
capability carrier (the handler closes over a live ``agent``) rather than an
out-of-process transport a stateless catalog server could use. It is read-only
(no registry write, no MCP server start) yet not a pure static read — the
``configuration`` / ``catalog`` posture with an in-process, agent-bound seam.

Single tool, single action — a per-action risk table for symmetry
-----------------------------------------------------------------
``mcp`` is a single public tool with an ``action`` discriminator whose live enum
is **``show`` only** (see ``lingtai.core.mcp.get_schema``). As with the other
stage-3 bundles, the action grade ships as a small :data:`MCP_ACTION_RISK` table
(``show`` → ``SAFE``) plus an :func:`mcp_action_risk` helper that fails safe
*high* (an unknown action grades ``DESTRUCTIVE``, never silently ``SAFE``) — a
declaration the stage-17 guard bridge may read, never a second gate. The
bundle-level posture equals the strongest action's grade: ``SAFE``.

What this module is NOT
-----------------------
Exactly as in the prior stages, it does **not** migrate, move, rewrite, import, or
call the real ``mcp`` implementation. The real handler is a wrapper capability
closure built by ``lingtai.core.mcp.make_handler(agent)`` (bound to a live
``agent``); importing it here would break SDK import-purity (the SDK must not
eagerly pull the wrapper) and is unnecessary — this module ships *declarations +
an injection seam* only:

    mcp manifest -> mcp_config_host(handler)   # wrapper injects the real make_handler(agent)
       -> host.invoke("mcp", **args)            # runs the wrapper capability's existing dispatch

The wrapper-side bridge that supplies the handler lives in
``lingtai.core.mcp_bundle`` (the wrapper *may* import the SDK and the kernel/
wrapper capability; the SDK must not import either). The tool **schema and
behavior are unchanged**: the bridge reuses ``mcp.make_handler`` verbatim, and the
live ``setup()`` registration path is untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam, the
tool-config/catalog mirror of the prior bundles.
"""
from __future__ import annotations

from typing import Any, Mapping

from .capabilities import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityDanger,
    SecurityPolicy,
    TransportKind,
    TransportSpec,
)
from .capability_host import BundleHost, ToolHandler
from .errors import BundleHostError

#: The one public tool-config/catalog tool this module is about.
MCP_TOOL_NAME = "mcp"

# --- declared argument schema (structural copy, descriptions i18n'd live) -----

# Language-neutral copy of the action enum returned by
# ``lingtai.core.mcp.get_schema``. The wrapper's own ``get_schema()`` remains the
# registration path; this copy lives in the manifest metadata so a host inspecting
# the manifest can see the argument contract without importing the wrapper
# capability. Descriptions are intentionally omitted here.
_MCP_ACTIONS: tuple[str, ...] = ("show",)

_MCP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_MCP_ACTIONS)},
    },
    "required": ["action"],
}


# --- per-action risk table ----------------------------------------------------

#: Per-action danger grading for the single ``mcp`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what the one live action
#: does:
#:
#: * **read-only registry view** (``SAFE``) — ``show`` re-reads
#:   ``mcp_registry.jsonl``, re-renders the agent's ``mcp`` system-prompt section,
#:   and returns a health snapshot. No registry write, no MCP server start, no
#:   external or process side effect. Agent-state-sensitive (it touches the
#:   rendered prompt section) but a read/presentation surface — hence ``SAFE``.
#:
#: The bundle-level posture equals the strongest action's grade: ``SAFE``.
MCP_ACTION_RISK: dict[str, SecurityDanger] = {
    "show": SecurityDanger.SAFE,
}


def _strongest(grades: Mapping[str, SecurityDanger]) -> SecurityDanger:
    """Return the strongest (highest-risk) danger grade in *grades*.

    The bundle-level posture is the strongest of its per-action grades, so a
    single bundle danger can never under-state any one action. Ordering is
    SAFE < CAUTION < DESTRUCTIVE.
    """
    order = {
        SecurityDanger.SAFE: 0,
        SecurityDanger.CAUTION: 1,
        SecurityDanger.DESTRUCTIVE: 2,
    }
    return max(grades.values(), key=lambda d: order[d])


def mcp_config_manifest() -> BundleManifest:
    """The ``mcp`` tool-config/catalog bundle manifest — the agent's MCP registry view.

    Declares the single public ``mcp`` tool (action ``show`` only): re-read the
    per-agent ``mcp_registry.jsonl``, re-render the ``mcp`` system-prompt section,
    and return the umbrella manual body plus a runtime health snapshot. Carried
    ``in_process`` via the wrapper capability ``setup()`` path (the same mechanism
    the file tools and ``daemon`` use), so it is **non-privileged** and freely
    ``REPLACEABLE``. The bundle-level posture is ``safe`` (the strongest — and only
    — action ``show`` is a read-only registry view); the per-action grading lives
    in :data:`MCP_ACTION_RISK`. The metadata is non-secret description only.
    **Manifest only** — the real handler (``mcp.make_handler`` bound to an agent)
    is injected by the wrapper bridge.
    """
    return BundleManifest(
        name=MCP_TOOL_NAME,
        version="0.0.1",
        summary="View the per-agent MCP-server registry: re-render the prompt "
        "section and return the manual plus a runtime health snapshot.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(MCP_TOOL_NAME,)),
        security=SecurityPolicy(danger=_strongest(MCP_ACTION_RISK).value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "config": True,
            "catalog": True,
            "read_only": True,
            "agent_state_sensitive": True,
            "role": "The agent's per-agent MCP-server registry view.",
            "actions": list(_MCP_ACTIONS),
            "schema": dict(_MCP_SCHEMA),
        },
    )


def mcp_config_manifests() -> tuple[BundleManifest, ...]:
    """The tool-config manifests in stable order: just ``("mcp",)`` for now."""
    return (mcp_config_manifest(),)


def mcp_config_names() -> tuple[str, ...]:
    """The tool-config tool names in stable order: ``("mcp",)``."""
    return tuple(m.name for m in mcp_config_manifests())


def is_mcp_config_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``mcp`` tool-config bundle by name."""
    return manifest.name == MCP_TOOL_NAME


def mcp_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for an ``mcp`` ``action``.

    Looks the action up in :data:`MCP_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than silently
    treated as safe — the same fail-safe-*high* direction the other stage-3 risk
    tables use (``system``, ``email``, and ``daemon`` unknown actions also grade
    as ``DESTRUCTIVE``). Pure declaration helper; gates nothing, never raises.
    """
    return MCP_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def mcp_config_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``mcp`` bundle from an injected handler.

    The tool-config/catalog mirror of
    :func:`~lingtai_sdk.communication_tools.daemon_exec_host`: ``mcp`` is an
    in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``mcp`` handler callable (the real ``mcp.make_handler(agent)`` closure, which
    the wrapper bridge injects), returns a host of the one declared ``mcp`` tool.
    This shim never imports or calls the real implementation, and constructing the
    host writes nothing to the registry and starts no MCP server — only an explicit
    ``host.invoke("mcp", action="show")`` re-reconciles, exactly as the live path
    does.

    The declared ``danger`` posture (bundle-level ``safe`` plus the
    :data:`MCP_ACTION_RISK` grading) is **not** enforced here: a host runs whatever
    handler it is given. ``BundleHost`` enforces the non-privileged / ``in_process``
    contract; danger gating is the stage-17 guard bridge's job.
    """
    if not callable(handler):
        raise BundleHostError(
            f"mcp config bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(mcp_config_manifest(), {MCP_TOOL_NAME: handler})


def mcp_config_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{"mcp": BundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`mcp_config_host`, parallel to
    :func:`~lingtai_sdk.communication_tools.communication_tool_hosts` /
    :func:`~lingtai_sdk.lifecycle_tools.system_lifecycle_hosts`, so the wrapper
    bridge has the same ``{name: host}`` shape across all stages. The mapping must
    contain exactly the ``mcp`` handler — a missing ``mcp`` handler or any handler
    for a non-``mcp`` name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial/typo'd wiring can
    never silently host the wrong surface.
    """
    expected = set(mcp_config_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for mcp config bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-mcp config bundle name(s): {sorted(extra)}"
        )
    return {MCP_TOOL_NAME: mcp_config_host(handlers[MCP_TOOL_NAME])}


__all__ = [
    "MCP_TOOL_NAME",
    "MCP_ACTION_RISK",
    "mcp_config_manifest",
    "mcp_config_manifests",
    "mcp_config_names",
    "is_mcp_config_manifest",
    "mcp_action_risk",
    "mcp_config_host",
    "mcp_config_hosts",
]
