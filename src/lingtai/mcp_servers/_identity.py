"""Shared public-identity helpers for curated messaging MCP servers."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IDENTITY_SCHEMA = "lingtai.mcp.identity.v1"

__all__ = [
    "IDENTITY_SCHEMA",
    "identity_payload",
    "identity_path",
    "write_identity_file",
]


def identity_payload(
    mcp_name: str,
    accounts: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the non-secret MCP identity document for a curated provider."""
    payload: dict[str, Any] = {
        "schema": IDENTITY_SCHEMA,
        "mcp": mcp_name,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
    }
    verified = [
        a.get("last_verified_at") for a in accounts if a.get("last_verified_at")
    ]
    if verified:
        payload["last_verified_at"] = max(str(v) for v in verified)
    return payload


def identity_path(working_dir: str | Path, mcp_name: str) -> Path:
    return Path(working_dir) / "system" / "mcp_identities" / f"{mcp_name}.json"


def write_identity_file(path: str | Path, payload: dict[str, Any]) -> Path:
    """Atomically write public, non-secret MCP identity metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path
