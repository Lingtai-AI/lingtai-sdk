"""LingTai IMAP MCP server.

Exposes the omnibus ``imap`` tool (send/check/read/reply/search/...)
over MCP/stdio and pushes inbound mail into the host agent's inbox via
LICC. Reads multi-account config from a JSON file pointed at by the
LINGTAI_IMAP_CONFIG env var.
"""
from .licc import push_inbox_event
from .server import serve, build_server, build_manager, load_config

__all__ = [
    "serve",
    "build_server",
    "build_manager",
    "load_config",
    "push_inbox_event",
]
