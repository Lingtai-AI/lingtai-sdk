"""Shared path helpers for built-in file tool wrappers."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent


def resolve_workdir_path(agent: "BaseAgent", path: str | Path) -> str | Path:
    """Resolve relative tool paths against the agent workdir.

    Absolute paths pass through unchanged to preserve the file tools' historical
    string/path behavior and error messages.
    """
    if not Path(path).is_absolute():
        return str(agent._working_dir / path)
    return path
