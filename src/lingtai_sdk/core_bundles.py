"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.core`.

The built-in core bundles (system / psyche / soul) now live under the
``bundles`` subpackage. This module re-exports that surface so legacy imports
(``from lingtai_sdk.core_bundles import core_bundle_manifests``) keep resolving
to the same objects.
"""
from __future__ import annotations

from .bundles.core import *  # noqa: F401,F403
from .bundles.core import __all__  # noqa: F401
