"""MailService — abstract message transport backing the mail intrinsic.

Implementation: FilesystemMailService (directory-based inbox delivery).

Design principles:
- Fire-and-forget: send() returns immediately, no request/response coupling
- Inbox model: listener polls for new messages in the agent's inbox directory
- No registry: the caller must know the address (discovery is external)
- Address = working directory name (relative basename, e.g. "本我")
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..handshake import is_agent, is_alive, manifest, resolve_address

logger = logging.getLogger(__name__)


class MailService(ABC):
    """Abstract message transport service.

    Backs the mail intrinsic. Implementations provide the actual
    transport mechanism.
    """

    @abstractmethod
    def send(
        self,
        address: str,
        message: dict,
        *,
        mode: str = "peer",
    ) -> str | None:
        """Send a message to an address. Returns None on success, error string on failure.

        Fire-and-forget — does not wait for a response.
        The address format is transport-specific (filesystem path for FilesystemMailService).

        Parameters
        ----------
        address:
            Recipient's address (working directory name or absolute path).
        message:
            Payload dict to deliver.
        mode:
            Address mode — "peer" (default) or "abs".
        """
        ...

    @abstractmethod
    def listen(self, on_message: Callable[[dict], None]) -> None:
        """Start listening for incoming messages.

        on_message is called for each received message.
        This should be non-blocking (start a background thread).
        """
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and clean up resources."""
        ...

    @property
    @abstractmethod
    def address(self) -> str:
        """This service's address (the agent's working directory name)."""
        ...


