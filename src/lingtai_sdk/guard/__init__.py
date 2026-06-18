"""``lingtai_sdk.guard`` — the bundle-manifest → kernel guard bridge.

The pure, import-light adapter that turns declared :class:`BundleManifest`
security postures into kernel :mod:`lingtai_kernel.tool_call_guard` primitives
lives in :mod:`lingtai_sdk.guard.bridge`. This package re-exports its surface;
the legacy module path ``lingtai_sdk.guard_bridge`` remains as a thin shim.
"""
from __future__ import annotations

from .bridge import (
    GuardPolicyMode,
    guard_check_from_manifests,
    tool_call_guard_from_manifests,
    tool_danger_index,
)

__all__ = [
    "GuardPolicyMode",
    "tool_danger_index",
    "guard_check_from_manifests",
    "tool_call_guard_from_manifests",
]
