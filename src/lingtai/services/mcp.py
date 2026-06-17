"""Compatibility shim — MCP transport clients moved to ``lingtai_sdk`` (SDK-MCP-01).

The implementation now lives in :mod:`lingtai_sdk.services.mcp`. This module is
an *alias*, not a copy: it rebinds the SDK module object into ``sys.modules``
under the historical name ``lingtai.services.mcp`` so that

* ``from lingtai.services.mcp import MCPClient, HTTPMCPClient`` keeps working
  (including ``Agent.connect_mcp`` / ``connect_mcp_http`` and the vision/web
  search/minimax callers that import via ``...services.mcp``), and
* ``monkeypatch.setattr(lingtai.services.mcp, "...", ...)`` patches the same
  module object the implementation reads its globals from.

Only the generic stdio/HTTP transport clients moved. The MCP catalog, the
``core/mcp/*`` registry + inbox poller, and ``agent.py``'s loaders stay put.

Preserving module identity (rather than star-importing into a fresh module) is
what makes existing monkeypatch-based tests continue to pass unchanged.
"""
from __future__ import annotations

import sys

from lingtai_sdk.services import mcp as _impl

# Rebind the old dotted name to the real implementation module so importers and
# monkeypatchers operate on the SDK module object itself (module identity).
sys.modules[__name__] = _impl
