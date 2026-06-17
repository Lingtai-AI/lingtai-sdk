"""SDK-MCP-01 invariants — the MCP transport-client peel into ``lingtai_sdk``.

These tests pin the compatibility contract of the MCP transport slice:

* the generic MCP transport clients (``MCPClient``, ``HTTPMCPClient``) live in
  ``lingtai_sdk.services.mcp`` and are importable there directly;
* the historical ``lingtai.services.mcp`` import path still works (a shim);
* the shim preserves *module identity* so callers importing via either path —
  and any monkeypatch against the old dotted name — operate on the real SDK
  module object.

Only the transport clients moved. The MCP catalog, ``core/mcp/*`` registry +
inbox poller, and ``agent.py``'s loaders deliberately stay under ``lingtai``.
"""
from __future__ import annotations

import importlib


# ---------------------------------------------------------------------------
# SDK module is importable directly and exposes the transport clients
# ---------------------------------------------------------------------------

def test_sdk_mcp_module_importable():
    import lingtai_sdk.services.mcp  # noqa: F401


def test_sdk_mcp_exposes_clients():
    import lingtai_sdk.services.mcp as sdk_mcp

    assert hasattr(sdk_mcp, "MCPClient")
    assert hasattr(sdk_mcp, "HTTPMCPClient")


# ---------------------------------------------------------------------------
# Old import path still works (shim)
# ---------------------------------------------------------------------------

def test_old_mcp_path_still_imports():
    from lingtai.services.mcp import HTTPMCPClient, MCPClient  # noqa: F401


# ---------------------------------------------------------------------------
# Module identity — the shim aliases the SDK module, not copies it
# ---------------------------------------------------------------------------

def test_mcp_shim_is_same_module_object():
    import lingtai.services.mcp as old
    import lingtai_sdk.services.mcp as new

    assert old is new


def test_mcp_client_symbols_are_identical():
    from lingtai.services.mcp import HTTPMCPClient as OldHTTP
    from lingtai.services.mcp import MCPClient as OldClient
    from lingtai_sdk.services.mcp import HTTPMCPClient as NewHTTP
    from lingtai_sdk.services.mcp import MCPClient as NewClient

    assert OldClient is NewClient
    assert OldHTTP is NewHTTP


def test_relative_and_absolute_import_resolve_identically():
    """The vision/web-search/minimax callers import via ``...services.mcp``.

    That relative path resolves to the same dotted name as the historical
    absolute path, so both must yield the same module object.
    """
    old = importlib.import_module("lingtai.services.mcp")
    new = importlib.import_module("lingtai_sdk.services.mcp")
    assert old is new


def test_monkeypatch_on_old_mcp_name_reaches_sdk_globals(monkeypatch):
    """Patching the *old* dotted name must reach the SDK module's real globals.

    This is the property that lets existing monkeypatch-based MCP tests keep
    passing after the move — the shim aliases the module rather than copying it.
    """
    import lingtai.services.mcp as old_mcp
    import lingtai_sdk.services.mcp as sdk_mcp

    sentinel = object()
    monkeypatch.setattr(old_mcp, "logger", sentinel)
    # The patch landed on the very same module object the SDK reads from.
    assert sdk_mcp.logger is sentinel
