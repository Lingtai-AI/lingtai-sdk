"""Core capability-bundle manifests: ``system`` / ``psyche`` / ``soul``.

The first deliberate contact with the privileged core surfaces — but **only as
a manifest contract plus a stub-injection seam**. This module declares the three
required, privileged, native-only core bundles as :class:`BundleManifest`
objects and offers a thin adapter that turns a core manifest plus an *injected*
handler callable into a native-authority :class:`NativeBundleHost`.

What this module is NOT
-----------------------
It does **not** migrate, move, rewrite, import, or call the existing
``system`` / ``psyche`` / ``soul`` implementations, and it does not touch the
kernel turn loop. The real privileged handlers are supplied by the native
runtime in a *later* stage; here the adapter only enforces the
manifest/handler/native-authority contract around a *supplied* callable. A
freshly-imported ``core_bundles`` pulls in nothing from the ``lingtai`` wrapper
(import-pure; imports only the import-pure ``.capabilities`` /
``.capability_host`` / ``.errors`` siblings).

The three manifests share the same role posture — ``required=True``,
``privileged=True``, ``native_only=True``,
``backend_replaceability=NATIVE_ONLY``, ``transport.kind == native`` — and each
declares exactly one public tool (``system`` / ``psyche`` / ``soul``). They
differ only in their declared danger posture, mirroring the real surfaces:

* ``system`` — ``destructive`` (lifecycle control incl. irreversible teardown);
* ``psyche`` — ``destructive`` (identity/context control includes set-once true-name writes);
* ``soul``   — ``caution`` (inner-voice consultation plus persisted flow/voice config).

See ``docs/sdk/architecture-foundation.md`` (stage 8).
"""
from __future__ import annotations

from typing import Callable, Mapping

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
from .host import NativeBundleHost, ToolHandler
from ..errors import BundleHostError


def _core_manifest(
    name: str,
    *,
    summary: str,
    danger: SecurityDanger,
    actions: tuple[str, ...],
    role: str,
) -> BundleManifest:
    """Build one core-bundle manifest with the shared privileged posture.

    Every core bundle is ``required`` + ``privileged`` + ``native_only`` with
    ``backend_replaceability=NATIVE_ONLY`` and a ``native`` transport, and
    declares exactly one public tool whose name equals the bundle ``name``. The
    metadata (``role`` statement + ``actions`` list) is helpful, non-secret
    description only — it carries no handler and no implementation.
    """
    return BundleManifest(
        name=name,
        version="0.0.1",
        summary=summary,
        roles=RoleFlags(
            required=True,
            privileged=True,
            native_only=True,
            can_override=False,
            backend_replaceability=BackendReplaceability.NATIVE_ONLY,
        ),
        surfaces=CapabilitySurfaces(tools=(name,)),
        security=SecurityPolicy(danger=danger.value),
        transport=TransportSpec(kind=TransportKind.NATIVE.value),
        metadata={"core": True, "role": role, "actions": list(actions)},
    )


def system_bundle() -> BundleManifest:
    """The ``system`` core bundle manifest — highest-risk lifecycle control.

    Declares the public ``system`` tool: runtime inspection, lifecycle control,
    synchronization, and inter-agent management (some actions irreversibly tear
    down agent state), hence ``danger=destructive``. **Manifest only** — the
    real handler is injected by the native runtime in a later stage.
    """
    return _core_manifest(
        "system",
        summary="Runtime inspection, lifecycle control, synchronization, "
        "and inter-agent management.",
        danger=SecurityDanger.DESTRUCTIVE,
        actions=(
            "refresh",
            "sleep",
            "lull",
            "suspend",
            "cpr",
            "interrupt",
            "clear",
            "nirvana",
            "presets",
            "notification",
            "dismiss",
        ),
        role="The agent's lifecycle and inter-agent control surface.",
    )


def psyche_bundle() -> BundleManifest:
    """The ``psyche`` core bundle manifest — identity / pad / context management.

    Declares the public ``psyche`` tool: edit/load identity (``lingtai.md``) and
    the pad sketchboard, molt (shed conversation context while keeping a
    briefing), and naming. The surface includes ``name.set``, a set-once true-name
    write, so the bundle-level posture is ``danger=destructive`` (the strongest
    per-action grade). **Manifest only** — the real handler is injected later.
    """
    return _core_manifest(
        "psyche",
        summary="Identity, pad, and context management — edit/load identity "
        "and pad, molt, and naming.",
        danger=SecurityDanger.DESTRUCTIVE,
        actions=("lingtai", "pad", "context", "name"),
        role="The agent's identity and working-context surface.",
    )