class FilesystemMailService(MailService):
    """Filesystem-based mail delivery.

    Delivers messages by writing files directly to the recipient's inbox
    directory.  Monitors its own inbox via polling.

    Address = working directory name (relative basename).  Example::

        svc = FilesystemMailService(Path("/agents/abc123"))
        svc.listen(on_message=lambda msg: print(msg))  # poll own inbox
        svc.send("def456", {"message": "hello"})  # write to sibling agent
    """

    def __init__(
        self,
        working_dir: str | Path,
        mailbox_rel: str = "mailbox",
        pseudo_agent_subscriptions: list[str] | None = None,
    ) -> None:
        self._working_dir = Path(working_dir)
        self._mailbox_rel = mailbox_rel
        self._mailbox_dir = self._working_dir / mailbox_rel
        self._inbox_dir = self._mailbox_dir / "inbox"
        self._inbox_dir.mkdir(parents=True, exist_ok=True)

        # Resolve subscribed pseudo-agent folders once at construction time.
        # Each subscription is a path relative to working_dir; we keep them as
        # absolute Paths so a later cwd change doesn't break lookups.
        self._pseudo_agent_dirs: list[Path] = []
        for sub in (pseudo_agent_subscriptions or []):
            self._pseudo_agent_dirs.append((self._working_dir / sub).resolve())

        # Polling state
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._seen: set[str] = set()

    # ------------------------------------------------------------------
    # address
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        """Return the working directory name as this agent's mail address."""
        return self._working_dir.name

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    def send(
        self,
        address: str,
        message: dict,
        *,
        mode: str = "peer",
    ) -> str | None:
        """Deliver *message* to the agent at *address*.

        Handshake:
        1. ``{address}/.agent.json`` must exist.
        2. ``{address}/.agent.heartbeat`` must be fresh (< 2 s).

        Then write ``message.json`` atomically into the recipient's inbox
        and copy any attachment files.

        Modes:
        - peer: resolve bare name against parent dir (default — sibling agents in same .lingtai/)
        - abs: use address as a literal absolute path (cross-network, same machine)
        """
        base_dir = self._working_dir.parent  # .lingtai/ directory
        if mode == "abs":
            recipient_dir = Path(address)
        else:
            recipient_dir = resolve_address(address, base_dir)

        # --- handshake ------------------------------------------------
        if not is_agent(recipient_dir):
            return f"No agent at {address}"

        if not is_alive(recipient_dir):
            return f"Agent at {address} is not running"

        # --- create inbox entry ---------------------------------------
        from ..intrinsics.email import _new_mailbox_id
        msg_id = _new_mailbox_id()
        inbox_dir = recipient_dir / self._mailbox_rel / "inbox"
        msg_dir = inbox_dir / msg_id

        # Inject mailbox metadata (required by mail intrinsic for
        # message tracking, read/unread, reply, archive, delete).
        from datetime import datetime, timezone
        message = {
            **message,
            "_mailbox_id": msg_id,
            "received_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }

        # Handle attachments
        attachment_paths = message.get("attachments")
        if attachment_paths:
            att_dir = msg_dir / "attachments"
            att_dir.mkdir(parents=True, exist_ok=True)
            local_copies: list[str] = []
            for fpath in attachment_paths:
                src = Path(fpath)
                if not src.is_file():
                    return f"Attachment not found: {fpath}"
                dst = att_dir / src.name
                shutil.copy2(src, dst)
                local_copies.append(str(dst))
            # Replace original paths with recipient-local paths
            message = {**message, "attachments": local_copies}
        else:
            msg_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write: tmp → rename
        tmp_path = msg_dir / "message.json.tmp"
        final_path = msg_dir / "message.json"
        try:
            tmp_path.write_text(
                json.dumps(message, indent=2, ensure_ascii=False, default=str)
            )
            os.replace(str(tmp_path), str(final_path))
        except OSError as e:
            return f"Failed to write message: {e}"

        return None

    # ------------------------------------------------------------------
    # listen / stop
    # ------------------------------------------------------------------

    def listen(self, on_message: Callable[[dict], None]) -> None:
        """Start polling the inbox for new messages.

        Existing messages are recorded in ``_seen`` so they are not
        re-delivered.  New directories that appear with a ``message.json``
        trigger *on_message*.
        """
        # Snapshot existing inbox entries so we don't re-notify
        if self._inbox_dir.is_dir():
            for entry in self._inbox_dir.iterdir():
                if entry.is_dir():
                    self._seen.add(entry.name)

        self._poll_stop.clear()

        def _poll_loop() -> None:
            while not self._poll_stop.is_set():
                try:
                    # Phase 1 — own inbox.
                    if self._inbox_dir.is_dir():
                        for entry in self._inbox_dir.iterdir():
                            if not entry.is_dir():
                                continue
                            if entry.name in self._seen:
                                continue
                            msg_file = entry / "message.json"
                            if msg_file.is_file():
                                try:
                                    payload = json.loads(msg_file.read_text(encoding="utf-8"))
                                    on_message(payload)
                                except (json.JSONDecodeError, OSError):
                                    pass
                                self._seen.add(entry.name)

                    # Phase 2 — subscribed pseudo-agent outboxes. Claim
                    # messages addressed to self via atomic rename outbox→sent.
                    for pseudo_dir in self._pseudo_agent_dirs:
                        self._poll_pseudo_outbox(pseudo_dir, on_message)
                except OSError:
                    pass
                self._poll_stop.wait(0.5)

        self._poll_thread = threading.Thread(target=_poll_loop, daemon=True)
        self._poll_thread.start()

    def _poll_pseudo_outbox(
        self,
        pseudo_dir: Path,
        on_message: Callable[[dict], None],
    ) -> None:
        """Claim addressed-to-self messages from a pseudo-agent's outbox.

        For each UUID folder in ``<pseudo_dir>/mailbox/outbox/``, read the
        message, check whether this service's address appears in its ``to``
        field, and if so:

          1. Pre-mark the UUID in ``_seen`` so Phase 1 won't re-dispatch it
             after we place a copy in own inbox.
          2. Write ``message.json`` atomically into ``<self>/mailbox/inbox/<uuid>/``
             (same tmp-write + os.replace pattern as ``send()``) — this is
             what makes the wake signal truthful: by the time ``on_message``
             fires, the message really is in this agent's inbox.
          3. Atomically rename ``<pseudo_dir>/mailbox/outbox/<uuid>/`` to
             ``<pseudo_dir>/mailbox/sent/<uuid>/`` to claim the message.
          4. Dispatch via ``on_message(payload)``.

        Concurrent pollers racing on the same message: each writes its own
        speculative inbox copy in step 2, then both race on step 3. Only one
        rename succeeds; the loser deletes its speculative inbox copy, clears
        ``_seen``, and silently skips.
        """
        outbox_dir = pseudo_dir / self._mailbox_rel / "outbox"
        if not outbox_dir.is_dir():
            return
        sent_parent = pseudo_dir / self._mailbox_rel / "sent"
        sent_parent.mkdir(parents=True, exist_ok=True)

        for entry in outbox_dir.iterdir():
            if not entry.is_dir():
                continue
            msg_file = entry / "message.json"
            if not msg_file.is_file():
                continue
            try:
                payload = json.loads(msg_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            # Normalize `to` to a list of strings.
            to_field = payload.get("to")
            if isinstance(to_field, str):
                recipients = [to_field]
            elif isinstance(to_field, list):
                recipients = [str(x) for x in to_field]
            else:
                recipients = []

            if self.address not in recipients:
                continue

            uuid_name = entry.name
            own_inbox_dir = self._inbox_dir / uuid_name
            own_msg_file = own_inbox_dir / "message.json"
            own_tmp_file = own_inbox_dir / "message.json.tmp"

            # Pre-mark BEFORE placing the file so Phase 1's own-inbox scan
            # on the next tick doesn't treat this UUID as a new arrival and
            # double-dispatch. Removed on rollback.
            self._seen.add(uuid_name)

            # Step 1: write the payload to own inbox (tmp → rename).
            try:
                own_inbox_dir.mkdir(parents=True, exist_ok=True)
                own_tmp_file.write_text(
                    json.dumps(payload, indent=2, ensure_ascii=False, default=str)
                )
                os.replace(str(own_tmp_file), str(own_msg_file))
            except OSError:
                logger.warning(
                    "failed to write claimed pseudo-agent message %s to own inbox",
                    uuid_name,
                    exc_info=True,
                )
                # Leave the message in the pseudo-agent outbox for retry;
                # do not attempt the sent-rename.
                self._seen.discard(uuid_name)
                # Best-effort cleanup of the partial inbox dir.
                shutil.rmtree(str(own_inbox_dir), ignore_errors=True)
                continue

            # Step 2: atomic claim by renaming outbox → sent. If a concurrent
            # poller won the race, the source no longer exists — roll back
            # our speculative inbox copy.
            sent_dir = sent_parent / uuid_name
            try:
                os.replace(str(entry), str(sent_dir))
            except OSError:
                shutil.rmtree(str(own_inbox_dir), ignore_errors=True)
                self._seen.discard(uuid_name)
                continue

            # Step 3: best-effort dispatch. If on_message raises, the
            # message is fully persisted (own inbox + sender sent/) and
            # nothing needs to unwind — just log so silent loss of the
            # handler-side effect is observable.
            try:
                on_message(payload)
            except Exception:
                logger.exception(
                    "on_message raised for claimed pseudo-agent message %s from %s",
                    uuid_name,
                    pseudo_dir,
                )

    def stop(self) -> None:
        """Stop the polling thread."""
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
        self._poll_thread = None
