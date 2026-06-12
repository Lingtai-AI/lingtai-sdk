from __future__ import annotations

import asyncio
import logging
import sys

from .server import serve


def main() -> None:
    # Logs to stderr so they do not pollute the MCP stdio channel.
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
