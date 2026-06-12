"""Cross-agent IMAP relay bridge — directory inbox watcher.

Other agents on the same host can drop a message file into
``<bridge_dir>/inbox/<sender>/<msg-uuid>/message.json`` to relay outbound
mail through this IMAP server. The bridge polls the inbox directory and
hands each new message to a callback (typically ``IMAPMailManager``'s
``_send`` proxy) for outbound delivery.

This is the minimal subset of the kernel's ``FilesystemMailService`` that
the IMAP relay actually needs: ``listen(on_message=...)`` + ``stop()``.
Vendored here so this MCP doesn't depend on lingtai-kernel.

Bridge layout:
    <bridge_dir>/inbox/<sender>/<msg-uuid>/message.json   # incoming
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # seconds


class FilesystemMailBridge:
    """Polls a bridge directory for cross-agent relay messages."""

    def __init__(self, bridge_dir: Path | str) -> None:
        self._bridge_dir = Path(bridge_dir)
        self._inbox_dir = self._bridge_dir / "inbox"
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._seen: set[str] = set()

    def listen(self, on_message: Callable[[dict], None]) -> None:
        """Begin polling the bridge inbox. Already-present messages are
        recorded as seen so they aren't re-delivered on restart."""
        self._inbox_dir.mkdir(parents=True, exist_ok=True)

        # Snapshot existing entries so we don't double-deliver across restarts.
        for sender_dir in self._inbox_dir.iterdir() if self._inbox_dir.is_dir() else []:
            if not sender_dir.is_dir():
                continue
            for msg_dir in sender_dir.iterdir():
                if msg_dir.is_dir():
                    self._seen.add(str(msg_dir))

        self._poll_stop.clear()

        def _loop() -> None:
            while not self._poll_stop.is_set():
                try:
                    self._scan(on_message)
                except OSError as e:
                    log.warning("bridge scan failed: %s", e)
                self._poll_stop.wait(POLL_INTERVAL)

        self._poll_thread = threading.Thread(
            target=_loop, daemon=True, name="imap-bridge-poll",
        )
        self._poll_thread.start()
        log.info("bridge listening on %s", self._inbox_dir)

    def _scan(self, on_message: Callable[[dict], None]) -> None:
        if not self._inbox_dir.is_dir():
            return
        for sender_dir in self._inbox_dir.iterdir():
            if not sender_dir.is_dir():
                continue
            for msg_dir in sender_dir.iterdir():
                if not msg_dir.is_dir():
                    continue
                key = str(msg_dir)
                if key in self._seen:
                    continue
                msg_file = msg_dir / "message.json"
                if not msg_file.is_file():
                    continue
                try:
                    payload = json.loads(msg_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("bridge: bad message %s: %s", msg_file, e)
                    self._seen.add(key)
                    continue
                try:
                    on_message(payload)
                except Exception as e:
                    log.error("bridge dispatch failed for %s: %s", msg_file, e)
                self._seen.add(key)

    def stop(self) -> None:
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
        self._poll_thread = None
