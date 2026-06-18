"""Console-script entry for the thin ``lingtai-cli`` host package."""
from __future__ import annotations

from .host import main as _host_main


def main() -> None:
    """Run the same host surface as ``lingtai-agent`` under the new CLI name."""
    _host_main(prog="lingtai-cli")


if __name__ == "__main__":
    main()
