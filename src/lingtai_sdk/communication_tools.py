"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.communication_tools`.

This tool-declaration module now lives under the ``bundles`` subpackage. This
module re-exports that surface so legacy imports
(``from lingtai_sdk.communication_tools import ...``) keep resolving to the same objects.
"""
from __future__ import annotations

from .bundles.communication_tools import *  # noqa: F401,F403
from .bundles.communication_tools import __all__  # noqa: F401
