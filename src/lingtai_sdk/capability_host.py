"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.host`.

The bundle tool-hosting machinery (``BundleHost`` / ``NativeBundleHost`` and the
proof hosts) now lives under the ``bundles`` subpackage. This module re-exports
that surface so legacy imports (``from lingtai_sdk.capability_host import
NativeBundleHost``) keep resolving to the same objects.
"""
from __future__ import annotations

from .bundles.host import *  # noqa: F401,F403
from .bundles.host import __all__  # noqa: F401
