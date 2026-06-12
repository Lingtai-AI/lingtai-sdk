"""LingTai WeChat MCP server.

Exposes the omnibus ``wechat`` tool (send/check/read/reply/search/...)
over MCP/stdio and pushes inbound messages into the host agent's inbox
via LICC. Reads bot config from a JSON file pointed at by the
LINGTAI_WECHAT_CONFIG env var; credentials.json sibling is produced by
the ``cli_login`` QR-code flow.
"""
from .licc import push_inbox_event
from .login import cli_login
from .server import serve, build_server, build_manager, load_config_and_credentials

__all__ = [
    "serve",
    "build_server",
    "build_manager",
    "load_config_and_credentials",
    "cli_login",
    "push_inbox_event",
]
