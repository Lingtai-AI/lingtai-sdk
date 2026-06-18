"""The ``avatar_spawn`` / ``avatar_rules`` peer-spawn bundle declarations + in-process
host seams (stage 3I).

The **peer-agent-spawning** counterpart of the ``daemon`` child-subagent surface
(stage 3D) and the ``bash`` arbitrary-shell surface (stage 3H). Where
:mod:`lingtai_sdk.communication_tools` declares ``daemon`` (which spawns *ephemeral
child* subagents the parent owns and reclaims) and :mod:`lingtai_sdk.bash_tools`
declares ``bash`` (arbitrary host-shell execution), this module declares the
agent's **independent-peer-spawning** surface — ``avatar`` (分身): it launches a
*fully detached* peer agent process whose existence does **not** depend on the
parent, and distributes network-wide ``.rules`` to the whole avatar subtree.

Two tools, not one tool with an ``action`` — why this bundle differs
--------------------------------------------------------------------
Unlike ``daemon`` / ``bash`` / ``email`` (a single public tool with an ``action``
discriminator), the live avatar capability registers **two separate public tools**,
each with its own simple top-level ``type: object`` schema and ordinary
``required`` fields:

* ``avatar_spawn`` — spawn a new avatar (``shallow`` 初生 or ``deep`` 二重身) as a
  detached process; supports ``dry_run`` (preview-only, no mutation) and
  ``confirm`` (acknowledge the mission-quality gate).
* ``avatar_rules`` — set rules content and distribute it via ``.rules`` signal
  files to self + every descendant in the avatar tree (admin-gated).

They are deliberately split (see ``lingtai.core.avatar.get_schema``) so both
schemas stay simple top-level objects — some OpenAI-compatible strict tool
validators reject top-level JSON Schema combinators such as ``allOf``. This module
mirrors that shape: it declares **two** manifests (like the ``email``/``daemon``
pair in :mod:`communication_tools`), one per tool, each carrying its own schema
copy. Because each tool *is* its own surface (no ``action`` enum), the risk grading
is **per-tool** (:data:`AVATAR_TOOL_RISK`), not per-action.

Per-tool risk + a per-args refinement for ``avatar_spawn``
----------------------------------------------------------
Both tools are graded ``DESTRUCTIVE`` at the tool level — the conservative,
faithful encoding of what they do:

* ``avatar_spawn`` (``DESTRUCTIVE``) — launches an **independent detached process**
  (``lingtai-agent run <dir>``), creating a sibling working directory and (for
  ``deep``) copying identity + knowledge. A real, externally-visible process side
  effect, the same posture as ``daemon``'s ``emanate`` and ``bash``'s ``run``.
* ``avatar_rules`` (``DESTRUCTIVE``) — mutates the network-wide rules and writes a
  ``.rules`` signal file to self **and every descendant** in the avatar subtree,
  changing the behavior of *other live agents*. Admin-gated in the live handler; a
  broad, lasting, multi-agent side effect, graded ``DESTRUCTIVE``.

The bundle-level posture of each manifest equals its (single) tool's grade:
``DESTRUCTIVE``.

In addition, :func:`avatar_spawn_risk` offers a **per-args** refinement that the
stage-17 guard bridge (or any host) may consult: a ``dry_run=true`` ``avatar_spawn``
call is graded ``SAFE`` — the live handler short-circuits **before** any working
dir is created or any process launched, returning only a preview (see
``lingtai.core.avatar._spawn``'s dry-run short-circuit). This mirrors the
faithful, conservative spirit of the per-action tables (grade what the call
actually does), while the *tool-level* default stays ``DESTRUCTIVE`` so an
unqualified ``avatar_spawn`` is never under-stated. Like every stage-3 risk helper,
the per-args helper and :func:`avatar_tool_risk` fail safe **high** (an unknown
tool grades ``DESTRUCTIVE``).

What this module is NOT
-----------------------
Exactly as in the prior stages, it does **not** migrate, move, rewrite, import, or
call the real ``avatar`` implementation. The real handlers are the ``handle_spawn``
/ ``handle_rules`` methods of an :class:`~lingtai.core.avatar.AvatarManager` built
by ``lingtai.core.avatar.make_manager(agent)`` (bound to a live parent ``agent``);
importing them here would break SDK import-purity (the SDK must not eagerly pull
the wrapper) and is unnecessary — this module ships *declarations + injection
seams* only:

    avatar_spawn manifest -> avatar_spawn_host(handler)  # wrapper injects make_spawn_handler(agent)
    avatar_rules manifest -> avatar_rules_host(handler)  # wrapper injects make_rules_handler(agent)
       -> host.invoke(name, **args)                      # runs the wrapper capability's dispatch

The wrapper-side bridge that supplies the handlers lives in
``lingtai.core.avatar_bundle`` (the wrapper *may* import the SDK and the wrapper
capability; the SDK must not import either). The tool **schemas and behavior are
unchanged**: the bridge reuses ``avatar.make_manager`` verbatim, and the live
``setup()`` registration path is untouched.

This stage wires **nothing** into live tool dispatch and installs **no** guard on
any agent — it is purely additive declarations + injection seams, the
independent-peer-spawning mirror of the prior bundles.
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

#: The two public tool names this module is about.
AVATAR_SPAWN_TOOL_NAME = "avatar_spawn"
AVATAR_RULES_TOOL_NAME = "avatar_rules"

# --- declared argument schemas (structural copies, descriptions i18n'd live) --

# Language-neutral copies of the shapes returned by
# ``lingtai.core.avatar.get_schema`` (``avatar_spawn``) and
# ``lingtai.core.avatar.get_rules_schema`` (``avatar_rules``). The wrapper's own
# ``get_schema(lang)`` / ``get_rules_schema(lang)`` remain the registration path;
# these copies live in the manifest metadata so a host inspecting the manifest can
# see the argument contract without importing the wrapper capability. Descriptions
# are i18n'd at registration time and intentionally omitted here.
_AVATAR_SPAWN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": ["shallow", "deep"]},
        "comment": {"type": "string"},
        "dry_run": {"type": "boolean"},
        "confirm": {"type": "boolean"},
    },
    "required": ["name"],
}

_AVATAR_RULES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rules_content": {"type": "string"},
    },
    "required": ["rules_content"],
}


# --- per-tool risk table ------------------------------------------------------

#: Per-tool danger grading. Unlike ``daemon`` / ``bash`` / ``email`` (one tool with
#: an ``action`` discriminator), the avatar capability is **two separate public
#: tools**, each its own surface — so the table is keyed by *tool name*, not action.
#: The conservative, faithful encoding of what each tool does:
#:
#: * **independent peer process spawn** (``DESTRUCTIVE``) — ``avatar_spawn`` launches
#:   a fully detached ``lingtai-agent run <dir>`` peer (分身), creating a sibling
#:   working dir and, for ``deep``, copying identity + knowledge. An externally
#:   visible process side effect — the same posture as ``daemon``'s ``emanate`` and
#:   ``bash``'s ``run``.
#: * **network-wide rules mutation** (``DESTRUCTIVE``) — ``avatar_rules`` mutates the
#:   rules and writes a ``.rules`` signal to self **and every descendant** in the
#:   avatar subtree, changing the behavior of other live agents. Admin-gated in the
#:   live handler; a broad, lasting, multi-agent side effect.
#:
#: A per-args refinement for ``avatar_spawn`` (``dry_run=true`` → ``SAFE``) lives in
#: :func:`avatar_spawn_risk`; the tool-level default here stays ``DESTRUCTIVE`` so an
#: unqualified spawn is never under-stated.
AVATAR_TOOL_RISK: dict[str, SecurityDanger] = {
    # independent detached peer-process spawn
    AVATAR_SPAWN_TOOL_NAME: SecurityDanger.DESTRUCTIVE,
    # network-wide rules mutation distributed to self + all descendants
    AVATAR_RULES_TOOL_NAME: SecurityDanger.DESTRUCTIVE,
}

#: The avatar tools that spawn a process or mutate the whole subtree — a
#: declaration of the externally/multi-agent-visible side effects of the bundle
#: (the live spawn / rules distribution is the ``AvatarManager``'s, not here). Both
#: tools qualify, so this is the full tool set; it exists for symmetry with the
#: ``DAEMON_PROCESS_ACTIONS`` / ``BASH_PROCESS_ACTIONS`` attention subsets.
AVATAR_SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {AVATAR_SPAWN_TOOL_NAME, AVATAR_RULES_TOOL_NAME}
)


def avatar_tool_risk(tool: str) -> SecurityDanger:
    """Return the declared per-tool danger grade for an avatar tool name.

    Looks the tool up in :data:`AVATAR_TOOL_RISK`. An **unknown** tool is graded
    conservatively as :attr:`SecurityDanger.DESTRUCTIVE` rather than silently
    treated as safe — the same fail-safe-*high* direction the other stage-3 risk
    helpers use (``daemon``, ``bash``, ``mcp``, ``system``, ``email``, and
    ``knowledge`` unknowns also grade ``DESTRUCTIVE``). Pure declaration helper;
    gates nothing, never raises.
    """
    return AVATAR_TOOL_RISK.get(tool, SecurityDanger.DESTRUCTIVE)


def avatar_spawn_risk(args: Mapping[str, Any] | None = None) -> SecurityDanger:
    """Return the per-args danger grade for an ``avatar_spawn`` call.

    A faithful, conservative *per-args* refinement of the tool-level grade: a
    ``dry_run=true`` spawn is graded :attr:`SecurityDanger.SAFE` because the live
    handler short-circuits **before** any working dir is created or process
    launched, returning only a preview (see ``lingtai.core.avatar._spawn``'s
    dry-run short-circuit). Any other ``avatar_spawn`` call — including one with no
    args, a falsy ``dry_run``, or a non-bool ``dry_run`` — grades ``DESTRUCTIVE``,
    so an unqualified spawn is never under-stated. This mirrors the per-action
    tables' spirit (grade what the call actually does) while keeping the fail-safe
    *high* default. Pure declaration helper; gates nothing, never raises.
    """
    if args is not None and args.get("dry_run") is True:
        return SecurityDanger.SAFE
    return SecurityDanger.DESTRUCTIVE


def _avatar_spawn_manifest_builder() -> BundleManifest:
    """Build the ``avatar_spawn`` peer-spawn bundle manifest.

    Carried ``in_process`` via the wrapper capability ``setup()`` path (the same
    mechanism the file tools, ``daemon``, ``mcp``, ``knowledge``, and ``bash`` use),
    so it is **non-privileged** and freely ``REPLACEABLE``. The bundle-level posture
    is ``destructive`` (it launches an independent detached process); the per-args
    grading lives in :func:`avatar_spawn_risk`. The metadata is non-secret
    description only. **Manifest only** — the real handler
    (``avatar.make_spawn_handler`` bound to a parent agent) is injected by the
    wrapper bridge.
    """
    return BundleManifest(
        name=AVATAR_SPAWN_TOOL_NAME,
        version="0.0.1",
        summary="Spawn an independent peer agent (avatar 分身) as a fully detached "
        "process — shallow (初生) or deep (二重身); supports dry_run preview.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(AVATAR_SPAWN_TOOL_NAME,)),
        security=SecurityPolicy(
            danger=AVATAR_TOOL_RISK[AVATAR_SPAWN_TOOL_NAME].value
        ),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "execution": True,
            "side_effect": True,
            "process_spawning": True,
            "spawns_independent_peer": True,
            "supports_dry_run": True,
            "role": "The agent's independent-peer (avatar 分身) spawning surface.",
            "schema": dict(_AVATAR_SPAWN_SCHEMA),
        },
    )


def _avatar_rules_manifest_builder() -> BundleManifest:
    """Build the ``avatar_rules`` rules-distribution bundle manifest.

    Carried ``in_process`` via the wrapper capability ``setup()`` path, so it is
    **non-privileged** and freely ``REPLACEABLE``. The bundle-level posture is
    ``destructive`` (it mutates the network-wide rules and signals self + every
    descendant — a broad, multi-agent side effect; the live handler is
    admin-gated). The metadata is non-secret description only. **Manifest only** —
    the real handler (``avatar.make_rules_handler`` bound to a parent agent) is
    injected by the wrapper bridge.
    """
    return BundleManifest(
        name=AVATAR_RULES_TOOL_NAME,
        version="0.0.1",
        summary="Set rules content and distribute it via .rules signal files to "
        "self and every descendant in the avatar subtree (admin-gated).",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=(AVATAR_RULES_TOOL_NAME,)),
        security=SecurityPolicy(
            danger=AVATAR_TOOL_RISK[AVATAR_RULES_TOOL_NAME].value
        ),
        transport=TransportSpec(kind=TransportKind.IN_PROCESS.value),
        metadata={
            "execution": True,
            "side_effect": True,
            "distributes_to_descendants": True,
            "admin_gated": True,
            "role": "The agent's network-wide rules-distribution surface.",
            "schema": dict(_AVATAR_RULES_SCHEMA),
        },
    )


def avatar_spawn_manifest() -> BundleManifest:
    """The ``avatar_spawn`` peer-spawn bundle manifest — the agent's 分身 surface."""
    return _avatar_spawn_manifest_builder()


