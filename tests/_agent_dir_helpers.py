"""Shared filesystem factory for tests that need a minimal agent working dir.

Several test modules each spelled out the same ``.agent.json`` + heartbeat
setup (``test_handshake.py``, ``test_filesystem_mail.py``, …).  This is the one
definition; modules import it directly or via the ``make_agent_dir`` fixture in
``conftest.py``.

Deliberately raw: the protocol filenames (``.agent.json``,
``.agent.heartbeat``) are spelled out here rather than routed through
``lingtai_kernel.workdir.workdir_layout`` so the factory stays an independent
oracle — a drift in the layout helper is caught by the exact-path assertions in
``test_workdir.py`` instead of being papered over by a shared constant.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def make_agent_dir(
    base: Path,
    name: str = "agent",
    *,
    heartbeat: bool = True,
    heartbeat_ts: float | None = None,
    human: bool = False,
    manifest: dict[str, Any] | None = None,
    mailbox: bool = False,
) -> Path:
    """Create a minimal agent working directory and return its ``Path``.

    Args:
        base: Parent directory (usually ``tmp_path``).
        name: Subdirectory name. Pass ``""`` to use *base* itself.
        heartbeat: Write a ``.agent.heartbeat`` file. Forced off for humans.
        heartbeat_ts: Heartbeat timestamp; defaults to ``time.time()`` (fresh).
        human: Write ``admin=null`` (a human pseudo-agent) and skip heartbeat.
        manifest: Full ``.agent.json`` contents; overrides the default shape.
        mailbox: Also create ``mailbox/inbox``.
    """
    d = base / name if name else base
    d.mkdir(parents=True, exist_ok=True)

    if manifest is None:
        manifest = {"agent_name": "test", "admin": None if human else {}}
    (d / ".agent.json").write_text(json.dumps(manifest))

    if human:
        heartbeat = False
    if heartbeat:
        ts = time.time() if heartbeat_ts is None else heartbeat_ts
        (d / ".agent.heartbeat").write_text(str(ts))

    if mailbox:
        (d / "mailbox" / "inbox").mkdir(parents=True, exist_ok=True)

    return d