def soul_bundle() -> BundleManifest:
    """The ``soul`` core bundle manifest — reflective inner-voice control.

    Declares the public ``soul`` tool: periodic past-self consultation,
    self-inquiry, and flow/voice tuning. It is lower-risk than lifecycle or
    identity/context control, but ``config`` / ``voice`` persist preferences, so
    the declared posture is ``danger=caution``. **Manifest only** — the real
    handler is injected by the native runtime in a later stage.
    """
    return _core_manifest(
        "soul",
        summary="The agent's inner voice — periodic past-self consultation, "
        "self-inquiry, and flow tuning.",
        danger=SecurityDanger.CAUTION,
        actions=("flow", "inquiry", "config", "voice", "dismiss"),
        role="The agent's reflective inner-voice surface.",
    )


# Stable, canonical order for the three core bundles.
_CORE_BUILDERS: tuple[Callable[[], BundleManifest], ...] = (
    system_bundle,
    psyche_bundle,
    soul_bundle,
)


def core_bundle_manifests() -> tuple[BundleManifest, ...]:
    """The three core bundle manifests in stable order: system, psyche, soul."""
    return tuple(builder() for builder in _CORE_BUILDERS)


def core_bundle_names() -> tuple[str, ...]:
    """The three core bundle names in stable order: ``("system", "psyche", "soul")``."""
    return tuple(m.name for m in core_bundle_manifests())


def is_core_manifest(manifest: BundleManifest) -> bool:
    """True iff ``manifest`` is one of the three core bundles by name."""
    return manifest.name in core_bundle_names()


def native_core_host(
    manifest: BundleManifest, handler: ToolHandler
) -> NativeBundleHost:
    """Build a native-authority host for a core bundle from an *injected* handler.

    The thin adapter/host shim. Given a core :class:`BundleManifest` and a single
    *supplied* callable, returns a :class:`NativeBundleHost` constructed with
    ``native_authority=True`` that hosts the bundle's one declared tool. The
    handler is whatever the native runtime injects in a later stage; this shim
    never calls or imports the real ``system`` / ``psyche`` / ``soul``
    implementation.

    It only enforces the contract:

    * ``manifest`` must be one of the core bundles (a non-core manifest raises
      :class:`~lingtai_sdk.errors.BundleHostError`);
    * ``handler`` must be callable (a missing / non-callable handler raises);
    * the manifest/handler parity and native-authority/transport rules are then
      enforced by :class:`NativeBundleHost` itself.
    """
    if not is_core_manifest(manifest):
        raise BundleHostError(
            f"native_core_host expects a core bundle "
            f"(one of {list(core_bundle_names())}), got {manifest.name!r}"
        )
    if not callable(handler):
        raise BundleHostError(
            f"core bundle {manifest.name!r} requires a callable handler, got "
            f"{type(handler).__name__}"
        )
    # The single declared tool always equals the bundle name (see _core_manifest).
    tool_name = manifest.name
    return NativeBundleHost(
        manifest, {tool_name: handler}, native_authority=True
    )


def native_core_hosts(
    handlers: Mapping[str, ToolHandler],
) -> dict[str, NativeBundleHost]:
    """Build ``{name: NativeBundleHost}`` for all core bundles from injected handlers.

    ``handlers`` maps each core bundle name (``system`` / ``psyche`` / ``soul``)
    to the callable the native runtime supplies for that bundle's tool. Every
    core bundle must have a handler and there must be no handler for a name that
    is not a core bundle — a missing or undeclared/extra handler raises
    :class:`~lingtai_sdk.errors.BundleHostError`, so a partial wiring can never
    boot a subset of the privileged core silently.
    """
    expected = set(core_bundle_names())
    provided = set(handlers)
    missing = expected - provided
    if missing:
        raise BundleHostError(
            f"missing handler(s) for core bundle(s): {sorted(missing)}"
        )
    extra = provided - expected
    if extra:
        raise BundleHostError(
            f"handler(s) for non-core bundle name(s): {sorted(extra)}"
        )
    return {
        manifest.name: native_core_host(manifest, handlers[manifest.name])
        for manifest in core_bundle_manifests()
    }


__all__ = [
    "system_bundle",
    "psyche_bundle",
    "soul_bundle",
    "core_bundle_manifests",
    "core_bundle_names",
    "is_core_manifest",
    "native_core_host",
    "native_core_hosts",
]
