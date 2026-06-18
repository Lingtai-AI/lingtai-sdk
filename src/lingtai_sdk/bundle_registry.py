"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.registry`.

The declared-bundle registry and dispatch-target seam now live under the
``bundles`` subpackage. This module re-exports that surface so legacy imports
(``from lingtai_sdk.bundle_registry import default_registry``) keep resolving to
the same objects.
"""
from __future__ import annotations

from .bundles.registry import *  # noqa: F401,F403
from .bundles.registry import __all__  # noqa: F401
