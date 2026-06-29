"""Handshake utility — validate agent presence and liveness by working dir path.

Used by FilesystemMailService (mail delivery), system intrinsic (karma/nirvana
actions), and lingtai's cpr logic.

Anatomy leaf: docs/plans/drafts/2026-04-30-anatomy-tree/leaves/core/network-discovery/
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .workdir import workdir_layout


def resolve_address(address: str | Path, base_dir: str | Path) -> Path:
    """Resolve an agent address to an absolute Path.

    Relative names (e.g. "本我") are joined with base_dir.
    Absolute paths are returned as-is.
    """
    p = Path(address)
    if p.is_absolute():
        return p
    return Path(base_dir) / address


def is_agent(path: str | Path) -> bool:
    """Check if an agent exists at *path* (has .agent.json)."""
    return workdir_layout(path).agent_manifest.is_file()


def is_human(path: str | Path) -> bool:
    """Check if the agent at *path* is a human (admin key explicitly set to null)."""
    try:
        data = manifest(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return data.get("admin") is None


def is_alive(path: str | Path, threshold: float = 2.0) -> bool:
    """Check if the agent at *path* has a fresh heartbeat.

    Returns False if heartbeat file is missing, unreadable, or older
    than *threshold* seconds.  Human agents (admin=null) are always
    considered alive — they don't write heartbeats.
    """
    if is_human(path):
        return True
    hb = workdir_layout(path).heartbeat
    if not hb.is_file():
        return False
    try:
        ts = float(hb.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return time.time() - ts < threshold


def manifest(path: str | Path) -> dict:
    """Read and return .agent.json contents.

    Raises FileNotFoundError if .agent.json does not exist.
    Raises json.JSONDecodeError if file is not valid JSON.
    """
    agent_json = workdir_layout(path).agent_manifest
    if not agent_json.is_file():
        raise FileNotFoundError(f"No .agent.json at {path}")
    return json.loads(agent_json.read_text(encoding="utf-8"))
