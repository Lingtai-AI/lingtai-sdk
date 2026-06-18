"""Compatibility shim — the CLI host moved to :mod:`lingtai_cli.host`.

The product-assembly / host layer now lives in the ``lingtai_cli`` package.
This module re-exports its public surface so existing imports
(``from lingtai.cli import load_init, build_agent, run, main``) and the
``lingtai-agent = lingtai.cli:main`` console-script entry keep working
unchanged. There is no forked behaviour here: every name resolves to the SAME
object ``lingtai_cli.host`` defines.

New code should import from ``lingtai_cli.host`` (host behaviour) or
``lingtai_cli.assembly`` (the light project-state → ``RuntimeOptions`` bridge).
"""
from __future__ import annotations

from lingtai_cli.host import (  # noqa: F401  (re-export)
    Agent,
    FilesystemMailService,
    LLMService,
    build_agent,
    load_env_file,
    load_init,
    main,
    resolve_env,
    run,
)

__all__ = [
    "load_init",
    "build_agent",
    "run",
    "main",
    "load_env_file",
    "resolve_env",
    "LLMService",
    "Agent",
    "FilesystemMailService",
]


if __name__ == "__main__":
    main()
