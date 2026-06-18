"""``lingtai_sdk.bundles`` — the capability-bundle machinery.

This package groups everything that declares, hosts, and registers capability
bundles:

- :mod:`~lingtai_sdk.bundles.contracts` — the ``BundleManifest`` DTO and friends
  (the declarative contract; legacy path ``lingtai_sdk.capabilities``).
- :mod:`~lingtai_sdk.bundles.host` — ``BundleHost`` / ``NativeBundleHost`` tool
  hosting (legacy path ``lingtai_sdk.capability_host``).
- :mod:`~lingtai_sdk.bundles.core` — the built-in core bundles (system / psyche /
  soul) (legacy path ``lingtai_sdk.core_bundles``).
- :mod:`~lingtai_sdk.bundles.registry` — the declared-bundle registry and
  dispatch-target seam (legacy path ``lingtai_sdk.bundle_registry``).
- :mod:`~lingtai_sdk.bundles.native` — the native bundle-hosting runtime adapter
  (legacy path ``lingtai_sdk.native``).
- the per-surface tool-declaration modules (``file_tools``, ``bash_tools``, …),
  each still importable both here and at its legacy top-level path.

Import-pure: importing this package pulls only the dependency-light kernel and
SDK contract modules, never the ``lingtai`` wrapper. The native runtime imports
the wrapper lazily, only when a session is started.
"""
from __future__ import annotations

from .contracts import (
    BackendReplaceability,
    BundleManifest,
    CapabilitySurfaces,
    RoleFlags,
    SecurityDanger,
    SecurityPolicy,
    TransportKind,
    TransportSpec,
    load_manifest,
    proof_bundle,
)
from .core import (
    core_bundle_manifests,
    core_bundle_names,
    is_core_manifest,
    native_core_host,
    native_core_hosts,
    psyche_bundle,
    soul_bundle,
    system_bundle,
)
from .host import (
    BundleHost,
    NativeBundleHost,
    PromptHandler,
    ResourceHandler,
    ToolHandler,
    native_privileged_proof_bundle,
    native_proof_host,
    proof_host,
)
from .native import AgentFactory, NativeRuntime, NativeRuntimeSession
from .registry import (
    BundleRegistry,
    DispatchTarget,
    all_bundle_manifests,
    default_registry,
)

__all__ = [
    # Contracts
    "BackendReplaceability",
    "SecurityDanger",
    "TransportKind",
    "RoleFlags",
    "CapabilitySurfaces",
    "SecurityPolicy",
    "TransportSpec",
    "BundleManifest",
    "load_manifest",
    "proof_bundle",
    # Host
    "ToolHandler",
    "ResourceHandler",
    "PromptHandler",
    "BundleHost",
    "NativeBundleHost",
    "proof_host",
    "native_privileged_proof_bundle",
    "native_proof_host",
    # Core bundles
    "system_bundle",
    "psyche_bundle",
    "soul_bundle",
    "core_bundle_manifests",
    "core_bundle_names",
    "is_core_manifest",
    "native_core_host",
    "native_core_hosts",
    # Registry / dispatch
    "DispatchTarget",
    "BundleRegistry",
    "all_bundle_manifests",
    "default_registry",
    # Native runtime adapter
    "NativeRuntime",
    "NativeRuntimeSession",
    "AgentFactory",
]