def avatar_rules_manifest() -> BundleManifest:
    """The ``avatar_rules`` rules-distribution bundle manifest."""
    return _avatar_rules_manifest_builder()


# Stable, canonical order for the two avatar bundles.
_AVATAR_BUILDERS = (avatar_spawn_manifest, avatar_rules_manifest)


def avatar_tool_manifests() -> tuple[BundleManifest, ...]:
    """The two avatar manifests in stable order: avatar_spawn, avatar_rules."""
    return tuple(builder() for builder in _AVATAR_BUILDERS)


def avatar_tool_names() -> tuple[str, ...]:
    """The two tool names in stable order: ``("avatar_spawn", "avatar_rules")``."""
    return tuple(m.name for m in avatar_tool_manifests())


def is_avatar_spawn_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``avatar_spawn`` bundle by name."""
    return manifest.name == AVATAR_SPAWN_TOOL_NAME


def is_avatar_rules_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is the ``avatar_rules`` bundle by name."""
    return manifest.name == AVATAR_RULES_TOOL_NAME


def is_avatar_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is either avatar bundle (spawn or rules) by name."""
    return manifest.name in (AVATAR_SPAWN_TOOL_NAME, AVATAR_RULES_TOOL_NAME)


def avatar_spawn_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``avatar_spawn`` bundle from an injected handler.

    The peer-spawn mirror of :func:`~lingtai_sdk.communication_tools.daemon_exec_host`
    and :func:`~lingtai_sdk.bash_tools.bash_exec_host`: ``avatar_spawn`` is an
    in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``avatar_spawn`` handler callable (the real ``AvatarManager.handle_spawn`` from
    ``avatar.make_spawn_handler(agent)``, which the wrapper bridge injects), returns
    a host of the one declared ``avatar_spawn`` tool. This shim never imports or
    calls the real implementation, and constructing the host spawns no process and
    writes no file — only an explicit ``host.invoke("avatar_spawn", name=...)``
    would, which the wrapper bridge gates exactly as the live path does (the
    mission-quality gate, name validation, and ``dry_run`` short-circuit).

    The declared ``danger`` posture (bundle-level ``destructive`` plus the
    :func:`avatar_spawn_risk` per-args grading) is **not** enforced here: a host
    runs whatever handler it is given. ``BundleHost`` enforces the non-privileged /
    ``in_process`` contract; danger gating is the stage-17 guard bridge's job.
    """
    if not callable(handler):
        raise BundleHostError(
            f"avatar_spawn bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(
        avatar_spawn_manifest(), {AVATAR_SPAWN_TOOL_NAME: handler}
    )


