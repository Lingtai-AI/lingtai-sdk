"""IMAP account — imapclient-based, multi-connection, watermark-driven.

One IMAPAccount owns:
  - a tool-call IMAPClient (lock-protected, used by manager actions)
  - a listener IMAPClient (dedicated thread, IDLE loop)
  - a WatermarkStore (per-(account, folder) UIDNEXT)

The on-message callback for new arrivals is registered via
``start_listening(on_message)`` and invoked from the listener thread.
"""
from __future__ import annotations

import email as email_mod
import email.policy as email_policy
import logging
import mimetypes
import re
import smtplib
import socket
import ssl
import threading
import time
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Callable

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from ._watermark import WatermarkStore

logger = logging.getLogger(__name__)

_SPECIAL_USE_ROLES = {
    b"\\Trash": "trash",
    b"\\Sent": "sent",
    b"\\Drafts": "drafts",
    b"\\Junk": "junk",
    b"\\All": "archive",
    b"\\Archive": "archive",
}
_NAME_HEURISTICS = {
    "trash": "trash", "deleted": "trash", "[gmail]/trash": "trash",
    "sent": "sent", "[gmail]/sent mail": "sent",
    "drafts": "drafts", "[gmail]/drafts": "drafts",
    "spam": "junk", "junk": "junk", "[gmail]/spam": "junk",
    "archive": "archive", "[gmail]/all mail": "archive",
}


