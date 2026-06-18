"""The ``knowledge`` catalog bundle declaration + an in-process host seam (stage 3G).

The **private-memory catalog** counterpart of the ``mcp`` registry-view bundle
(stage 3F). Where :mod:`lingtai_sdk.mcp_tools` declares the agent's per-agent
MCP-server registry view, this module declares the single bounded-side-effect
``knowledge`` surface — the agent's **private, durable knowledge catalog** view
(the structurally isomorphic, physically separate sibling of the ``skills``
catalog declared in :mod:`lingtai_sdk.skill_tools`).

Carrier and host class — consistent with the live wiring
--------------------------------------------------------
``knowledge`` is a **wrapper capability** registered live by its ``setup()``
through ``agent.add_tool`` — the *same* non-native, in-process registration path
the file tools, ``daemon``, and ``mcp`` use, **not** a kernel intrinsic. So, like
``mcp``, it declares ``in_process`` transport + ``privileged=False`` and is hosted
by the non-native :class:`~lingtai_sdk.capability_host.BundleHost`, mirroring how
the live ``setup()`` path carries it.

Bounded-side-effect and **agent-state-sensitive** — why an in-process host, not an
out-of-process transport: the ``info`` action does more than return static text.
It re-scans ``<agent>/knowledge/`` and **re-renders the agent's ``knowledge``
system-prompt section** (``_reconcile`` calls ``agent.update_system_prompt``) and
reports a runtime health snapshot bound to ``agent._working_dir``. That state
coupling is why it rides the in-process capability carrier (the handler closes
over a live ``agent``) rather than an out-of-process transport a stateless catalog
server could use. It is normally read-only of the agent's *authored* knowledge, but the live
``_reconcile`` path still owns a one-time legacy-JSON migration that can write
migrated ``KNOWLEDGE.md`` entries and rename the legacy JSON file. That bounded
filesystem side effect is why the action is graded ``CAUTION`` — the ``configuration`` / ``catalog`` posture with an
in-process, agent-bound seam, exactly like ``mcp``.

Single tool, single action — a per-action risk table for symmetry
-----------------------------------------------------------------
``knowledge`` is a single public tool with an ``action`` discriminator whose live
enum is **``info`` only** (see ``lingtai.core.knowledge.get_schema``). As with the
other stage-3 bundles, the action grade ships as a small
:data:`KNOWLEDGE_ACTION_RISK` table (``info`` → ``CAUTION``) plus a
:func:`knowledge_action_risk` helper that fails safe *high* (an unknown action
grades ``DESTRUCTIVE``, never silently ``SAFE``) — a declaration the stage-17 guard
bridge may read, never a second gate. The bundle-level posture equals the
strongest action's grade: ``CAUTION``.

What this module is NOT
-----------------------
Exactly as in the prior stages, it does **not** migrate, move, rewrite, import, or
call the real ``knowledge`` implementation. The real handler is a wrapper
capability closure built by ``lingtai.core.knowledge.make_handler(agent)`` (bound
to a live ``agent``); importing it here would break SDK import-purity (the SDK must
not eagerly pull the wrapper) and is unnecessary — this module ships
*declarations + an injection seam* only:

    knowledge manifest -> knowledge_catalog_host(handler)  # wrapper injects make_handler(agent)
       -> host.invoke("knowledge", **args)                 # runs the wrapper capability's dispatch

The wrapper-side bridge that supplies the handler lives in
``lingtai.core.knowledge_bundle`` (the wrapper *may* import the SDK and the
wrapper capability; the SDK must not import either). The tool **schema and
behavior are unchanged**: the bridge reuses ``knowledge.make_handler`` verbatim,
and the live ``setup()`` registration path is untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam, the catalog
mirror of the prior bundles.
"""
from __future__ import annotations

from typing import Any, Mapping

from .contracts import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityDanger,
    SecurityPolicy,
    TransportKind,
    TransportSpec,
)
from .host import BundleHost, ToolHandler
from ..errors import BundleHostError

#: The one public catalog tool this module is about.
KNOWLEDGE_TOOL_NAME = "knowledge"

# --- declared argument schema (structural copy, descriptions i18n'd live) -----

# Language-neutral copy of the action enum returned by
# ``lingtai.core.knowledge.get_schema``. The wrapper's own ``get_schema()`` remains
# the registration path; this copy lives in the manifest metadata so a host
# inspecting the manifest can see the argument contract without importing the
# wrapper capability. Descriptions are intentionally omitted here.
_KNOWLEDGE_ACTIONS: tuple[str, ...] = ("info",)

_KNOWLEDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_KNOWLEDGE_ACTIONS)},
    },
    "required": ["action"],
}


# --- per-action risk table ----------------------------------------------------