def avatar_rules_host(handler: ToolHandler) -> BundleHost:
    """Build an in-process host for the ``avatar_rules`` bundle from an injected handler.

    The rules-distribution mirror of :func:`avatar_spawn_host`: ``avatar_rules`` is
    an in-process wrapper capability, so its host is a non-native
    :class:`~lingtai_sdk.capability_host.BundleHost`. Given the single *supplied*
    ``avatar_rules`` handler callable (the real ``AvatarManager.handle_rules`` from
    ``avatar.make_rules_handler(agent)``, which the wrapper bridge injects), returns
    a host of the one declared ``avatar_rules`` tool. This shim never imports or
    calls the real implementation, and constructing the host writes nothing — only
    an explicit ``host.invoke("avatar_rules", rules_content=...)`` would, gated by
    the live handler's admin check exactly as the live path does.
    """
    if not callable(handler):
        raise BundleHostError(
            f"avatar_rules bundle requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    return BundleHost(
        avatar_rules_manifest(), {AVATAR_RULES_TOOL_NAME: handler}
    )


def avatar_tool_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, BundleHost]:
    """Build ``{name: host}`` for the two avatar bundles.

    The mapping mirror of the per-bundle host seams, parallel to
    :func:`~lingtai_sdk.communication_tools.communication_tool_hosts`, so the
    wrapper bridge has the same ``{name: host}`` shape across all stages.
    ``handlers`` must contain exactly the ``avatar_spawn`` and ``avatar_rules``
    handlers — a missing handler or any handler for a non-avatar name raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial / typo'd wiring can
    never silently host the wrong surface. Both are hosted in-process, matching
    their live carrier.
    """
    expected = set(avatar_tool_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for avatar bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-avatar bundle name(s): {sorted(extra)}"
        )
    return {
        AVATAR_SPAWN_TOOL_NAME: avatar_spawn_host(handlers[AVATAR_SPAWN_TOOL_NAME]),
        AVATAR_RULES_TOOL_NAME: avatar_rules_host(handlers[AVATAR_RULES_TOOL_NAME]),
    }


__all__ = [
    "AVATAR_SPAWN_TOOL_NAME",
    "AVATAR_RULES_TOOL_NAME",
    "AVATAR_TOOL_RISK",
    "AVATAR_SIDE_EFFECT_TOOLS",
    "avatar_tool_risk",
    "avatar_spawn_risk",
    "avatar_spawn_manifest",
    "avatar_rules_manifest",
    "avatar_tool_manifests",
    "avatar_tool_names",
    "is_avatar_spawn_manifest",
    "is_avatar_rules_manifest",
    "is_avatar_manifest",
    "avatar_spawn_host",
    "avatar_rules_host",
    "avatar_tool_hosts",
]
