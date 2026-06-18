"""Advisory-first wrapper wiring of the SDK guard bridge (stage 18, C3).

Stage 17 built a pure, import-light SDK adapter
(:mod:`lingtai_sdk.guard_bridge`) that turns one or more
:class:`~lingtai_sdk.capabilities.BundleManifest` objects into a kernel
:class:`~lingtai_kernel.tool_call_guard.ToolCallGuard` — but wired *nothing* into
a live agent. This module is the thin wrapper-layer seam that finally installs
such a guard onto the Stage-16 ``BaseAgent._tool_call_guard`` slot, so the turn
loop's ``ToolExecutor`` consults declared bundle posture before a tool is
dispatched.

Behaviour contract (deliberately advisory-first / fail-open)
------------------------------------------------------------
* **Default live mode is advisory.** :data:`DEFAULT_LIVE_GUARD_MODE` is
  :attr:`~lingtai_sdk.guard_bridge.GuardPolicyMode.ADVISORY`: a manifest-declared
  ``destructive`` tool is surfaced as a *warning*, never denied, in default live
  wiring. Blocking is reachable only by an explicit ``mode=`` opt-in and is never
  the wrapper default.
* **Default agents use the canonical SDK bundle registry.** Stage 3K adds a
  name/tool-indexed registry over all declared SDK bundles; default live wiring
  installs an advisory guard from that registry so every declared caution /
  destructive surface warns consistently. Safe tools still pass through cleanly.
* **Unknown / unmanifested tools fail open.** MCP tools, ``add_tool`` tools, and
  any tool without a declared SDK bundle are unknown to the bridge and pass
  through cleanly — this slice can only ever *add advisories* for explicitly
  declared surfaces, never block an undeclared tool.
* **No lifecycle/system tool is blocked.** Because the live mode is advisory,
  even a destructive core tool (e.g. ``system``) would only warn, never deny.
* **Fail open on any error.** If manifest collection or guard construction
  raises, the seam is left at its existing safe value rather than failing closed.

Import direction
----------------
The wrapper (``src/lingtai/...``) may import the SDK bridge/types; the kernel
must stay SDK-free. This module therefore imports
:mod:`lingtai_sdk.guard_bridge` (a wrapper→SDK edge, which is allowed), and the
kernel never imports it back.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from lingtai_kernel.tool_call_guard import ToolCallGuard
from lingtai_sdk.bundles.registry import default_registry
from lingtai_sdk.bundles.contracts import BundleManifest
from lingtai_sdk.guard.bridge import (
    GuardPolicyMode,
    tool_call_guard_from_manifests,
)

#: Private agent attribute set to ``True`` when this wrapper wiring has
#: installed a bundle-derived guard onto the agent's ``_tool_call_guard`` seam.
#: Used to distinguish a wrapper-owned guard from a host/subclass manually
#: installed one, so a later wiring call with *no* manifests can safely reset
#: only its own stale guard and never clobber a manual one.
PROVENANCE_FLAG = "_bundle_guard_installed"
#: Private agent attribute recording the names of the bundle manifests the
#: currently installed wrapper guard was derived from (provenance/debug only).
PROVENANCE_SOURCE = "_bundle_guard_source"

#: A capability→manifest provider maps an enabled capability name to a
#: zero-arg callable returning that capability's declared :class:`BundleManifest`.
ManifestProvider = Callable[[], BundleManifest]
ManifestRegistry = dict[str, ManifestProvider]

#: The wrapper's default live policy mode. Advisory-first: declared destructive
#: tools warn, they are never blocked by default live wiring.
DEFAULT_LIVE_GUARD_MODE: GuardPolicyMode = GuardPolicyMode.ADVISORY

#: Core intrinsic bundle names, kept in the same stable order as
#: ``lingtai_sdk.core_bundles.core_bundle_manifests()`` but sourced through the
#: Stage-3K canonical registry below so live guard wiring has one declared-set
#: source of truth.
CORE_BUNDLE_NAMES: tuple[str, str, str] = ("system", "psyche", "soul")


def default_manifest_registry() -> ManifestRegistry:
    """The default capability→manifest registry — still empty.

    No shipping *capability* declares an SDK bundle manifest yet, so capability
    manifests remain opt-in via this registry (or a caller-supplied one). Stage
    20 core manifests are collected through :func:`collect_core_bundle_manifests`
    instead, because ``system`` / ``psyche`` / ``soul`` are kernel intrinsics,
    not wrapper capabilities listed in ``_capabilities``.
    """
    return {}


def core_manifest_registry() -> ManifestRegistry:
    """The populated core capability→manifest registry.

    Compatibility view over the Stage-3K canonical bundle registry, restricted to
    the three privileged core surfaces (``system`` / ``psyche`` / ``soul``). The
    core surfaces are kernel *intrinsics* (always present, never listed in an
    agent's ``_capabilities``), so older callers that explicitly collect only the
    core manifests can keep using this named registry while the default live path
    now consumes :func:`collect_default_bundle_manifests`.
    """
    return {
        name: (lambda name=name: default_registry().get(name))
        for name in CORE_BUNDLE_NAMES
    }


def collect_core_bundle_manifests(
    agent: Any,
    *,
    registry: ManifestRegistry | None = None,
) -> list[BundleManifest]:
    """Collect the core bundle manifests directly.

    Unlike :func:`collect_agent_bundle_manifests`, this does **not** gate on the
    agent's ``_capabilities`` — the three core surfaces are kernel intrinsics
    that are always present (registered in
    ``lingtai_kernel.intrinsics.__init__``), never declared as wrapper
    capabilities. Stage 20 default wiring calls this seam unless
    ``include_core=False`` is passed to :func:`wire_agent_guard`.

    Fail-open like the capability collector: a provider that raises is skipped
    (logged via the agent's ``_log`` if available) rather than aborting
    collection, so one broken core manifest can never block construction. The
    ``agent`` is accepted only for fail-open logging symmetry; collection itself
    does not read agent state.
    """
    if registry is None:
        registry = core_manifest_registry()
    manifests: list[BundleManifest] = []
    for name in CORE_BUNDLE_NAMES:
        provider = registry.get(name)
        if provider is None:
            continue
        try:
            manifest = provider()
        except Exception as exc:  # fail open — never block construction
            _safe_log(agent, "guard_wiring_core_manifest_skipped",
                      capability=name, reason=str(exc))
            continue
        if isinstance(manifest, BundleManifest):
            manifests.append(manifest)
    return manifests


def collect_agent_bundle_manifests(
    agent: Any,
    *,
    registry: ManifestRegistry | None = None,
) -> list[BundleManifest]:
    """Collect declared bundle manifests for an agent's enabled capabilities.

    Walks the agent's ``_capabilities`` (a list of ``(name, kwargs)`` pairs set
    by wrapper ``Agent`` construction) and, for each capability that has a
    provider in ``registry``, calls the provider to obtain its
    :class:`BundleManifest`. Capabilities with no provider contribute nothing —
    their tools remain unknown to the bridge and fail open.

    Fail-open: a provider that raises is skipped (logged via the agent's
    ``_log`` if available) rather than aborting collection, so one broken
    manifest can never deny an otherwise-clean agent its construction.
    """
    if registry is None:
        registry = default_manifest_registry()
    if not registry:
        return []

    capabilities = getattr(agent, "_capabilities", None) or []
    manifests: list[BundleManifest] = []
    seen: set[str] = set()
    for entry in capabilities:
        name = entry[0] if isinstance(entry, (tuple, list)) and entry else entry
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        provider = registry.get(name)
        if provider is None:
            continue
        try:
            manifest = provider()
        except Exception as exc:  # fail open — never block construction
            _safe_log(agent, "guard_wiring_manifest_skipped", capability=name,
                      reason=str(exc))
            continue
        if isinstance(manifest, BundleManifest):
            manifests.append(manifest)
    return manifests


def install_bundle_guard(
    agent: Any,
    *,
    manifests: Iterable[BundleManifest],
    mode: GuardPolicyMode = DEFAULT_LIVE_GUARD_MODE,
) -> None:
    """Build a guard from ``manifests`` and install it on the Stage-16 seam.

    Replaces ``agent._tool_call_guard`` with the chain
    :func:`~lingtai_sdk.guard_bridge.tool_call_guard_from_manifests` returns for
    the supplied manifests and ``mode`` (advisory by default). With no manifests
    this is the unchanged ``default_allow`` pass-through, so calling it is always
    safe. The turn loop already threads ``_tool_call_guard`` into every
    ``ToolExecutor`` it builds (Stage 16), so the installed guard becomes live
    without any executor/turn change.

    Provenance (Stage 19): when manifests are supplied this tags the agent with
    :data:`PROVENANCE_FLAG`/:data:`PROVENANCE_SOURCE` so a later wiring call can
    recognise the guard as wrapper-derived and safely reset it (see
    :func:`reset_bundle_guard`). With *no* manifests the guard is a plain
    pass-through and no provenance is claimed — there is nothing to later reset,
    and claiming ownership of a default guard could wrongly mask a host guard.
    """
    manifest_list = list(manifests)
    guard = tool_call_guard_from_manifests(manifest_list, mode=mode)
    agent._tool_call_guard = guard
    if manifest_list:
        setattr(agent, PROVENANCE_FLAG, True)
        setattr(
            agent,
            PROVENANCE_SOURCE,
            tuple(m.name for m in manifest_list if isinstance(m, BundleManifest)),
        )


def reset_bundle_guard(agent: Any) -> None:
    """Reset a wrapper-installed bundle guard back to a pass-through.

    Stage 19 safety seam. Restores ``agent._tool_call_guard`` to a default,
    empty :class:`~lingtai_kernel.tool_call_guard.ToolCallGuard` (the same
    pass-through posture a freshly built default agent owns) and clears the
    provenance markers. Intended for the case where a previous wiring installed
    a bundle-derived guard but a later wiring collects no manifests — without
    this, the stale advisory guard would linger.

    Caller responsibility: only invoke when the agent's current guard is known
    to be wrapper-derived (i.e. :data:`PROVENANCE_FLAG` is truthy), so a
    host/subclass manually-installed guard is never clobbered.
    """
    agent._tool_call_guard = ToolCallGuard()
    setattr(agent, PROVENANCE_FLAG, False)
    setattr(agent, PROVENANCE_SOURCE, ())


def collect_default_bundle_manifests(agent: Any) -> list[BundleManifest]:
    """Collect the full Stage-3K canonical declared SDK bundle set.

    This is the default live registry/dispatch seam: every SDK-declared bundle
    manifest is collected once through :func:`lingtai_sdk.bundle_registry.default_registry`,
    then installed in advisory mode by :func:`wire_agent_guard`. It still migrates
    no handlers and blocks nothing by default; the guard bridge treats ``safe``
    tools as clean pass-through and turns ``caution`` / ``destructive`` tools into
    warnings only.

    Fail-open: if the registry ever raises (for example because a future manifest
    introduced a duplicate name/tool), construction continues with no manifests
    and a best-effort log entry rather than failing closed.
    """
    try:
        return list(default_registry().manifests())
    except Exception as exc:  # fail open — never block construction
        _safe_log(agent, "guard_wiring_default_registry_failed", reason=str(exc))
        return []


def wire_agent_guard(
    agent: Any,
    *,
    registry: ManifestRegistry | None = None,
    mode: GuardPolicyMode = DEFAULT_LIVE_GUARD_MODE,
    include_core: bool = True,
) -> None:
    """Live entry point: collect an agent's manifests and install an advisory guard.

    Called once near the end of wrapper ``Agent`` construction (and reconstruct).
    Advisory-first and fail-open:

    * collects capability-declared manifests from ``registry`` (default: the
      empty :func:`default_manifest_registry` — no *capability* declares an SDK
      manifest yet);
    * **Stage 20 (behaviour-active):** unless ``include_core=False``, also
      collects the three always-present core manifests (``system`` / ``psyche`` /
      ``soul``) via :func:`collect_core_bundle_manifests`. These surfaces are
      kernel intrinsics — always present, never listed in ``_capabilities`` — so
      the capability walk alone never reaches them; this is the seam that makes
      the Stage-18 wiring *behaviour-active* on every agent instead of dormant;
    * installs an **advisory** guard (default ``mode``) onto the Stage-16 seam:
      declared caution/destructive tools (including ``system`` and ``psyche``)
      become a *warning*, never a denial. No lifecycle/system tool is blocked by default;
    * unknown / unmanifested tools (MCP, ``add_tool``, capability tools without a
      manifest) remain unknown to the bridge and fail open — this slice only ever
      *adds advisories* for the declared core surfaces;
    * when **no** manifests are collected, resets *only* a previously
      wrapper-installed bundle guard back to a pass-through (Stage 19), leaving
      any host/subclass manually-installed guard untouched;
    * on **any** error leaves the seam untouched (safe pass-through) rather than
      failing closed.

    Pass ``include_core=False`` (and the default empty ``registry``) to recover
    the pre-Stage-20 pure pass-through (e.g. a host that wants no advisories).
    Blocking remains opt-in only (an explicit ``mode=BLOCKING``); it is never the
    default, so default wiring can never deny a core tool.
    """
    try:
        if _has_manual_guard(agent):
            # A host/subclass installed its own guard before wiring ran (non-empty
            # chain, no wrapper provenance). Stage 19's guarantee — never clobber a
            # manual guard — extends to the Stage-20 default install: leave it
            # untouched rather than overwriting it with core advisories.
            _safe_log(agent, "guard_wiring_skipped_manual_guard")
            return
        if registry is None and include_core:
            # Stage 3K default: install advisories from the canonical declared
            # SDK bundle registry, not just the three core intrinsics. Safe
            # declarations still pass through cleanly; caution/destructive
            # declarations warn only because DEFAULT_LIVE_GUARD_MODE is advisory.
            manifests = collect_default_bundle_manifests(agent)
        else:
            manifests = collect_agent_bundle_manifests(agent, registry=registry)
            if include_core:
                # Compatibility path for caller-supplied capability registries:
                # still add the three core intrinsic manifests explicitly.
                core_manifests = collect_core_bundle_manifests(agent)
                have = {m.name for m in manifests}
                manifests.extend(m for m in core_manifests if m.name not in have)
        if not manifests:
            # Nothing declared. Only reset a guard *this wrapper* previously
            # installed (provenance flag set); never clobber a host/subclass
            # manual guard, and never needlessly churn an already-default seam.
            # The flag is set to the literal ``True`` by install_bundle_guard;
            # an identity check (rather than truthiness) is deliberate so a host
            # agent that never had the flag — including a test double whose
            # missing attributes auto-vivify to a truthy stand-in — is treated
            # as *not* wrapper-owned and left untouched.
            if getattr(agent, PROVENANCE_FLAG, False) is True:
                reset_bundle_guard(agent)
            return
        install_bundle_guard(agent, manifests=manifests, mode=mode)
    except Exception as exc:  # fail open — never break construction
        _safe_log(agent, "guard_wiring_failed", reason=str(exc))


def _has_manual_guard(agent: Any) -> bool:
    """True iff the agent already owns a *host/subclass* manually-installed guard.

    A manual guard is a non-empty :class:`ToolCallGuard` chain that this wrapper
    did **not** install (no :data:`PROVENANCE_FLAG` set to ``True``). A default
    empty guard (no checks) and a wrapper-derived guard (provenance set) are both
    *not* manual, so re-wiring is free to install/replace/reset them. Stage 20's
    default core install consults this so it never overwrites a host guard.

    Conservative and total: any error inspecting the seam yields ``False`` (treat
    as not-manual) so a quirky stand-in can never wedge wiring — the worst case is
    that wiring proceeds, which for a genuine manual guard is prevented here, and
    for a non-guard object simply installs the advisory chain as before.
    """
    try:
        if getattr(agent, PROVENANCE_FLAG, False) is True:
            return False  # wrapper-owned, not manual
        guard = getattr(agent, "_tool_call_guard", None)
        checks = getattr(guard, "_checks", None)
        return bool(checks)
    except Exception:
        return False


def _safe_log(agent: Any, event: str, **fields: Any) -> None:
    """Best-effort structured log via the agent's ``_log``; never raises."""
    log = getattr(agent, "_log", None)
    if callable(log):
        try:
            log(event, **fields)
        except Exception:
            pass


__all__ = [
    "ManifestProvider",
    "ManifestRegistry",
    "DEFAULT_LIVE_GUARD_MODE",
    "PROVENANCE_FLAG",
    "PROVENANCE_SOURCE",
    "default_manifest_registry",
    "CORE_BUNDLE_NAMES",
    "core_manifest_registry",
    "collect_core_bundle_manifests",
    "collect_agent_bundle_manifests",
    "collect_default_bundle_manifests",
    "install_bundle_guard",
    "reset_bundle_guard",
    "wire_agent_guard",
]