#: Per-action danger grading for the single ``knowledge`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what the one live action
#: does:
#:
#: * **catalog view with legacy-migration side effect** (``CAUTION``) — ``info`` re-scans
#:   ``<agent>/knowledge/``, re-renders the agent's ``knowledge`` system-prompt
#:   section, and returns a health snapshot. Normally no catalog write, no external or process side effect. However,
#:   the live ``_reconcile`` path can perform the one-time legacy JSON migration
#:   (write migrated ``KNOWLEDGE.md`` entries and rename the legacy JSON file) before
#:   rendering the catalog. That bounded filesystem side effect makes the faithful
#:   grade ``CAUTION`` rather than ``SAFE``.
#:
#: The bundle-level posture equals the strongest action's grade: ``CAUTION``.
KNOWLEDGE_ACTION_RISK: dict[str, SecurityDanger] = {
    "info": SecurityDanger.CAUTION,
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


def knowledge_catalog_manifest() -> BundleManifest:
    """The ``knowledge`` catalog bundle manifest — the agent's private-memory view.

    Declares the single public ``knowledge`` tool (action ``info`` only): re-scan
    ``<agent>/knowledge/``, re-render the ``knowledge`` system-prompt section, and
    return a runtime health snapshot. Carried ``in_process`` via the wrapper
    capability ``setup()`` path (the same mechanism the file tools, ``daemon``, and
    ``mcp`` use), so it is **non-privileged** and freely ``REPLACEABLE``. The
    bundle-level posture is ``caution`` (the strongest — and only — action ``info``
    can perform bounded legacy-migration writes before rendering the catalog); the per-action grading lives in
    :data:`KNOWLEDGE_ACTION_RISK`. The metadata is non-secret description only.
    **Manifest only** — the real handler (``knowledge.make_handler`` bound to an
    agent) is injected by the wrapper bridge.
    """
    return BundleManifest(
        name=KNOWLEDGE_TOOL_NAME,
        version="0.0.1",
        summary="View the per-agent private knowledge catalog: re-scan "
        "knowledge/, re-render the prompt section, and return a health snapshot.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(KNOWLEDGE_TOOL_NAME,)),
        security=SecurityPolicy(danger=_strongest(KNOWLEDGE_ACTION_RISK).value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "config": True,
            "catalog": True,
            "read_only": False,
            "migrates_legacy_json": True,
            "agent_state_sensitive": True,
            "role": "The agent's private, durable knowledge catalog view.",
            "actions": list(_KNOWLEDGE_ACTIONS),
            "schema": dict(_KNOWLEDGE_SCHEMA),
        },
    )


def knowledge_catalog_manifests() -> tuple[BundleManifest, ...]:
    """The catalog manifests in stable order: just ``("knowledge",)`` for now."""
    return (knowledge_catalog_manifest(),)


def knowledge_catalog_names() -> tuple[str, ...]:
    """The catalog tool names in stable order: ``("knowledge",)``."""
    return tuple(m.name for m in knowledge_catalog_manifests())


def is_knowledge_catalog_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``knowledge`` catalog bundle by name."""
    return manifest.name == KNOWLEDGE_TOOL_NAME


def knowledge_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``knowledge`` ``action``.

    Looks the action up in :data:`KNOWLEDGE_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than silently
    treated as safe — the same fail-safe-*high* direction the other stage-3 risk
    tables use (``mcp``, ``system``, ``email``, and ``daemon`` unknown actions also
    grade as ``DESTRUCTIVE``). Pure declaration helper; gates nothing, never raises.
    """
    return KNOWLEDGE_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def knowledge_catalog_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``knowledge`` bundle from an injected handler.

    The catalog mirror of :func:`~lingtai_sdk.mcp_tools.mcp_config_host`:
    ``knowledge`` is an in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``knowledge`` handler callable (the real ``knowledge.make_handler(agent)``
    closure, which the wrapper bridge injects), returns a host of the one declared
    ``knowledge`` tool. This shim never imports or calls the real implementation,
    and constructing the host writes nothing — only an explicit
    ``host.invoke("knowledge", action="info")`` re-reconciles, exactly as the live
    path does.

    The declared ``danger`` posture (bundle-level ``caution`` plus the
    :data:`KNOWLEDGE_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. ``BundleHost`` enforces the non-privileged /
    ``in_process`` contract; danger gating is the stage-17 guard bridge's job.
    """
    if not callable(handler):
        raise BundleHostError(
            f"knowledge catalog bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(knowledge_catalog_manifest(), {KNOWLEDGE_TOOL_NAME: handler})


def knowledge_catalog_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{"knowledge": BundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`knowledge_catalog_host`, parallel to
    :func:`~lingtai_sdk.mcp_tools.mcp_config_hosts`, so the wrapper bridge has the
    same ``{name: host}`` shape across all stages. The mapping must contain exactly
    the ``knowledge`` handler — a missing ``knowledge`` handler or any handler for a
    non-``knowledge`` name raises :class:`~lingtai_sdk.errors.BundleHostError`, so a
    partial/typo'd wiring can never silently host the wrong surface.
    """
    expected = set(knowledge_catalog_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for knowledge catalog bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-knowledge catalog bundle name(s): {sorted(extra)}"
        )
    return {KNOWLEDGE_TOOL_NAME: knowledge_catalog_host(handlers[KNOWLEDGE_TOOL_NAME])}


__all__ = [
    "KNOWLEDGE_TOOL_NAME",
    "KNOWLEDGE_ACTION_RISK",
    "knowledge_catalog_manifest",
    "knowledge_catalog_manifests",
    "knowledge_catalog_names",
    "is_knowledge_catalog_manifest",
    "knowledge_action_risk",
    "knowledge_catalog_host",
    "knowledge_catalog_hosts",
]
