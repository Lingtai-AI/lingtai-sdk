"""Compatibility shim — moved to :mod:`lingtai_sdk.bundles.mcp_tools`.

This tool-declaration module now lives under the ``bundles`` subpackage. This
module re-exports that surface so legacy imports
(``from lingtai_sdk.mcp_tools import ...``) keep resolving to the same objects.
"""
from __future__ import annotations

from .bundles.mcp_tools import *  # noqa: F401,F403
from .bundles.mcp_tools import __all__  # noqa: F401
