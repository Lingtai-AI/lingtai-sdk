"""Per-(account, folder) UIDNEXT watermark store.

State file shape::

    {
      "<folder>": {
        "uidvalidity": <int>,
        "last_delivered_uid": <int>
      },
      ...
    }

Atomic on POSIX and Windows via tmp-file + os.replace.
Corrupt or missing files are treated as empty — the addon will rebootstrap.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class WatermarkStore:
    """Tiny JSON-on-disk persistence for UID watermarks."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, dict]:
        """Return the persisted dict, or {} if missing/corrupt."""
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, state: dict[str, dict]) -> None:
        """Atomically replace the state file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp",
        )
        try:
            os.write(fd, json.dumps(state, indent=2).encode("utf-8"))
            os.close(fd)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise
