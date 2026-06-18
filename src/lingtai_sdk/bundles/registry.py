"""The declared-bundle registry / dispatch-target seam (stage 3K).

Stages 3A–3J declared every SDK capability bundle in its own per-domain module
and gave each its own ``<domain>_manifests()`` aggregator — but nothing
collected those aggregators into a single, canonical view of *the whole declared
set*. Each consumer that wanted "all the bundles" (the stage-17
:mod:`lingtai_sdk.guard_bridge`, a future live runtime's tool router) had to
re-derive the union by importing and concatenating the domain modules by hand.

This module is that missing connective tissue, and *only* that:

* :func:`all_bundle_manifests` enumerates every declared SDK bundle manifest
  **once**, in a stable, documented order (core → file read → file mutation →
  communication → tool-config/catalog → shell → peer-spawn). The privileged
  core ``system`` / ``psyche`` / ``soul`` are sourced from
  :mod:`lingtai_sdk.core_bundles` (their single source of truth), not from the
  ``lifecycle_tools`` / ``psyche_tools`` / ``soul_tools`` modules that merely
  re-export them — so a core bundle is never double-counted.
* :class:`BundleRegistry` validates that union and indexes it two ways — by
  bundle ``name`` and by declared tool name — enforcing one invariant the
  per-domain modules cannot see on their own: **no two bundles share a name, and
  no two bundles declare the same tool**. A duplicate is a :class:`BundleLoadError`,
  never a silent last-writer-wins.
* :meth:`BundleRegistry.dispatch_target` turns a tool name into a
  :class:`DispatchTarget` (the owning bundle name, its manifest, and the
  declared :class:`~lingtai_sdk.capabilities.SecurityDanger` posture) — the
  read-only lookup a tool router or guard installer consults *before* dispatch.

What this module is NOT
-----------------------
It declares no new bundle and migrates no implementation. It builds **no hosts**
and holds **no handlers** — dispatch here means *"which bundle declares this
tool"*, not *"call the tool"*. Wiring real handlers, choosing which bundles gate
a live agent, and installing a guard remain the deferred, higher-risk live-wiring
slices (see ``docs/sdk/architecture-foundation.md``). Import-pure: it imports
only the import-pure declaration siblings + ``.capabilities`` + ``.errors``, so
``import lingtai_sdk.bundle_registry`` pulls in no ``lingtai`` wrapper module and
no provider SDK.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .avatar_tools import avatar_tool_manifests
from .bash_tools import bash_exec_manifests
from .contracts import BundleManifest, SecurityDanger
from .communication_tools import communication_tool_manifests
from .core import core_bundle_manifests
from ..errors import BundleLoadError
from .file_mutation_tools import file_mutation_tool_manifests
from .file_tools import file_tool_manifests
from .knowledge_tools import knowledge_catalog_manifests
from .mcp_tools import mcp_config_manifests
from .skill_tools import skills_catalog_manifests

#: The per-domain aggregators in stable registry order. Each is a zero-arg
#: callable returning a tuple of freshly-built :class:`BundleManifest`s. The
#: core bundles come from ``core_bundles`` (their single source of truth), so the
#: ``lifecycle_tools`` / ``psyche_tools`` / ``soul_tools`` re-export modules are
#: deliberately absent here — including them would double-count the core.
_DOMAIN_AGGREGATORS = (
    core_bundle_manifests,
    file_tool_manifests,
    file_mutation_tool_manifests,
    communication_tool_manifests,
    mcp_config_manifests,
    knowledge_catalog_manifests,
    skills_catalog_manifests,
    bash_exec_manifests,
    avatar_tool_manifests,
)


def all_bundle_manifests() -> tuple[BundleManifest, ...]:
    """Every declared SDK bundle manifest, once, in stable order.

    The canonical union over the per-domain aggregators (see
    :data:`_DOMAIN_AGGREGATORS`). Order is deterministic and documented:
    privileged core first, then the non-privileged surfaces grouped by domain.
    Pure — builds fresh manifests on each call and imports no wrapper.
    """
    return tuple(
        manifest
        for aggregator in _DOMAIN_AGGREGATORS
        for manifest in aggregator()
    )


@dataclass(frozen=True)
class DispatchTarget:
    """The bundle that declares a given tool, plus its declared posture.

    A read-only lookup result — *where* a tool lives in the declared set, not a
    way to call it. ``danger`` is the owning bundle's declared
    :class:`~lingtai_sdk.capabilities.SecurityDanger` (bundle-level posture),
    the same value the guard bridge keys its decision on.
    """

    bundle_name: str
    manifest: BundleManifest
    danger: SecurityDanger


class BundleRegistry:
    """A validated, name- and tool-indexed view over a set of bundle manifests.

    Construction is where every check happens, so a constructed registry is
    always internally consistent:

    * each manifest is :meth:`~lingtai_sdk.capabilities.BundleManifest.validate`-d
      (an invalid declaration is a :class:`BundleLoadError`);
    * bundle names are unique (a duplicate name is a :class:`BundleLoadError`);
    * tool names are globally unique across bundles (two bundles declaring the
      same tool is a :class:`BundleLoadError`, never silent last-writer-wins).

    The registry is read-only after construction; the lookup methods never
    mutate it and never import the wrapper.
    """

    def __init__(self, manifests: Iterable[BundleManifest]) -> None:
        by_name: dict[str, BundleManifest] = {}
        tool_owner: dict[str, str] = {}
        ordered: list[BundleManifest] = []
        for manifest in manifests:
            try:
                manifest.validate()
            except ValueError as exc:
                raise BundleLoadError(
                    f"cannot register an invalid manifest: {exc}"
                ) from exc
            if manifest.name in by_name:
                raise BundleLoadError(
                    f"duplicate bundle name {manifest.name!r} in registry"
                )
            for tool in manifest.surfaces.tools:
                owner = tool_owner.get(tool)
                if owner is not None:
                    raise BundleLoadError(
                        f"tool {tool!r} is declared by two bundles: "
                        f"{owner!r} and {manifest.name!r}"
                    )
                tool_owner[tool] = manifest.name
            by_name[manifest.name] = manifest
            ordered.append(manifest)
        self._by_name = by_name
        self._tool_owner = tool_owner
        self._ordered: tuple[BundleManifest, ...] = tuple(ordered)

    # -- read-only views ----------------------------------------------------
    def manifests(self) -> tuple[BundleManifest, ...]:
        """All registered manifests in registration order."""
        return self._ordered

    def names(self) -> tuple[str, ...]:
        """All bundle names in registration order."""
        return tuple(m.name for m in self._ordered)

    def tool_names(self) -> tuple[str, ...]:
        """Every declared tool name across all bundles, in registration order."""
        return tuple(
            tool for m in self._ordered for tool in m.surfaces.tools
        )

    def get(self, name: str) -> BundleManifest:
        """The manifest registered under ``name``; unknown name raises."""
        manifest = self._by_name.get(name)
        if manifest is None:
            raise BundleLoadError(
                f"no bundle named {name!r} in registry; "
                f"available: {sorted(self._by_name)}"
            )
        return manifest

    def bundle_for_tool(self, tool: str) -> BundleManifest:
        """The manifest of the bundle that declares ``tool``; unknown raises."""
        owner = self._tool_owner.get(tool)
        if owner is None:
            raise BundleLoadError(
                f"no bundle hosts tool {tool!r} in registry; "
                f"declared tools: {sorted(self._tool_owner)}"
            )
        return self._by_name[owner]

    def dispatch_target(self, tool: str) -> DispatchTarget:
        """Resolve ``tool`` to its owning bundle + declared danger posture.

        The read-only lookup a tool router or guard installer consults before
        dispatch: it says *which* declared bundle owns the tool and *how
        dangerous* that bundle declared itself — not how to invoke it. An
        unrecognized danger string (which a validated manifest cannot carry, but
        the bridge stays robust to) is treated conservatively as
        :attr:`~lingtai_sdk.capabilities.SecurityDanger.DESTRUCTIVE`.
        """
        manifest = self.bundle_for_tool(tool)
        try:
            danger = SecurityDanger(manifest.security.danger)
        except ValueError:
            danger = SecurityDanger.DESTRUCTIVE
        return DispatchTarget(
            bundle_name=manifest.name, manifest=manifest, danger=danger
        )


def default_registry() -> BundleRegistry:
    """A :class:`BundleRegistry` over the full declared SDK bundle set.

    The convenience entry point: :func:`all_bundle_manifests` registered into a
    validated registry. Because the declared set carries no name or tool
    collisions, this never raises today; it would begin to raise the moment a
    new declaration introduced a duplicate — which is exactly the regression the
    registry exists to catch.
    """
    return BundleRegistry(all_bundle_manifests())


__all__ = [
    "DispatchTarget",
    "BundleRegistry",
    "all_bundle_manifests",
    "default_registry",
]
