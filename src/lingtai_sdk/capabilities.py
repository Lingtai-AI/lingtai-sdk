"""CapabilityBundle manifest seed.

The public DTO schema describing a capability bundle: its identity, role flags,
the surfaces it contributes (tools, resources, prompts, events, hooks,
lifecycle, state), its security/permission posture, and its transport. This is
the *public schema only*: native privileged handlers live in the kernel/wrapper,
never here. The schema lets the kernel, the wrapper, and external embedders
agree on what a bundle *declares* without coupling to how it is *implemented*.

This PR ships the schema plus a single harmless ``proof_bundle()`` — a synthetic
metadata-only bundle that exercises the shape end to end. Core bundles
(``system`` / ``psyche`` / ``soul``) are intentionally NOT migrated here; that
is a later, higher-risk PR. See ``docs/sdk/architecture-foundation.md``.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from .errors import BundleLoadError


class BackendReplaceability(str, enum.Enum):
    """How freely a non-native backend may re-implement this bundle."""

    NATIVE_ONLY = "native_only"  # only the native runtime can provide it
    REPLACEABLE = "replaceable"  # any backend may re-implement
    AUGMENTABLE = "augmentable"  # backend may extend but not replace


@dataclass(frozen=True)
class RoleFlags:
    """Privilege / role posture of a bundle."""

    required: bool = False  # boots with every agent
    privileged: bool = False  # touches kernel-protected surfaces
    native_only: bool = False  # only the native runtime can host it
    can_override: bool = False  # may override an existing intrinsic/bundle
    backend_replaceability: BackendReplaceability = BackendReplaceability.REPLACEABLE


@dataclass(frozen=True)
class CapabilitySurfaces:
    """The named surfaces a bundle contributes. Names only — the manifest is a
    declaration, not an implementation."""

    tools: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    prompts: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    lifecycle: tuple[str, ...] = ()
    state: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecurityPolicy:
    """Permission / security posture for the bundle's tools."""

    permissions: tuple[str, ...] = ()  # named permissions the bundle needs
    requires_confirmation: tuple[str, ...] = ()  # tool names gated on confirm
    danger: str = "safe"  # "safe" | "caution" | "destructive"


@dataclass(frozen=True)
class TransportSpec:
    """How the bundle's surfaces are carried."""

    kind: str = "native"  # "native" | "stdio" | "http" | "in_process"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class BundleManifest:
    """The full public declaration of a capability bundle.

    Manifests are intentionally mutable in this seed contract so future assembly
    code can build them incrementally before freezing/validation policy is
    finalized. Call ``validate()`` explicitly before treating a manifest as
    trusted; this PR does not auto-validate in ``__post_init__`` so callers can
    surface multiple construction errors in later loaders.
    """

    name: str
    version: str
    summary: str = ""
    roles: RoleFlags = field(default_factory=RoleFlags)
    surfaces: CapabilitySurfaces = field(default_factory=CapabilitySurfaces)
    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    transport: TransportSpec = field(default_factory=TransportSpec)
    manual: tuple[str, ...] = ()  # skill/manual asset paths
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Raise ``ValueError`` if the manifest violates a basic invariant."""
        if not self.name:
            raise ValueError("BundleManifest.name is required")
        if not self.version:
            raise ValueError("BundleManifest.version is required")
        if self.roles.native_only and not self.roles.privileged:
            raise ValueError(
                f"native_only bundles must also be privileged (bundle {self.name!r})"
            )
        if (
            self.roles.native_only
            and self.roles.backend_replaceability
            is not BackendReplaceability.NATIVE_ONLY
        ):
            raise ValueError(
                "native_only bundles must declare "
                f"backend_replaceability=NATIVE_ONLY (bundle {self.name!r})"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view (enums -> their values) for serialization / docs."""
        d = asdict(self)
        d["roles"]["backend_replaceability"] = self.roles.backend_replaceability.value
        return d


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a JSON array/tuple of names into a ``tuple[str, ...]``.

    Strings are rejected explicitly rather than split into characters; the
    manifest schema treats named surfaces as arrays of names.
    """
    if value is None:
        return ()
    if isinstance(value, str | bytes) or not isinstance(value, (list, tuple)):
        raise BundleLoadError(
            f"manifest name lists must be arrays, got {type(value).__name__}"
        )
    if not all(isinstance(item, str) for item in value):
        raise BundleLoadError("manifest name lists must contain only strings")
    return tuple(value)


def load_manifest(data: Mapping[str, Any]) -> BundleManifest:
    """Load a validated :class:`BundleManifest` from a plain-dict declaration.

    The inverse of :meth:`BundleManifest.to_dict`: a host receives a bundle's
    *declaration* as data (e.g. parsed from a manifest file) and gets back a
    typed, **validated** manifest. Reconstructs the nested frozen dataclasses
    and the ``BackendReplaceability`` enum, then calls ``validate()`` — so a
    successfully loaded manifest is always a valid one. Unknown enum values,
    non-mapping nested blocks, or failed invariants raise
    :class:`~lingtai_sdk.errors.BundleLoadError`.

    Only recognized fields are read; unknown keys are ignored so a newer
    declaration stays loadable by an older reader.
    """
    if not isinstance(data, Mapping):
        raise BundleLoadError(
            f"manifest declaration must be a mapping, got {type(data).__name__}"
        )

    def _block(key: str) -> Mapping[str, Any]:
        block = data.get(key, {}) or {}
        if not isinstance(block, Mapping):
            raise BundleLoadError(f"manifest {key!r} block must be a mapping")
        return block

    roles_d = _block("roles")
    repl_raw = roles_d.get("backend_replaceability")
    try:
        replaceability = (
            BackendReplaceability(repl_raw)
            if repl_raw is not None
            else BackendReplaceability.REPLACEABLE
        )
    except ValueError as exc:
        raise BundleLoadError(
            f"unknown backend_replaceability {repl_raw!r}"
        ) from exc

    surfaces_d = _block("surfaces")
    security_d = _block("security")
    transport_d = _block("transport")

    try:
        manifest = BundleManifest(
            name=data.get("name", ""),
            version=data.get("version", ""),
            summary=data.get("summary", ""),
            roles=RoleFlags(
                required=bool(roles_d.get("required", False)),
                privileged=bool(roles_d.get("privileged", False)),
                native_only=bool(roles_d.get("native_only", False)),
                can_override=bool(roles_d.get("can_override", False)),
                backend_replaceability=replaceability,
            ),
            surfaces=CapabilitySurfaces(
                tools=_as_tuple(surfaces_d.get("tools")),
                resources=_as_tuple(surfaces_d.get("resources")),
                prompts=_as_tuple(surfaces_d.get("prompts")),
                events=_as_tuple(surfaces_d.get("events")),
                hooks=_as_tuple(surfaces_d.get("hooks")),
                lifecycle=_as_tuple(surfaces_d.get("lifecycle")),
                state=_as_tuple(surfaces_d.get("state")),
            ),
            security=SecurityPolicy(
                permissions=_as_tuple(security_d.get("permissions")),
                requires_confirmation=_as_tuple(
                    security_d.get("requires_confirmation")
                ),
                danger=security_d.get("danger", "safe"),
            ),
            transport=TransportSpec(
                kind=transport_d.get("kind", "native"),
                config=dict(transport_d.get("config", {}) or {}),
            ),
            manual=_as_tuple(data.get("manual")),
            metadata=dict(data.get("metadata", {}) or {}),
        )
    except (TypeError, ValueError) as exc:
        raise BundleLoadError(f"malformed manifest declaration: {exc}") from exc

    try:
        manifest.validate()
    except ValueError as exc:
        raise BundleLoadError(str(exc)) from exc
    return manifest


def proof_bundle() -> BundleManifest:
    """A harmless, metadata-only synthetic bundle exercising the schema.

    Deliberately NOT one of the core bundles. It declares a single read-only
    ``echo`` tool, no privileges, and is freely backend-replaceable — the lowest
    possible risk surface to prove the manifest shape end to end.
    """
    return BundleManifest(
        name="sdk_proof_echo",
        version="0.0.1",
        summary="Synthetic metadata-only proof bundle for the SDK foundation.",
        roles=RoleFlags(
            required=False,
            privileged=False,
            native_only=False,
            can_override=False,
            backend_replaceability=BackendReplaceability.REPLACEABLE,
        ),
        surfaces=CapabilitySurfaces(tools=("echo",)),
        security=SecurityPolicy(danger="safe"),
        transport=TransportSpec(kind="in_process"),
        metadata={"proof": True},
    )


__all__ = [
    "BackendReplaceability",
    "RoleFlags",
    "CapabilitySurfaces",
    "SecurityPolicy",
    "TransportSpec",
    "BundleManifest",
    "load_manifest",
    "proof_bundle",
]
