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
from typing import Any


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
    "proof_bundle",
]
