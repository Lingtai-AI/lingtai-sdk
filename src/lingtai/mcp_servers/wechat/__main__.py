"""Entry point for `python -m lingtai.mcp_servers.wechat` and the lingtai-wechat script."""
from __future__ import annotations

import asyncio
import logging
import sys

from .server import serve


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
