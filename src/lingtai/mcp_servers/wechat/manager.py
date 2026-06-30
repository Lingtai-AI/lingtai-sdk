"""WeChat addon manager — tool dispatch, message persistence, bridge."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from typing import Callable

from .types import (
    MessageItemType, WeixinMessage, MessageItem, TextItem,
    msg_from_dict, msg_to_dict,
)
from . import api
from . import media as media_mod
from .lockfile import AccountLock, PollerLockBusy
from .. import _identity, _skill

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


def _load_notification_header_template() -> str:
    return resources.files(__package__).joinpath("notification_header.md").read_text(
        encoding="utf-8"
    )


_NOTIFICATION_HEADER_TEMPLATE = _load_notification_header_template()

# Bundled usage manual (skill format) — SKILL.md ships in this package folder.
# action='manual' reads the full body; the YAML frontmatter name/description are
# injected into the tool schema as a progressive-disclosure catalog entry.
_SKILL_NAME = "wechat-mcp-manual"
_SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH = _skill.load_skill(__package__)

TEXT_CHUNK_LIMIT = 4000
SESSION_EXPIRED_ERRCODE = -14

# Max number of stable inbound signatures retained in the replay-guard
# index (inbox_seen.json). Sized well above a single refresh backlog so the
# guard never forgets a message inside the replay window, while keeping the
# state file bounded.
SEEN_KEYS_MAX = 5000

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "send", "check", "read", "reply", "search",
                "contacts", "add_contact", "remove_contact", "accounts",
                "manual",
            ],
            "description": (
                "send: send a message to a WeChat user "
                "(user_id, text; optional media_path for file/image/voice/video). "
                "check: list recent conversations with unread counts. "
                "read: read messages from a user (user_id; optional limit). "
                "reply: reply to a specific message "
                "(message_id from read results, text). "
                "search: search inbox messages by regex "
                "(query; optional user_id). "
                "contacts: list saved contacts. "
                "add_contact: save a contact (user_id, alias). "
                "remove_contact: remove a contact (alias or user_id). "
                "accounts: list configured WeChat accounts. "
                + _skill.manual_action_description(_SKILL_FRONTMATTER, _SKILL_NAME)
            ),
        },
        "user_id": {
            "type": "string",
            "description": "WeChat user ID (e.g. wxid_abc123@im.wechat)",
        },
        "text": {
            "type": "string",
            "description": "Message text content",
        },
        "media_path": {
            "type": "string",
            "description": (
                "Absolute path to a file to send as media. "
                "Type detected from extension: "
                ".jpg/.png=image, .mp4=video, .wav/.mp3=voice, other=file."
            ),
        },
        "message_id": {
            "type": "string",
            "description": "Message ID from read results (for reply action)",
        },
        "limit": {
            "type": "integer",
            "description": "Max messages to return (for read, default 10)",
            "default": 10,
        },
        "query": {
            "type": "string",
            "description": "Search query (regex pattern)",
        },
        "alias": {
            "type": "string",
            "description": "Human-friendly contact alias",
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "WeChat client — interact with WeChat users via iLink Bot API. "
    "MCP OWNERSHIP: this MCP belongs to the orchestrator (admin). If you are "
    "an avatar (your admin block is empty or all admin privileges are false), "
    "do not attempt to configure or reconfigure this MCP — your orchestrator "
    "manages it, and if the network needs this MCP to reach you the wiring "
    "is propagated to your session automatically. "
    "Supports text, images, voice, video, and files. "
    "Use 'send' for outgoing messages (text and/or media_path). "
    "'check' to see recent conversations with unread counts. "
    "'read' to read messages from a user. "
    "'reply' to respond to a message. "
    "'search' to find messages by keyword or regex. "
    "'contacts' to manage saved contacts. "
    "'accounts' to list configured WeChat accounts."
)


class WechatManager:
    """Manages WeChat addon lifecycle, tool dispatch, and message storage."""

    def __init__(
        self,
        *,
        base_url: str = api.DEFAULT_BASE_URL,
        cdn_base_url: str = api.CDN_BASE_URL,
        token: str,
        user_id: str,
        poll_interval: float = 1.0,
        allowed_users: list[str] | None = None,
        working_dir: Path,
        on_inbound: Callable[[dict], None],
        config_source: str | None = None,
        credentials_source: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._cdn_base_url = cdn_base_url
        self._token = token
        self._user_id = user_id
        self._poll_interval = poll_interval
        self._allowed_users = set(allowed_users) if allowed_users else None
        self._working_dir = Path(working_dir)
        self._on_inbound = on_inbound
        self._config_source = config_source
        self._credentials_source = credentials_source
        self._last_verified_at: str | None = None

        # Filesystem dirs
        self._wechat_dir = working_dir / "wechat"
        self._inbox_dir = self._wechat_dir / "inbox"
        self._sent_dir = self._wechat_dir / "sent"
        self._media_dir = self._wechat_dir / "media"
        for d in (self._inbox_dir, self._sent_dir, self._media_dir):
            d.mkdir(parents=True, exist_ok=True)

        # State
        self._get_updates_buf = ""
        self._context_tokens: dict[str, str] = {}  # user_id -> context_token
        self._contacts: dict[str, dict] = {}  # alias -> {user_id, name}
        self._read_ids: set[str] = set()
        # Replay/idempotency guard. Maps a stable inbound signature (derived
        # from upstream-stable fields, NOT the local UUID) to the local UUID
        # we first landed it under. Survives refresh/relaunch so that a stale
        # get_updates cursor re-fetching the same upstream messages does not
        # re-land them as fresh unread with new UUIDs. See _stable_key().
        self._seen_keys: dict[str, str] = {}
        # Bounded FIFO of stable keys to cap inbox_seen.json growth. The
        # WeChat replay bug only re-fetches a recent backlog, so a window of
        # the last N keys is sufficient and avoids unbounded state files.
        self._seen_order: list[str] = []
        self._lock = threading.Lock()  # guards shared mutable state
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll_thread: threading.Thread | None = None
        self._running = False

        # Per-account poller lock — iLink getUpdates is single-consumer,
        # so two pollers for the same bot_token race over inbound messages.
        self._account_lock = AccountLock(token)

        # Load persisted state
        self._load_state()

    def start(self) -> None:
        """Start the long-poll loop on a dedicated daemon thread.

        Refuses to start if another lingtai-wechat poller already holds the
        per-account lock on this machine. The caller may catch
        PollerLockBusy and surface it to the human (server.py logs it and
        keeps the manager nil, so tool calls return a clear error rather
        than silently competing with the other poller).
        """
        try:
            self._account_lock.acquire()
        except PollerLockBusy:
            # Re-raise after logging — server boot will catch this and
            # report it through the standard "manager not initialized" path.
            log.error(
                "WeChat poller refused to start: another poller already "
                "holds the lock for this iLink account (%s).",
                self._account_lock.path,
            )
            raise

        self._last_verified_at = datetime.now(timezone.utc).isoformat()
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._poll_thread = threading.Thread(
            target=self._loop.run_until_complete,
            args=(self._poll_loop(),),
            daemon=True,
            name="wechat-poll",
        )
        self._poll_thread.start()
        try:
            path = self.write_identity_file()
            log.info("Wrote WeChat MCP identity metadata to %s", path)
        except Exception as e:
            log.warning(
                "Failed to write WeChat MCP identity metadata (continuing): %s", e
            )
        log.info("WeChat addon started for %s", self._user_id)

    def stop(self) -> None:
        """Stop the long-poll loop and join the thread."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=40.0)  # long-poll is 35s
        self._save_state()
        self._account_lock.release()
        log.info("WeChat addon stopped")

    # ── Poll loop ──────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                resp = await api.get_updates(
                    self._base_url, self._token, self._get_updates_buf,
                )

                # Check for session expiry
                if resp.errcode == SESSION_EXPIRED_ERRCODE:
                    log.warning("WeChat session expired (errcode -14)")
                    self._notify_session_expired()
                    self._running = False
                    return

                for msg in resp.msgs:
                    await self._on_incoming(msg)

                # Advance and checkpoint the cursor only AFTER the batch has
                # been landed. Previously the cursor was bumped in-memory and
                # persisted only on a clean stop(), which never runs on a
                # worker-hang refresh/kill — so the next launch re-fetched from
                # the stale offset (the replay). Persisting here narrows that
                # window to a single in-flight batch; the inbox_seen.json guard
                # in _on_incoming is the durable backstop for whatever still
                # slips through.
                if resp.get_updates_buf and resp.get_updates_buf != self._get_updates_buf:
                    self._get_updates_buf = resp.get_updates_buf
                    try:
                        self._save_state()
                    except Exception as e:  # checkpoint is best-effort
                        log.warning("WeChat cursor checkpoint failed: %s", e)

            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("WeChat poll error: %s", e)

            await asyncio.sleep(self._poll_interval)

    def _notify_session_expired(self) -> None:
        """Send a system-level LICC event indicating WeChat session expired."""
        try:
            self._on_inbound({
                "from": "system",
                "subject": "wechat session expired",
                "body": (
                    "WeChat session expired. Please ask me to re-login to WeChat "
                    "(see lingtai-wechat README for QR-code re-auth instructions)."
                ),
                "metadata": {"event_type": "session_expired"},
                "wake": True,
            })
        except Exception as e:
            log.error("Failed to notify session expiry: %s", e)

    # ── Incoming message processing ────────────────────────────

    async def _on_incoming(self, msg: WeixinMessage) -> None:
        """Process an incoming WeChat message."""
        from_user = msg.from_user_id or ""

        # iLink/OpenClaw mark direction via message_type (1 = USER, 2 = BOT).
        # We previously used from_user == self._user_id to detect echo, but
        # QR login stores ilink_user_id (the human's id) into credentials,
        # so that comparison silently discarded every real inbound message.
        #
        # Accept only message_type == 1 (USER). Bot echoes (type=2) and any
        # other system/control message types are dropped — they have no
        # body to forward and would surface as empty inbox events.
        if msg.message_type != 1:
            return

        # Filter by allowed_users
        if self._allowed_users and from_user not in self._allowed_users:
            return

        # Cache context token (lock for cross-thread safety)
        if msg.context_token:
            with self._lock:
                self._context_tokens[from_user] = msg.context_token

        # Build text representation
        body_parts: list[str] = []
        for item in msg.item_list:
            item_type = item.type or 0
            if item_type == MessageItemType.TEXT:
                if item.text_item and item.text_item.text:
                    body_parts.append(item.text_item.text)

            elif item_type == MessageItemType.IMAGE:
                if item.image_item and item.image_item.media:
                    try:
                        ext = ".jpg"
                        fname = f"{uuid.uuid4().hex}{ext}"
                        path = await media_mod.download_media(
                            item.image_item.media, self._media_dir, fname,
                        )
                        suffix = media_mod.validate_image_bytes(path).render_suffix()
                        body_parts.append(f"[Image: {path}]{suffix}")
                    except Exception as e:
                        body_parts.append(f"[Image: download failed — {e}]")

            elif item_type == MessageItemType.VOICE:
                if item.voice_item:
                    transcription = item.voice_item.text or ""
                    audio_path = ""
                    if item.voice_item.media:
                        try:
                            silk_name = f"{uuid.uuid4().hex}.silk"
                            silk_path = await media_mod.download_media(
                                item.voice_item.media, self._media_dir, silk_name,
                            )
                            wav_path = silk_path.replace(".silk", ".wav")
                            audio_path = media_mod.decode_voice(silk_path, wav_path)
                        except Exception as e:
                            audio_path = f"download failed — {e}"
                    if transcription and audio_path:
                        body_parts.append(
                            f'[Voice: "{transcription}" (audio: {audio_path})]'
                        )
                    elif transcription:
                        body_parts.append(f'[Voice: "{transcription}"]')
                    elif audio_path:
                        body_parts.append(f"[Voice: (audio: {audio_path})]")

            elif item_type == MessageItemType.FILE:
                if item.file_item and item.file_item.media:
                    try:
                        fname = item.file_item.file_name or f"{uuid.uuid4().hex}"
                        path = await media_mod.download_media(
                            item.file_item.media, self._media_dir, fname,
                        )
                        suffix = media_mod.validate_media_bytes(path).render_suffix()
                        body_parts.append(f"[File: {fname} ({path})]{suffix}")
                    except Exception as e:
                        body_parts.append(f"[File: download failed — {e}]")

            elif item_type == MessageItemType.VIDEO:
                if item.video_item and item.video_item.media:
                    try:
                        fname = f"{uuid.uuid4().hex}.mp4"
                        path = await media_mod.download_media(
                            item.video_item.media, self._media_dir, fname,
                        )
                        body_parts.append(f"[Video: {path}]")
                    except Exception as e:
                        body_parts.append(f"[Video: download failed — {e}]")

        body = "\n".join(body_parts) if body_parts else "(empty message)"

        # Replay guard. After a worker hang the kernel refreshes via the
        # chat_history_save_skipped path and relaunches without committing the
        # WeChat get_updates cursor, so the iLink server re-delivers the same
        # backlog. Those re-deliveries arrive with identical upstream ids but
        # would otherwise be landed under brand-new local UUIDs and counted as
        # fresh unread. Detect them by their stable upstream signature and skip
        # the second landing entirely — no new inbox entry, no LICC wake.
        stable_key = self._stable_key(msg, from_user, body)
        if self._is_replay(stable_key):
            with self._lock:
                first_id = self._seen_keys.get(stable_key)
            log.info(
                "WeChat inbound replay suppressed: stable_key=%s "
                "first_local_id=%s from=%s (skipped duplicate landing)",
                stable_key, first_id, from_user,
            )
            return

        # Persist to inbox
        msg_id = str(uuid.uuid4())
        msg_dir = self._inbox_dir / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        msg_data = {
            "id": msg_id,
            "from_user_id": from_user,
            "body": body,
            "date": datetime.now(timezone.utc).isoformat(),
            "raw_item_types": [item.type for item in msg.item_list],
            # Replay-guard provenance: the stable upstream signature this
            # message was first landed under. Recorded for traceability so a
            # suppressed duplicate can always be traced back to its original.
            "stable_key": stable_key,
            "upstream_message_id": msg.message_id,
            "upstream_create_time_ms": msg.create_time_ms,
        }
        (msg_dir / "message.json").write_text(
            json.dumps(msg_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        # Record the signature only after the inbox write has succeeded, so a
        # crash mid-write cannot mark a message as seen without landing it.
        self._record_seen(stable_key, msg_id)

        # Forward to host via LICC. Body is a conversation preview with
        # guidance directing the agent to react only to the latest
        # unresponded incoming message — older lines are background only.
        try:
            contact = self._find_contact_by_user_id(from_user)
            display = contact.get("alias", from_user) if contact else from_user
            try:
                preview = self._build_conversation_preview(from_user, msg_id)
            except Exception as exc:
                log.warning("_build_conversation_preview failed: %s", exc)
                preview = body[:300].replace("\n", " ")
                if len(body) > 300:
                    preview += "..."
            self._on_inbound({
                "from": display,
                "subject": f"wechat message from {display}",
                "body": preview,
                "metadata": {
                    "message_id": msg_id,
                    "from_user_id": from_user,
                    "preview_truncated": len(body) > 300,
                    "full_length": len(body),
                    "item_types": msg_data["raw_item_types"],
                },
                "wake": True,
            })
        except Exception as e:
            log.error("Failed to forward inbound to LICC: %s", e)

    # ── Tool handler dispatch ──────────────────────────────────

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        try:
            if action == "send":
                return self._handle_send(args)
            elif action == "check":
                return self._handle_check(args)
            elif action == "read":
                return self._handle_read(args)
            elif action == "reply":
                return self._handle_reply(args)
            elif action == "search":
                return self._handle_search(args)
            elif action == "contacts":
                return self._handle_contacts()
            elif action == "add_contact":
                return self._handle_add_contact(args)
            elif action == "remove_contact":
                return self._handle_remove_contact(args)
            elif action == "accounts":
                return self._handle_accounts()
            elif action == "manual":
                return self._handle_manual()
            else:
                return {"error": f"Unknown wechat action: {action!r}"}
        except Exception as e:
            return {"error": str(e)}

    def _handle_manual(self) -> dict:
        # The manual lives in this package's bundled SKILL.md (standard skill
        # format: YAML frontmatter + markdown body), loaded at import time.
        # action='manual' returns the full skill markdown plus parsed metadata
        # and the resolved path; the frontmatter is also injected into the
        # schema's 'manual' action description as a catalog entry. Bundled
        # asset/reference sidecars, if any, are documented inside SKILL.md and
        # are not returned as structured tool fields.
        return _skill.manual_payload(
            _SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH, _SKILL_NAME
        )

    # ── Action handlers ────────────────────────────────────────

    def _handle_send(self, args: dict) -> dict:
        user_id = args.get("user_id")
        text = args.get("text", "")
        media_path = args.get("media_path")

        if not user_id:
            return {"error": "user_id is required for send"}
        if not text and not media_path:
            return {"error": "text or media_path is required"}

        # Validate media_path before sending text to avoid partial sends
        if media_path and not Path(media_path).is_file():
            return {"error": f"File not found: {media_path}"}

        results = []

        # Snapshot context token under lock (poll thread may update it)
        with self._lock:
            ctx_token = self._context_tokens.get(user_id)

        # Send text (chunked if needed)
        if text:
            chunks = _chunk_text(text, TEXT_CHUNK_LIMIT)
            for chunk in chunks:
                msg = WeixinMessage(
                    from_user_id="",
                    to_user_id=user_id,
                    client_id=f"lingtai-wechat-{uuid.uuid4().hex}",
                    message_type=2,   # BOT (matches Hermes/OpenClaw)
                    message_state=2,  # FINISH
                    context_token=ctx_token,
                    item_list=[MessageItem(
                        type=int(MessageItemType.TEXT),
                        text_item=TextItem(text=chunk),
                    )],
                )
                self._run_async(
                    api.send_message(self._base_url, self._token, msg)
                )
                results.append(f"text ({len(chunk)} chars)")

        # Send media (already validated above)
        if media_path:
            path = Path(media_path)
            upload_info = self._run_async(
                media_mod.upload_media(path, self._base_url, self._token, user_id)
            )
            media_item = media_mod.make_media_item(upload_info, path)
            msg = WeixinMessage(
                from_user_id="",
                to_user_id=user_id,
                client_id=f"lingtai-wechat-{uuid.uuid4().hex}",
                message_type=2,   # BOT
                message_state=2,  # FINISH
                context_token=ctx_token,
                item_list=[media_item],
            )
            self._run_async(
                api.send_message(self._base_url, self._token, msg)
            )
            results.append(f"media ({path.name})")

        # Persist to sent
        msg_id = str(uuid.uuid4())
        msg_dir = self._sent_dir / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)
        sent_data = {
            "id": msg_id,
            "to_user_id": user_id,
            "text": text,
            "media_path": media_path,
            "date": datetime.now(timezone.utc).isoformat(),
        }
        (msg_dir / "message.json").write_text(
            json.dumps(sent_data, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        return {"status": "ok", "sent": results, "message_id": msg_id}

    def _handle_check(self, args: dict) -> dict:
        """List conversations with unread counts.

        Merges inbox + sent so post-molt agents see their own outgoing
        replies alongside inbound messages and can avoid duplicate sends.
        Unread counts incoming messages only — outgoing ones are things
        the agent already produced.
        """
        all_msgs = self._load_inbox_messages() + self._load_sent_messages()
        all_msgs.sort(key=lambda m: m.get("date", ""), reverse=True)

        conversations: dict[str, dict] = {}
        for data in all_msgs:
            direction = "outgoing" if data.get("to_user_id") else "incoming"
            user = data.get("to_user_id") if direction == "outgoing" else data.get("from_user_id", "unknown")
            if not user:
                continue
            msg_id = data.get("id", "")
            preview = data.get("body") or data.get("text") or ""
            if user not in conversations:
                contact = self._find_contact_by_user_id(user)
                conversations[user] = {
                    "user_id": user,
                    "alias": contact.get("alias", user) if contact else user,
                    "total": 0,
                    "unread": 0,
                    "latest": preview[:100],
                    "date": data.get("date", ""),
                }
            conversations[user]["total"] += 1
            if direction == "incoming" and msg_id not in self._read_ids:
                conversations[user]["unread"] += 1
            # Don't overwrite latest — messages are sorted newest-first,
            # so the first entry per user (set in the if-block above) is correct.

        return {"conversations": list(conversations.values())}

    def _handle_read(self, args: dict) -> dict:
        user_id = args.get("user_id")
        limit = args.get("limit", 10)
        if not user_id:
            return {"error": "user_id is required for read"}

        # Merge inbox + sent so the agent can see its own outgoing replies
        # after a molt and avoid sending duplicate responses.
        combined = self._load_inbox_messages() + self._load_sent_messages()
        combined.sort(key=lambda m: m.get("date", ""), reverse=True)

        messages = []
        for data in combined:
            if data.get("to_user_id"):
                if data.get("to_user_id") != user_id:
                    continue
                data = {**data, "_direction": "outgoing"}
            else:
                if data.get("from_user_id") != user_id:
                    continue
                msg_id = data.get("id", "")
                self._read_ids.add(msg_id)
                data = {**data, "_direction": "incoming"}
            messages.append(data)
            if len(messages) >= limit:
                break

        self._save_read()
        return {"messages": messages}

    def _handle_reply(self, args: dict) -> dict:
        message_id = args.get("message_id")
        text = args.get("text", "")
        if not message_id or not text:
            return {"error": "message_id and text are required for reply"}

        # Find the original message to get user_id
        msg_file = self._inbox_dir / message_id / "message.json"
        if not msg_file.is_file():
            return {"error": f"Message not found: {message_id}"}
        data = json.loads(msg_file.read_text(encoding="utf-8"))
        user_id = data.get("from_user_id")
        if not user_id:
            return {"error": "Cannot determine user_id from message"}

        return self._handle_send({"user_id": user_id, "text": text})

    def _handle_search(self, args: dict) -> dict:
        query = args.get("query", "")
        user_id_filter = args.get("user_id")
        if not query:
            return {"error": "query is required for search"}

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        all_msgs = self._load_inbox_messages()
        matches = []
        for data in all_msgs:
            if user_id_filter and data.get("from_user_id") != user_id_filter:
                continue
            body = data.get("body", "")
            if pattern.search(body):
                matches.append(data)
            if len(matches) >= 20:
                break

        return {"matches": matches}

    def _handle_contacts(self) -> dict:
        return {"contacts": self._contacts}

    def _handle_accounts(self) -> dict:
        return {
            "status": "ok",
            "accounts": ["default"],
            "details": self.account_details(),
            "identity_path": str(self.identity_path()),
        }

    def _handle_add_contact(self, args: dict) -> dict:
        user_id = args.get("user_id")
        alias = args.get("alias")
        if not user_id or not alias:
            return {"error": "user_id and alias are required"}
        self._contacts[alias] = {
            "user_id": user_id,
            "name": args.get("name", alias),
        }
        self._save_contacts()
        return {"status": "ok", "alias": alias}

    def _handle_remove_contact(self, args: dict) -> dict:
        alias = args.get("alias")
        user_id = args.get("user_id")
        if alias and alias in self._contacts:
            del self._contacts[alias]
        elif user_id:
            self._contacts = {
                k: v for k, v in self._contacts.items()
                if v.get("user_id") != user_id
            }
        else:
            return {"error": "alias or user_id required"}
        self._save_contacts()
        return {"status": "ok"}

    @property
    def allowed_users_count(self) -> int | None:
        """Return the allow-list size without exposing user IDs."""
        if self._allowed_users is None:
            return None
        return len(self._allowed_users)

    def account_details(self) -> list[dict[str, Any]]:
        """Return non-secret public identity details for the configured account."""
        identity: dict[str, Any] = {
            "alias": "default",
            "user_id": self._user_id,
            "last_verified_at": self._last_verified_at,
            "allowed_users_count": self.allowed_users_count,
            "contact_count": len(self._contacts),
        }
        if self._config_source:
            identity["config_source"] = self._config_source
        if self._credentials_source:
            identity["credentials_source"] = self._credentials_source
        return [{k: v for k, v in identity.items() if v is not None}]

    def identity_payload(self) -> dict[str, Any]:
        """Build the non-secret MCP identity document for this service."""
        return _identity.identity_payload("wechat", self.account_details())

    def identity_path(self) -> Path:
        return _identity.identity_path(self._working_dir, "wechat")

    def write_identity_file(self) -> Path:
        """Atomically write public, non-secret MCP identity metadata."""
        return _identity.write_identity_file(
            self.identity_path(), self.identity_payload()
        )

    # ── Helpers ─────────────────────────────────────────────────

    def _build_conversation_preview(
        self,
        user_id: str,
        current_message_id: str,
        max_messages: int = 10,
    ) -> str:
        """Build a guidance-prefixed conversation preview for a notification.

        Merges inbox + sent records filtered to *user_id*, takes the last
        *max_messages* by date ascending, and formats each line as:

            [relative_time] #id sender: text

        A header tells the agent to react only to the latest unresponded
        incoming message — older lines (including past drafts or
        conditionals) are background, not new approval.
        """
        now = datetime.now(timezone.utc)

        def _rel_time(date_str: str) -> str:
            try:
                dt = datetime.fromisoformat(date_str)
            except (ValueError, TypeError):
                return date_str or "?"
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            delta = (now - dt).total_seconds()
            if delta < 60:
                return "just now"
            if delta < 3600:
                return f"{int(delta // 60)} min ago"
            if delta < 86400:
                return f"{int(delta // 3600)} hr ago"
            if delta < 172800:
                return "yesterday"
            return dt.strftime("%Y-%m-%d")

        inbox = [
            {**m, "_direction": "incoming"}
            for m in self._load_inbox_messages()
            if m.get("from_user_id") == user_id
        ]
        sent = [
            {**m, "_direction": "outgoing"}
            for m in self._load_sent_messages()
            if m.get("to_user_id") == user_id
        ]
        messages = inbox + sent
        messages.sort(key=lambda m: m.get("date") or "")
        messages = messages[-max_messages:]

        contact = self._find_contact_by_user_id(user_id)
        peer_name = contact.get("alias", user_id) if contact else user_id

        lines: list[str] = []
        for m in messages:
            mid = m.get("id", "")
            rel = _rel_time(m.get("date", ""))
            if m.get("_direction") == "outgoing":
                sender = "me"
                text = m.get("text") or m.get("body") or ""
                if not text and m.get("media_path"):
                    text = f"[media: {m['media_path']}]"
            else:
                sender = peer_name
                text = m.get("body") or m.get("text") or ""
            text_display = text.replace("\n", " ")
            lines.append(f"[{rel}] #{mid} {sender}: {text_display}")

        header = _NOTIFICATION_HEADER_TEMPLATE.format(channel="WeChat").rstrip("\n")
        tail = f"**Conversation — last {len(messages)} messages (user {peer_name})**"
        prefix = f"{header}\n\n{tail}"
        conversation = "\n".join(lines)
        body = f"{prefix}\n{conversation}" if conversation else prefix
        if len(body) > 10000:
            budget = 10000 - len(prefix) - len("\n…\n")
            if budget > 0:
                conversation = "…\n" + conversation[-budget:]
                body = f"{prefix}\n{conversation}"
            else:
                body = body[:9997] + "…"
        return body

    def _load_inbox_messages(self) -> list[dict]:
        """Load all inbox messages, sorted by date (newest first). Skips corrupt files."""
        return self._load_messages_from(self._inbox_dir)

    def _load_sent_messages(self) -> list[dict]:
        """Load all sent messages, sorted by date (newest first). Skips corrupt files."""
        return self._load_messages_from(self._sent_dir)

    @staticmethod
    def _load_messages_from(folder: Path) -> list[dict]:
        messages: list[dict] = []
        if not folder.is_dir():
            return messages
        for msg_dir in folder.iterdir():
            msg_file = msg_dir / "message.json"
            if not msg_file.is_file():
                continue
            try:
                data = json.loads(msg_file.read_text(encoding="utf-8"))
                messages.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        messages.sort(key=lambda m: m.get("date", ""), reverse=True)
        return messages

    # ── State persistence ──────────────────────────────────────

    def _load_state(self) -> None:
        contacts_file = self._wechat_dir / "contacts.json"
        if contacts_file.is_file():
            self._contacts = json.loads(
                contacts_file.read_text(encoding="utf-8")
            )
        read_file = self._wechat_dir / "read.json"
        if read_file.is_file():
            self._read_ids = set(
                json.loads(read_file.read_text(encoding="utf-8"))
            )
        state_file = self._wechat_dir / "state.json"
        if state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            self._get_updates_buf = state.get("get_updates_buf", "")
            self._context_tokens = state.get("context_tokens", {})
        seen_file = self._wechat_dir / "inbox_seen.json"
        if seen_file.is_file():
            try:
                seen = json.loads(seen_file.read_text(encoding="utf-8"))
                self._seen_keys = dict(seen.get("keys", {}))
                self._seen_order = [
                    k for k in seen.get("order", []) if k in self._seen_keys
                ]
            except (ValueError, AttributeError) as e:
                # Corrupt index degrades to "no guard", never crashes boot.
                log.warning("Failed to load inbox_seen.json (ignoring): %s", e)
                self._seen_keys = {}
                self._seen_order = []

    def _save_state(self) -> None:
        state = {
            "get_updates_buf": self._get_updates_buf,
            "context_tokens": self._context_tokens,
        }
        self._atomic_write(
            self._wechat_dir / "state.json",
            json.dumps(state, ensure_ascii=False, indent=2),
        )

    def _save_contacts(self) -> None:
        self._atomic_write(
            self._wechat_dir / "contacts.json",
            json.dumps(self._contacts, ensure_ascii=False, indent=2),
        )

    def _save_read(self) -> None:
        self._atomic_write(
            self._wechat_dir / "read.json",
            json.dumps(list(self._read_ids), ensure_ascii=False),
        )

    # ── Inbound replay / idempotency guard ─────────────────────

    @staticmethod
    def _stable_key(msg: WeixinMessage, from_user: str, body: str) -> str:
        """Derive a stable, replay-resistant signature for an inbound message.

        The local inbox UUID is regenerated on every fetch, so it cannot be
        used to detect replays. Instead we prefer upstream-stable identifiers
        that the iLink server assigns once per message and repeats verbatim
        when a stale cursor re-fetches the same backlog:

          1. ``message_id``        — upstream per-message id (most stable)
          2. ``seq``               — upstream monotonic sequence
          3. first item ``msg_id`` — item-level upstream id

        When none is present we fall back to a content signature over
        ``(from_user_id, create_time_ms, body_hash)``. ``create_time_ms`` is
        the upstream send time (NOT the local landing time, which the bug
        rewrites on replay), so two genuinely distinct messages with the same
        text at different times still produce different keys — we never drop a
        real new message.
        """
        upstream_id = None
        if msg.message_id is not None:
            upstream_id = f"mid:{msg.message_id}"
        elif msg.seq is not None:
            upstream_id = f"seq:{msg.seq}"
        else:
            for item in msg.item_list:
                if getattr(item, "msg_id", None):
                    upstream_id = f"item:{item.msg_id}"
                    break
        if upstream_id is not None:
            # Namespace by sender so an id collision across users (should not
            # happen, but cheap insurance) cannot suppress a real message.
            return f"{from_user}|{upstream_id}"

        # Content-signature fallback. Hash the body so we never persist or
        # log message text in the dedup index, only an opaque digest.
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        ctime = msg.create_time_ms if msg.create_time_ms is not None else ""
        return f"{from_user}|content:{ctime}:{body_hash}"

    def _is_replay(self, key: str) -> bool:
        """True if this stable key was already landed (replay guard hit)."""
        with self._lock:
            return key in self._seen_keys

    def _record_seen(self, key: str, local_id: str) -> None:
        """Persist that ``key`` was landed under ``local_id`` (atomic)."""
        with self._lock:
            if key in self._seen_keys:
                return
            self._seen_keys[key] = local_id
            self._seen_order.append(key)
            # Evict oldest beyond the window to bound the state file.
            while len(self._seen_order) > SEEN_KEYS_MAX:
                evicted = self._seen_order.pop(0)
                self._seen_keys.pop(evicted, None)
        self._save_seen()

    def _save_seen(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "order": list(self._seen_order),
                "keys": dict(self._seen_keys),
            }
        self._atomic_write(
            self._wechat_dir / "inbox_seen.json",
            json.dumps(payload, ensure_ascii=False),
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content to path atomically via tempfile + os.replace."""
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _find_contact_by_user_id(self, user_id: str) -> dict | None:
        for alias, data in self._contacts.items():
            if data.get("user_id") == user_id:
                return {"alias": alias, **data}
        return None

    def _run_async(self, coro):
        """Run an async coroutine from the sync tool handler thread.

        Schedules onto the poll loop's event loop via run_coroutine_threadsafe.
        Raises RuntimeError if the addon has not been started.
        """
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("WeChat addon not started — call start() first")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=30)


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most `limit` characters."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks
