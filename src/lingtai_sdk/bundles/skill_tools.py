"""The ``skills`` catalog bundle declaration + an in-process host seam (stage 3G).

The **skill catalog** counterpart of the ``knowledge`` private-memory bundle
(this same stage) and the ``mcp`` registry-view bundle (stage 3F). Where
:mod:`lingtai_sdk.knowledge_tools` declares the agent's *private* knowledge
catalog, this module declares the single low-side-effect ``skills`` surface — the
agent's **portable skill catalog** view (the structurally isomorphic, physically
separate sibling of ``knowledge``). The module is named ``skill_tools`` (singular)
to match the ``mcp_tools`` / ``communication_tools`` ``<domain>_tools`` convention
without the awkward ``skills_tools`` doubling; the declared *tool* name stays the
live ``skills`` (plural).

Carrier and host class — consistent with the live wiring
--------------------------------------------------------
``skills`` is a **wrapper capability** registered live by its ``setup()`` through
``agent.add_tool`` — the *same* non-native, in-process registration path the file
tools, ``daemon``, ``mcp``, and ``knowledge`` use, **not** a kernel intrinsic. So,
like ``knowledge``, it declares ``in_process`` transport + ``privileged=False`` and
is hosted by the non-native :class:`~lingtai_sdk.capability_host.BundleHost`,
mirroring how the live ``setup()`` path carries it.

Read-only, but **agent-state-sensitive** — why an in-process host, not an
out-of-process transport: the ``info`` action does more than return static text.
It re-scans ``.library/{intrinsic,custom}`` plus the configured Tier-1 ``paths``
and **re-renders the agent's ``skills`` system-prompt section** (``_reconcile``
calls ``agent.update_system_prompt``), reports a runtime health snapshot, and
returns the skills-manual body — all bound to ``agent._working_dir``. That state
coupling (and the Tier-1 ``paths`` the handler closes over) is why it rides the
in-process capability carrier rather than an out-of-process transport. It never
writes to ``.library/`` (installation is the Agent initializer's job) — the
``configuration`` / ``catalog`` posture with an in-process, agent-bound seam,
exactly like ``knowledge`` and ``mcp``.

Single tool, single action — a per-action risk table for symmetry
-----------------------------------------------------------------
``skills`` is a single public tool with an ``action`` discriminator whose live
enum is **``info`` only** (see ``lingtai.core.skills.get_schema``). As with the
other stage-3 bundles, the action grade ships as a small
:data:`SKILLS_ACTION_RISK` table (``info`` → ``SAFE``) plus a
:func:`skills_action_risk` helper that fails safe *high* (an unknown action grades
``DESTRUCTIVE``, never silently ``SAFE``) — a declaration the stage-17 guard bridge
may read, never a second gate. The bundle-level posture equals the strongest
action's grade: ``SAFE``.

What this module is NOT
-----------------------
Exactly as in the prior stages, it does **not** migrate, move, rewrite, import, or
call the real ``skills`` implementation. The real handler is a wrapper capability
closure built by ``lingtai.core.skills.make_handler(agent, paths)`` (bound to a
live ``agent`` and its Tier-1 ``paths``); importing it here would break SDK
import-purity (the SDK must not eagerly pull the wrapper) and is unnecessary —
this module ships *declarations + an injection seam* only:

    skills manifest -> skills_catalog_host(handler)  # wrapper injects make_handler(agent, paths)
       -> host.invoke("skills", **args)              # runs the wrapper capability's dispatch

The wrapper-side bridge that supplies the handler lives in
``lingtai.core.skills_bundle`` (the wrapper *may* import the SDK and the wrapper
capability; the SDK must not import either). The tool **schema and behavior are
unchanged**: the bridge reuses ``skills.make_handler`` verbatim, and the live
``setup()`` registration path is untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declaration + an injection seam, the skill-
catalog mirror of the prior bundles.
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

#: The one public catalog tool this module is about (the live tool name is plural).
SKILLS_TOOL_NAME = "skills"

# --- declared argument schema (structural copy, descriptions i18n'd live) -----

# Language-neutral copy of the action enum returned by
# ``lingtai.core.skills.get_schema``. The wrapper's own ``get_schema()`` remains the
# registration path; this copy lives in the manifest metadata so a host inspecting
# the manifest can see the argument contract without importing the wrapper
# capability. Descriptions are intentionally omitted here.
_SKILLS_ACTIONS: tuple[str, ...] = ("info",)

_SKILLS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(_SKILLS_ACTIONS)},
    },
    "required": ["action"],
}


# --- per-action risk table ----------------------------------------------------

#: Per-action danger grading for the single ``skills`` tool's ``action``
#: discriminator. The conservative, faithful encoding of what the one live action
#: does:
#:
#: * **read-only catalog view** (``SAFE``) — ``info`` re-scans ``.library/`` +
#:   the Tier-1 paths, re-renders the agent's ``skills`` system-prompt section, and
#:   returns the manual body plus a health snapshot. No catalog write (the
#:   capability is pure presentation — installation is the initializer's job), no
#:   external or process side effect. Agent-state-sensitive (it touches the
#:   rendered prompt section) but a read/presentation surface — hence ``SAFE``.
#:
#: The bundle-level posture equals the strongest action's grade: ``SAFE``.
SKILLS_ACTION_RISK: dict[str, SecurityDanger] = {
    "info": SecurityDanger.SAFE,
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


def skills_catalog_manifest() -> BundleManifest:
    """The ``skills`` catalog bundle manifest — the agent's skill-catalog view.

    Declares the single public ``skills`` tool (action ``info`` only): re-scan
    ``.library/`` + the Tier-1 paths, re-render the ``skills`` system-prompt
    section, and return the skills-manual body plus a runtime health snapshot.
    Carried ``in_process`` via the wrapper capability ``setup()`` path (the same
    mechanism the file tools, ``daemon``, ``mcp``, and ``knowledge`` use), so it is
    **non-privileged** and freely ``REPLACEABLE``. The bundle-level posture is
    ``safe`` (the strongest — and only — action ``info`` is a read-only catalog
    view); the per-action grading lives in :data:`SKILLS_ACTION_RISK`. The metadata
    is non-secret description only. **Manifest only** — the real handler
    (``skills.make_handler`` bound to an agent + its Tier-1 paths) is injected by
    the wrapper bridge.
    """
    return BundleManifest(
        name=SKILLS_TOOL_NAME,
        version="0.0.1",
        summary="View the per-agent skill catalog: re-scan .library/ + Tier-1 "
        "paths, re-render the prompt section, and return the manual plus a "
        "health snapshot.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(SKILLS_TOOL_NAME,)),
        security=SecurityPolicy(danger=_strongest(SKILLS_ACTION_RISK).value),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "config": True,
            "catalog": True,
            "read_only": True,
            "agent_state_sensitive": True,
            "role": "The agent's portable skill catalog view.",
            "actions": list(_SKILLS_ACTIONS),
            "schema": dict(_SKILLS_SCHEMA),
        },
    )


def skills_catalog_manifests() -> tuple[BundleManifest, ...]:
    """The catalog manifests in stable order: just ``("skills",)`` for now."""
    return (skills_catalog_manifest(),)


def skills_catalog_names() -> tuple[str, ...]:
    """The catalog tool names in stable order: ``("skills",)``."""
    return tuple(m.name for m in skills_catalog_manifests())


def is_skills_catalog_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``skills`` catalog bundle by name."""
    return manifest.name == SKILLS_TOOL_NAME


def skills_action_risk(action: str) -> SecurityDanger:
    """Return the declared per-action danger grade for a ``skills`` ``action``.

    Looks the action up in :data:`SKILLS_ACTION_RISK`. An **unknown** action is
    graded conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than silently
    treated as safe — the same fail-safe-*high* direction the other stage-3 risk
    tables use (``knowledge``, ``mcp``, ``system``, ``email``, and ``daemon``
    unknown actions also grade as ``DESTRUCTIVE``). Pure declaration helper; gates
    nothing, never raises.
    """
    return SKILLS_ACTION_RISK.get(action, SecurityDanger.DESTRUCTIVE)


def skills_catalog_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``skills`` bundle from an injected handler.

    The catalog mirror of :func:`~lingtai_sdk.knowledge_tools.knowledge_catalog_host`
    / :func:`~lingtai_sdk.mcp_tools.mcp_config_host`: ``skills`` is an in-process
    wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``skills`` handler callable (the real ``skills.make_handler(agent, paths)``
    closure, which the wrapper bridge injects), returns a host of the one declared
    ``skills`` tool. This shim never imports or calls the real implementation, and
    constructing the host writes nothing — only an explicit
    ``host.invoke("skills", action="info")`` re-reconciles, exactly as the live path
    does.

    The declared ``danger`` posture (bundle-level ``safe`` plus the
    :data:`SKILLS_ACTION_RISK` grading) is **not** enforced here: a host runs
    whatever handler it is given. ``BundleHost`` enforces the non-privileged /
    ``in_process`` contract; danger gating is the stage-17 guard bridge's job.
    """
    if not callable(handler):
        raise BundleHostError(
            f"skills catalog bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(skills_catalog_manifest(), {SKILLS_TOOL_NAME: handler})


def skills_catalog_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{"skills": BundleHost}`` from a single-entry handler mapping.

    The mapping mirror of :func:`skills_catalog_host`, parallel to
    :func:`~lingtai_sdk.knowledge_tools.knowledge_catalog_hosts` /
    :func:`~lingtai_sdk.mcp_tools.mcp_config_hosts`, so the wrapper bridge has the
    same ``{name: host}`` shape across all stages. The mapping must contain exactly
    the ``skills`` handler — a missing ``skills`` handler or any handler for a
    non-``skills`` name raises :class:`~lingtai_sdk.errors.BundleHostError`, so a
    partial/typo'd wiring can never silently host the wrong surface.
    """
    expected = set(skills_catalog_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for skills catalog bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-skills catalog bundle name(s): {sorted(extra)}"
        )
    return {SKILLS_TOOL_NAME: skills_catalog_host(handlers[SKILLS_TOOL_NAME])}


__all__ = [
    "SKILLS_TOOL_NAME",
    "SKILLS_ACTION_RISK",
    "skills_catalog_manifest",
    "skills_catalog_manifests",
    "skills_catalog_names",
    "is_skills_catalog_manifest",
    "skills_action_risk",
    "skills_catalog_host",
    "skills_catalog_hosts",
]
