"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.native`.

The native bundle-hosting runtime adapter now lives under the ``bundles``
subpackage. This module re-exports that surface so legacy imports
(``from lingtai_sdk.native import NativeRuntime``) keep resolving to the same
objects. Importing this shim stays import-pure — the wrapper ``Agent`` is loaded
lazily only when a native session is started.
"""
from __future__ import annotations

from .bundles.native import *  # noqa: F401,F403
from .bundles.native import __all__  # noqa: F401
