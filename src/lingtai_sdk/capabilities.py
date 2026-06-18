"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.contracts`.

The capability-bundle manifest contract (``BundleManifest`` and friends) now
lives under the ``bundles`` subpackage. This module re-exports that surface so
legacy imports (``from lingtai_sdk.capabilities import BundleManifest``) keep
resolving to the same objects. No fork: every name here *is* the bundles object.
"""
from __future__ import annotations

from .bundles.contracts import *  # noqa: F401,F403
from .bundles.contracts import __all__  # noqa: F401