def _decode_header_value(value: str) -> str:
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_text_body(msg: email_mod.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return _strip_html_tags(part.get_content())
                except Exception:
                    pass
        return ""
    try:
        body = msg.get_content()
    except Exception:
        return ""
    if msg.get_content_type() == "text/html":
        return _strip_html_tags(body)
    return body or ""


def _strip_html_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def _extract_attachments(msg: email_mod.message.Message) -> list[dict]:
    attachments: list[dict] = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        if part.get_content_disposition() != "attachment":
            continue
        filename = part.get_filename() or "attachment"
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        attachments.append({
            "filename": _decode_header_value(filename),
            "content_type": part.get_content_type(),
            "data": data,
        })
    return attachments


class IMAPAccount:
    """One IMAP/SMTP account."""

    def __init__(
        self,
        email_address: str,
        email_password: str,
        *,
        imap_host: str = "imap.gmail.com",
        imap_port: int = 993,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        working_dir: Path | str | None = None,
        allowed_senders: list[str] | None = None,
        poll_interval: int = 30,
    ) -> None:
        self._email_address = email_address
        self._email_password = email_password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._working_dir = Path(working_dir) if working_dir else None
        self._allowed_senders = allowed_senders
        self._poll_interval = poll_interval

        # Tool-call connection
        self._tool_imap: IMAPClient | None = None
        self._lock = threading.Lock()

        # Serializes the watermark load→search→save round-trip in reconcile()
        # against any potential concurrent callers. The listener thread is the
        # only intended caller in production; this lock is a defensive guard.
        self._reconcile_lock = threading.Lock()

        # Listener connection (background thread only)
        self._listen_imap: IMAPClient | None = None
        self._listen_in_idle = False

        # Capabilities
        self._capabilities: set[bytes] = set()
        self._has_idle = False
        self._has_move = False
        self._has_uidplus = False

        # Folder discovery
        self._folders: dict[str, str | None] = {}
        self._folder_by_role: dict[str, str] = {}

        # Watermark
        _sp = self._state_path()
        self._watermark = WatermarkStore(_sp) if _sp else None

        # Reconnect backoff
        self._backoff_steps = [1, 2, 5, 10, 60]
        self._backoff_index = 0

        # Listener thread
        self._bg_thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None

    # -- Properties ---------------------------------------------------------

    @property
    def address(self) -> str:
        return self._email_address

    @property
    def capabilities(self) -> set[str]:
        return {c.decode("ascii") if isinstance(c, bytes) else str(c)
                for c in self._capabilities}

    @property
    def has_idle(self) -> bool:
        return self._has_idle

    @property
    def has_move(self) -> bool:
        return self._has_move

    @property
    def has_uidplus(self) -> bool:
        return self._has_uidplus

    @property
    def folders(self) -> dict[str, str | None]:
        return dict(self._folders)

    @property
    def connected(self) -> bool:
        """True iff the tool-call connection is alive (NOOP succeeds)."""
        if self._tool_imap is None:
            return False
        try:
            with self._lock:
                self._tool_imap.noop()
            return True
        except Exception:
            self._tool_imap = None
            return False

    @property
    def listening(self) -> bool:
        """True iff the listener thread is alive AND currently inside IDLE."""
        return (
            self._bg_thread is not None
            and self._bg_thread.is_alive()
            and self._listen_in_idle
        )

    # -- Connection lifecycle ----------------------------------------------

    def connect(self) -> None:
        """Open the tool-call connection, parse capabilities, discover folders."""
        if self._tool_imap is not None:
            return
        client = IMAPClient(self._imap_host, port=self._imap_port, ssl=True)
        client.login(self._email_address, self._email_password)
        self._tool_imap = client
        self._fetch_capabilities()
        self._discover_folders()
        logger.info("IMAP connected: %s (%s)", self._email_address, self._imap_host)

    def disconnect(self) -> None:
        if self._tool_imap is not None:
            try:
                self._tool_imap.logout()
            except Exception:
                pass
            self._tool_imap = None

    # Transient lower-layer errors that mean "socket is dead, reconnect."
    # ``IMAPClientError`` is bound to ``imaplib.IMAP4.error`` and therefore
    # also catches ``imaplib.IMAP4.abort`` and ``IMAPClientAbortError``.
    # ``ssl.SSLError`` and ``socket.error`` are both subclasses of
    # ``OSError``; listing them explicitly documents intent and survives
    # any future divergence in the stdlib class hierarchy.
    _TRANSIENT_IMAP_ERRORS = (
        socket.error,
        OSError,
        ssl.SSLError,
        IMAPClientError,
    )

    def _ensure_connected(self) -> IMAPClient:
        """Return a live tool-call IMAPClient.

        Callers must already hold ``self._lock``. A cached client is probed
        with NOOP before being handed back; if NOOP raises (Gmail closed
        the socket after a long idle, SSL EOF, etc.) the client is
        discarded and a fresh one is opened.
        """
        if self._tool_imap is not None:
            try:
                self._tool_imap.noop()
                return self._tool_imap
            except self._TRANSIENT_IMAP_ERRORS as e:
                logger.info(
                    "imap %s: cached tool connection is dead (%s); "
                    "reconnecting", self._email_address, e,
                )
                self._drop_tool_imap()
        if self._tool_imap is None:
            self.connect()
        assert self._tool_imap is not None
        return self._tool_imap

    def _drop_tool_imap(self) -> None:
        """Best-effort close + clear the cached tool-call client."""
        client = self._tool_imap
        self._tool_imap = None
        if client is None:
            return
        try:
            client.logout()
        except Exception:
            pass

    def _with_reconnect(self, op):
        """Run ``op(imap)`` against the tool connection with retry-once.

        ``op`` is a callable that receives a live ``IMAPClient`` and does
        all of its own SELECT / SEARCH / FETCH / STORE work. If the call
        fails with a transient socket/SSL/IMAP error the cached client is
        dropped, a fresh one is opened, and ``op`` is invoked exactly one
        more time. A second failure propagates.

        Callers MUST already hold ``self._lock``; this helper does not
        acquire it. Long-running ops like SMTP send are intentionally
        excluded — only call this for read/write IMAP work.
        """
        imap = self._ensure_connected()
        try:
            return op(imap)
        except self._TRANSIENT_IMAP_ERRORS as e:
            logger.info(
                "imap %s: transient error (%s); reconnecting and retrying",
                self._email_address, e,
            )
            self._drop_tool_imap()
            imap = self._ensure_connected()
            return op(imap)

    def _fetch_capabilities(self) -> None:
        assert self._tool_imap is not None
        caps = set(self._tool_imap.capabilities())
        self._capabilities = caps
        self._has_idle = b"IDLE" in caps
        self._has_move = b"MOVE" in caps
        self._has_uidplus = b"UIDPLUS" in caps

    def _discover_folders(self) -> None:
        assert self._tool_imap is not None
        folders: dict[str, str | None] = {}
        folder_by_role: dict[str, str] = {}
        for entry in self._tool_imap.list_folders():
            attrs, _delim, name = entry
            role: str | None = None
            for attr in attrs:
                if attr in _SPECIAL_USE_ROLES:
                    role = _SPECIAL_USE_ROLES[attr]
                    break
            if not role:
                role = _NAME_HEURISTICS.get(name.lower())
            folders[name] = role
            if role and role not in folder_by_role:
                folder_by_role[role] = name
        self._folders = folders
        self._folder_by_role = folder_by_role

    def get_folder_by_role(self, role: str) -> str | None:
        return self._folder_by_role.get(role)

    # -- Watermark state ----------------------------------------------------

    def _state_path(self) -> Path | None:
        if self._working_dir is None:
            return None
        # Per-account file: working_dir/imap/<address>.state.json
        return self._working_dir / "imap" / f"{self._email_address}.state.json"

    # -- Tool-call methods --------------------------------------------------

    _HEADER_FETCH_KEY = b"BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)]"

    def fetch_envelopes(self, folder: str, n: int = 20) -> list[dict]:
        """Return headers for the N most recent UIDs in `folder`."""
        def _op(imap):
            imap.select_folder(folder, readonly=True)
            all_uids = imap.search("ALL")
            if not all_uids:
                return {}
            recent = all_uids[-n:] if n > 0 else all_uids
            return imap.fetch(
                recent,
                ["FLAGS", "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"],
            )

        with self._lock:
            data = self._with_reconnect(_op)
        return [self._envelope_from_fetch(uid, info, folder)
                for uid, info in sorted(data.items())]

    def fetch_headers_by_uids(
        self, folder: str, uids: list[str],
    ) -> list[dict]:
        """Fetch headers for explicit UIDs."""
        if not uids:
            return []
        int_uids = [int(u) for u in uids]

        def _op(imap):
            imap.select_folder(folder, readonly=True)
            return imap.fetch(
                int_uids,
                ["FLAGS", "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"],
            )

        with self._lock:
            data = self._with_reconnect(_op)
        return [self._envelope_from_fetch(uid, info, folder)
                for uid, info in sorted(data.items())]

    def _envelope_from_fetch(
        self, uid: int, info: dict, folder: str,
    ) -> dict:
        flags_raw = info.get(b"FLAGS", ())
        flags = [
            f.decode("ascii", errors="replace") if isinstance(f, bytes)
            else str(f)
            for f in flags_raw
        ]
        header_bytes = info.get(self._HEADER_FETCH_KEY, b"") or b""
        msg = email_mod.message_from_bytes(
            header_bytes, policy=email_policy.default,
        )
        return {
            "uid": str(uid),
            "from": _decode_header_value(msg.get("From", "")),
            "to": _decode_header_value(msg.get("To", "")),
            "subject": _decode_header_value(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "flags": flags,
            "email_id": f"{self._email_address}:{folder}:{uid}",
        }

    def fetch_full(self, folder: str, uid: str) -> dict | None:
        """Fetch the full message for a single UID."""
        uid_int = int(uid)

        def _op(imap):
            imap.select_folder(folder, readonly=True)
            return imap.fetch([uid_int], ["FLAGS", "RFC822"])

        with self._lock:
            data = self._with_reconnect(_op)
        info = data.get(uid_int)
        if not info:
            return None

        flags_raw = info.get(b"FLAGS", ())
        flags = [
            f.decode("ascii", errors="replace") if isinstance(f, bytes)
            else str(f)
            for f in flags_raw
        ]
        raw_email = info.get(b"RFC822", b"")
        msg = email_mod.message_from_bytes(raw_email, policy=email_policy.default)

        from_raw = msg.get("From", "")
        _, from_addr = parseaddr(from_raw)
        attachments = _extract_attachments(msg)
        attachment_info = [{
            "filename": a["filename"],
            "content_type": a["content_type"],
            "size": len(a["data"]),
        } for a in attachments]

        return {
            "uid": str(uid_int),
            "from": _decode_header_value(from_raw),
            "from_address": from_addr,
            "to": _decode_header_value(msg.get("To", "")),
            "subject": _decode_header_value(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "body": _extract_text_body(msg),
            "attachments": attachment_info,
            "attachments_raw": attachments,
            "flags": flags,
            "message_id": msg.get("Message-ID", ""),
            "in_reply_to": msg.get("In-Reply-To", ""),
            "references": msg.get("References", ""),
            "email_id": f"{self._email_address}:{folder}:{uid_int}",
        }

    def search(self, folder: str, query: str) -> list[str]:
        """Server-side IMAP SEARCH with our DSL."""
        criteria = self._build_search_criteria(query)

        def _op(imap):
            imap.select_folder(folder, readonly=True)
            return imap.search(criteria)

        with self._lock:
            uids = self._with_reconnect(_op)
        return [str(u) for u in uids]

    # Bare bool keywords with no value, e.g. ``unseen`` on its own.
    _BARE_FLAGS = {
        "flagged": b"FLAGGED",
        "unseen": b"UNSEEN",
        "seen": b"SEEN",
    }

    # Tokeniser order matters: ``key:"quoted"`` and ``key:value`` must be
    # tried before bare ``"quoted phrase"`` and bare ``token``, otherwise
    # ``from:"a b"`` would be split into key+bare.
    _SEARCH_TOKEN_RE = re.compile(
        r'(\w+):"([^"]+)"'   # key:"quoted value"   → grp 0,1
        r'|(\w+):(\S+)'      # key:value            → grp 2,3
        r'|"([^"]+)"'        # "bare quoted phrase" → grp 4
        r'|(\S+)',           # bare token           → grp 5
    )

    @staticmethod
    def _build_search_criteria(query: str) -> list[bytes]:
        """Translate our query DSL into imapclient SEARCH criteria.

        Recognised keys:
            from:<addr> to:<addr> subject:<text>
            since:YYYY-MM-DD before:YYYY-MM-DD
            flagged unseen seen
        Anything that isn't a recognised key (a bare word or a bare
        ``"quoted phrase"``) becomes a ``TEXT`` clause so the server
        actually searches for it instead of silently returning every
        message in the folder. Multiple clauses are implicitly AND-ed by
        IMAP. A truly empty query still compiles to ``ALL``.
        """
        from datetime import datetime
        criteria: list[bytes] = []
        for grp in IMAPAccount._SEARCH_TOKEN_RE.findall(query.strip()):
            kv_key_q, kv_val_q, kv_key, kv_val, bare_q, bare = grp
            if kv_key_q and kv_val_q:
                key, val = kv_key_q.lower(), kv_val_q
            elif kv_key and kv_val:
                key, val = kv_key.lower(), kv_val
            elif bare_q:
                criteria += [b"TEXT", bare_q.encode()]
                continue
            elif bare:
                bare_lc = bare.lower()
                if bare_lc in IMAPAccount._BARE_FLAGS:
                    criteria.append(IMAPAccount._BARE_FLAGS[bare_lc])
                else:
                    criteria += [b"TEXT", bare.encode()]
                continue
            else:
                continue

            if key == "from" and val:
                criteria += [b"FROM", val.encode()]
            elif key == "to" and val:
                criteria += [b"TO", val.encode()]
            elif key == "subject" and val:
                criteria += [b"SUBJECT", val.encode()]
            elif key == "since" and val:
                try:
                    d = datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "imap search: invalid date %r for since:, skipping",
                        val,
                    )
                    continue
                criteria += [b"SINCE", d.strftime("%d-%b-%Y").encode()]
            elif key == "before" and val:
                try:
                    d = datetime.strptime(val, "%Y-%m-%d")
                except ValueError:
                    logger.warning(
                        "imap search: invalid date %r for before:, skipping",
                        val,
                    )
                    continue
                criteria += [b"BEFORE", d.strftime("%d-%b-%Y").encode()]
        return criteria or [b"ALL"]

    def store_flags(
        self, folder: str, uid: str, flags: list[str], action: str = "+FLAGS",
    ) -> bool:
        try:
            flag_bytes = [f.encode("ascii") for f in flags]
        except UnicodeEncodeError:
            logger.warning(
                "imap store_flags: non-ASCII flag in %r, refusing", flags,
            )
            return False
        if action not in ("+FLAGS", "-FLAGS", "FLAGS"):
            logger.warning(
                "imap store_flags: unknown action %r, refusing", action,
            )
            return False

        def _op(imap):
            imap.select_folder(folder)
            if action == "+FLAGS":
                imap.add_flags([int(uid)], flag_bytes)
            elif action == "-FLAGS":
                imap.remove_flags([int(uid)], flag_bytes)
            else:
                imap.set_flags([int(uid)], flag_bytes)

        with self._lock:
            try:
                self._with_reconnect(_op)
                return True
            except IMAPClientError:
                # Server-rejected flag change (permission, unknown flag,
                # etc.) — distinct from a dead socket. Already swallowed
                # by the original implementation.
                return False

    def mark_seen(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Seen"])

    def mark_unseen(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Seen"], action="-FLAGS")

    def mark_flagged(self, folder: str, uid: str) -> bool:
        return self.store_flags(folder, uid, ["\\Flagged"])

    def list_folders(self) -> dict[str, str]:
        return {k: v for k, v in self._folders.items() if v is not None}

    def move_message(self, folder: str, uid: str, dest_folder: str) -> bool:
        def _op(imap):
            imap.select_folder(folder)
            if self._has_move:
                imap.move([int(uid)], dest_folder)
            else:
                imap.copy([int(uid)], dest_folder)
                imap.add_flags([int(uid)], [b"\\Deleted"])
                if self._has_uidplus:
                    imap.uid_expunge([int(uid)])
                else:
                    # No UIDPLUS — bare EXPUNGE removes ALL \Deleted msgs
                    # in this folder. Acceptable risk only because the
                    # server lacks both MOVE and UIDPLUS, which is rare.
                    logger.warning(
                        "imap: %s lacks MOVE+UIDPLUS; EXPUNGE may "
                        "affect other \\Deleted messages",
                        self._email_address,
                    )
                    imap.expunge()

        with self._lock:
            try:
                self._with_reconnect(_op)
                return True
            except IMAPClientError as e:
                logger.warning("move failed: %s", e)
                return False

    def delete_message(self, folder: str, uid: str) -> bool:
        trash = self.get_folder_by_role("trash")
        if trash and folder != trash:
            return self.move_message(folder, uid, trash)

        def _op(imap):
            imap.select_folder(folder)
            imap.add_flags([int(uid)], [b"\\Deleted"])
            if self._has_uidplus:
                imap.uid_expunge([int(uid)])
            else:
                logger.warning(
                    "imap: %s lacks UIDPLUS; EXPUNGE may affect "
                    "other \\Deleted messages",
                    self._email_address,
                )
                imap.expunge()

        with self._lock:
            try:
                self._with_reconnect(_op)
                return True
            except IMAPClientError:
                return False

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        *,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        attachments: list[str] | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str | None:
        if not subject and not body and not attachments:
            return "Cannot send empty email (no subject, no body, and no attachments)"
        if attachments:
            for filepath in attachments:
                if not Path(filepath).is_file():
                    return f"Attachment not found: {filepath}"
        try:
            if attachments:
                mime_msg = MIMEMultipart()
                mime_msg.attach(MIMEText(body, "plain", "utf-8"))
                for filepath in attachments:
                    path = Path(filepath)
                    content_type, _ = mimetypes.guess_type(str(path))
                    if content_type is None:
                        content_type = "application/octet-stream"
                    maintype, subtype = content_type.split("/", 1)
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(path.read_bytes())
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition", "attachment", filename=path.name,
                    )
                    mime_msg.attach(part)
            else:
                mime_msg = MIMEText(body, "plain", "utf-8")

            mime_msg["From"] = formataddr(("", self._email_address))
            mime_msg["To"] = ", ".join(to)
            mime_msg["Subject"] = subject
            mime_msg["Date"] = formatdate(localtime=True)
            mime_msg["Message-ID"] = make_msgid()
            if cc:
                mime_msg["CC"] = ", ".join(cc)
            if in_reply_to:
                mime_msg["In-Reply-To"] = in_reply_to
            if references:
                mime_msg["References"] = references

            all_recipients = list(to)
            if cc:
                all_recipients.extend(cc)
            if bcc:
                all_recipients.extend(bcc)

            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.starttls()
                server.login(self._email_address, self._email_password)
                server.sendmail(
                    self._email_address, all_recipients, mime_msg.as_string(),
                )
            return None
        except Exception as e:
            error = f"SMTP send failed: {e}"
            logger.error(error)
            return error

    # -- UID watermark reconcile --------------------------------------------

    def reconcile(self, folder: str = "INBOX") -> list[dict]:
        """Detect and return new headers since last watermark.

        Returns a list of envelope dicts (same shape as fetch_headers_by_uids)
        for messages that have not yet been delivered. Updates the watermark
        atomically before returning.

        On UIDVALIDITY change, resets state for the folder and returns [].
        On first call (no state), bootstraps by delivering current UNSEEN
        once and setting the watermark to UIDNEXT-1.

        The listener thread is the intended caller. The watermark load → SEARCH
        → save round-trip is serialized via _reconcile_lock so concurrent
        callers (e.g., a tool-call invocation from manager) cannot deliver
        duplicate envelopes by both reading the same stale watermark.
        """
        if self._watermark is None:
            return []

        with self._reconcile_lock:
            with self._lock:
                status = self._with_reconnect(
                    lambda imap: imap.folder_status(
                        folder, [b"UIDVALIDITY", b"UIDNEXT"],
                    ),
                )
            uidvalidity = int(status[b"UIDVALIDITY"])
            uidnext = int(status[b"UIDNEXT"])

            state = self._watermark.load()
            folder_state = state.get(folder)

            # Bootstrap: no state for this folder. Deliver currently-UNSEEN
            # messages once and pin watermark at uidnext-1 so future calls
            # only consider UIDs >= uidnext as new. UNSEEN UIDs are by
            # definition < UIDNEXT, so they are guaranteed to be at or below
            # the watermark we set here.
            if folder_state is None:
                unseen_envelopes = self._bootstrap_deliver_unseen(folder)
                state[folder] = {
                    "uidvalidity": uidvalidity,
                    "last_delivered_uid": uidnext - 1,
                }
                self._watermark.save(state)
                return unseen_envelopes

            # UIDVALIDITY change: reset, deliver nothing
            if folder_state.get("uidvalidity") != uidvalidity:
                logger.warning(
                    "UIDVALIDITY changed for %s/%s (%s -> %s); resetting watermark",
                    self._email_address, folder,
                    folder_state.get("uidvalidity"), uidvalidity,
                )
                state[folder] = {
                    "uidvalidity": uidvalidity,
                    "last_delivered_uid": uidnext - 1,
                }
                self._watermark.save(state)
                return []

            # Normal path
            last = int(folder_state["last_delivered_uid"])

            def _search_uids(imap):
                imap.select_folder(folder, readonly=True)
                return imap.search([b"UID", f"{last+1}:*".encode()])

            with self._lock:
                new_uids = self._with_reconnect(_search_uids)
            # IMAP semantics: when range start > UIDNEXT the server returns
            # the highest existing UID. Filter that out.
            new_uids = [u for u in new_uids if int(u) > last]
            if not new_uids:
                return []

            envelopes = self.fetch_headers_by_uids(folder, [str(u) for u in new_uids])
            if envelopes:
                new_high = max(int(e["uid"]) for e in envelopes)
                state[folder] = {
                    "uidvalidity": uidvalidity,
                    "last_delivered_uid": new_high,
                }
                self._watermark.save(state)
            return envelopes

    def _bootstrap_deliver_unseen(self, folder: str) -> list[dict]:
        """Bootstrap path: deliver currently-UNSEEN messages once.

        SELECT + SEARCH + FETCH all run inside one lock scope so a
        concurrent tool-call cannot select a different folder mid-flight.
        """
        def _op(imap):
            imap.select_folder(folder, readonly=True)
            uids = imap.search(b"UNSEEN")
            if not uids:
                return {}
            return imap.fetch(
                uids,
                ["FLAGS", "BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)]"],
            )

        with self._lock:
            data = self._with_reconnect(_op)
        return [self._envelope_from_fetch(uid, info, folder)
                for uid, info in sorted(data.items())]

    # -- Listener loop ------------------------------------------------------

    _IDLE_SLICE_SEC = 540          # 9 min slice
    _IDLE_CYCLE_SEC = 29 * 60      # 29 min hard cap

    def start_listening(self, on_message: Callable[[list[dict]], None]) -> None:
        if self._bg_thread is not None:
            return
        self._stop_event = threading.Event()
        self._bg_thread = threading.Thread(
            target=self._run_listener_loop,
            args=("INBOX", on_message),
            daemon=True,
        )
        self._bg_thread.start()

    def _run_listener_loop(
        self,
        folder: str,
        on_message: Callable[[list[dict]], None],
        *,
        max_iterations: int | None = None,
        backoff_override: float | None = None,
    ) -> None:
        """The listener body. Connect, IDLE in 9-min slices for up to 29 min,
        reconcile on EXISTS/RECENT, NOOP on silent slice, reconnect on error.

        ``max_iterations`` and ``backoff_override`` exist for testability —
        production calls leave them None.
        """
        assert self._stop_event is not None
        iterations = 0
        while not self._stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                return
            iterations += 1
            try:
                self._connect_listener(folder)
                # Catch up on anything that arrived while we were down
                envelopes = self.reconcile(folder)
                if envelopes:
                    on_message(envelopes)
                if self._stop_event.is_set():
                    return
                self._idle_session(folder, on_message)
                # Reset backoff only after a complete successful listener
                # cycle. A connection that succeeds but immediately fails
                # in IDLE should still advance the reconnect backoff.
                self._backoff_index = 0
            except (socket.error, OSError, IMAPClientError) as e:
                logger.warning(
                    "listener error on %s: %s", self._email_address, e,
                )
                self._disconnect_listener()
                delay = (
                    backoff_override
                    if backoff_override is not None
                    else self._backoff_steps[
                        min(self._backoff_index, len(self._backoff_steps) - 1)
                    ]
                )
                self._backoff_index += 1
                if self._stop_event.wait(delay):
                    return
        self._disconnect_listener()

    def _connect_listener(self, folder: str) -> None:
        """Open the dedicated listener IMAPClient and select INBOX."""
        if self._listen_imap is not None:
            try:
                self._listen_imap.logout()
            except Exception:
                pass
        client = IMAPClient(self._imap_host, port=self._imap_port, ssl=True)
        client.login(self._email_address, self._email_password)
        client.select_folder(folder)
        self._listen_imap = client

    def _disconnect_listener(self) -> None:
        if self._listen_imap is not None:
            try:
                self._listen_imap.logout()
            except Exception:
                pass
            self._listen_imap = None
        self._listen_in_idle = False

    def _idle_session(
        self,
        folder: str,
        on_message: Callable[[list[dict]], None],
    ) -> None:
        """One full IDLE session: re-issue every 29 min, slice every 9 min,
        NOOP probe on silent slices."""
        assert self._listen_imap is not None
        assert self._stop_event is not None
        imap = self._listen_imap
        cycle_deadline = time.monotonic() + self._IDLE_CYCLE_SEC
        imap.idle()
        self._listen_in_idle = True
        try:
            while time.monotonic() < cycle_deadline:
                if self._stop_event.is_set():
                    return
                responses = imap.idle_check(timeout=self._IDLE_SLICE_SEC)
                interesting = [
                    r for r in responses
                    if isinstance(r, tuple) and len(r) >= 2
                    and r[1] in (b"EXISTS", b"RECENT")
                ]
                if interesting:
                    imap.idle_done()
                    self._listen_in_idle = False
                    if self._stop_event.is_set():
                        return
                    envelopes = self.reconcile(folder)
                    if envelopes:
                        on_message(envelopes)
                    if self._stop_event.is_set():
                        return
                    imap.idle()
                    self._listen_in_idle = True
                elif not responses:
                    # Silent slice — probe socket
                    imap.idle_done()
                    self._listen_in_idle = False
                    imap.noop()
                    if self._stop_event.is_set():
                        return
                    imap.idle()
                    self._listen_in_idle = True
                # else: keep-alive or unrelated event, stay in IDLE
        finally:
            try:
                if self._listen_in_idle:
                    imap.idle_done()
            except Exception:
                pass
            self._listen_in_idle = False

    def stop_listening(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        # Unblock any in-flight idle_check on the listener thread by sending
        # DONE on the listener socket. Cross-thread send is safe: imapclient's
        # idle_done() is a single socket write + read, and the listener thread
        # will observe either a normal idle_check return or an IMAPClientError,
        # check the stop event, and exit. Without this, idle_check can block
        # for up to _IDLE_SLICE_SEC (9 min) before noticing the stop event.
        if self._listen_imap is not None and self._listen_in_idle:
            try:
                self._listen_imap.idle_done()
            except Exception:
                pass
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=15.0)
            self._bg_thread = None
