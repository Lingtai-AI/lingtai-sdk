"""Compatibility shim — moved to :mod:`lingtai_sdk.guard.bridge`.

The bundle-manifest → kernel guard bridge now lives under the ``guard``
subpackage. This module re-exports that surface so legacy imports
(``from lingtai_sdk.guard_bridge import guard_check_from_manifests``) keep
resolving to the same objects.
"""
from __future__ import annotations

from .guard.bridge import *  # noqa: F401,F403
from .guard.bridge import __all__  # noqa: F401
