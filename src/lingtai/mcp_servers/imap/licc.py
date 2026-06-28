"""LICC v1 client shim for the imap MCP server.

Keeps the ``lingtai.mcp_servers.imap.licc.push_inbox_event`` import path stable
while delegating to the shared ``lingtai.mcp_servers._licc_compat`` wrapper.
"""
from __future__ import annotations

from .._licc_compat import (
    EVENT_SUFFIX,
    INBOX_DIRNAME,
    LICC_VERSION,
    TMP_SUFFIX,
    push_inbox_event,
)

__all__ = [
    "push_inbox_event",
    "LICC_VERSION",
    "INBOX_DIRNAME",
    "TMP_SUFFIX",
    "EVENT_SUFFIX",
]
