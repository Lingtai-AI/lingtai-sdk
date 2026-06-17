"""SDK error surface.

A single SDK base error, runtime-scoped subclasses, plus a re-export of the
kernel's ``UnknownToolError``. Kept in a leaf module with no heavy imports so
``import lingtai_sdk`` stays cheap.
"""
from __future__ import annotations

from lingtai_kernel.types import UnknownToolError


class LingTaiSDKError(Exception):
    """Base class for all SDK-level errors."""


class NativeRuntimeConfigurationError(LingTaiSDKError):
    """Raised when ``NativeRuntime`` cannot build a session from its options.

    The default ``agent_factory`` builds an ``LLMService`` from
    ``RuntimeOptions.provider``/``model`` (with optional ``base_url``/
    ``api_key``); this error is raised — *before* any agent is constructed —
    when that LLM config is partial or absent and no ``agent_factory`` was
    injected to supply a ready service. Its message never echoes ``api_key``.
    """


class BundleLoadError(LingTaiSDKError):
    """Raised when a declared ``BundleManifest`` cannot be loaded from data.

    Covers both shape errors (an unrecognized ``backend_replaceability`` value,
    a non-mapping nested block) and the manifest's own ``validate()`` invariants
    failing — ``load_manifest`` validates before returning, so a loaded manifest
    is always a *valid* manifest.
    """


class BundleHostError(LingTaiSDKError):
    """Raised when a ``BundleHost`` refuses to host or invoke a bundle.

    Refusals are part of the load/host *boundary* contract: a privileged or
    native-only manifest (only the native runtime may host those), a
    manifest/handler mismatch (a declared tool with no handler, or a handler for
    an undeclared tool), or an ``invoke`` of a tool the bundle does not declare.
    """


__all__ = [
    "LingTaiSDKError",
    "NativeRuntimeConfigurationError",
    "BundleLoadError",
    "BundleHostError",
    "UnknownToolError",
]
