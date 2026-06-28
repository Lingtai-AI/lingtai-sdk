"""Shared stdio entrypoint for curated MCP servers.

Every bundled MCP server's ``__main__.py`` ran the same three steps: configure
INFO logging to **stderr** (so logs never corrupt the JSON-RPC stdout channel),
``asyncio.run(serve())``, and swallow ``KeyboardInterrupt`` on Ctrl-C. This is
the single copy.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable


def run_stdio_server_main(serve: Callable[[], Awaitable[None]]) -> None:
    """Configure stderr logging and run ``serve()`` until interrupted."""
    # Logs to stderr so they don't pollute the MCP stdio channel.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
