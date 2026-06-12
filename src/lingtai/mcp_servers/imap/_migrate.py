"""One-shot legacy state cleanup for the IMAP addon.

The pre-rewrite addon persisted a `_processed_uids` dict-of-set per account.
After the rewrite, the new shape is a dict-of-(uidvalidity, last_delivered_uid).
This module deletes legacy state files at addon load. Idempotent.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_legacy_state(state_dir: Path | str) -> list[Path]:
    """Delete any legacy `<address>.state.json` files in `state_dir`.

    A file is "legacy" iff it parses as JSON and none of its top-level
    folder entries have a `last_delivered_uid` key. New-shape files are
    preserved, unparseable files are preserved (treated as opaque user
    data — better safe than sorry).

    Returns the list of paths that were deleted.
    """
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return []

    deleted: list[Path] = []
    for path in state_dir.iterdir():
        if not path.is_file():
            continue
        if not path.name.endswith(".state.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        # New shape: every value is a dict with last_delivered_uid.
        is_new_shape = all(
            isinstance(v, dict) and "last_delivered_uid" in v
            for v in data.values()
        )
        if is_new_shape and data:
            continue
        try:
            path.unlink()
            deleted.append(path)
            logger.info("imap: removed legacy state file %s", path)
        except OSError as exc:
            logger.warning("imap: could not remove legacy state file %s: %s", path, exc)
    return deleted
