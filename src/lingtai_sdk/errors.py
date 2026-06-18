"""SDK error surface.

A single SDK base error plus a re-export of the kernel's ``UnknownToolError``.
Kept in a leaf module with no heavy imports so ``import lingtai_sdk`` stays
cheap. Specific SDK error subclasses are added as the live runtime lands in a
later PR; this PR only needs the stable base and the kernel re-export.
"""
from __future__ import annotations

from lingtai_kernel.types import UnknownToolError


class LingTaiSDKError(Exception):
    """Base class for all SDK-level errors."""


__all__ = ["LingTaiSDKError", "UnknownToolError"]
