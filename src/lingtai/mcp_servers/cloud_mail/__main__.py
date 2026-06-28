"""Entry point for `python -m lingtai.mcp_servers.cloud_mail` and the
lingtai-cloud-mail console script."""
from __future__ import annotations

from .._entrypoint import run_stdio_server_main
from .server import serve


def main() -> None:
    run_stdio_server_main(serve)


if __name__ == "__main__":
    main()
