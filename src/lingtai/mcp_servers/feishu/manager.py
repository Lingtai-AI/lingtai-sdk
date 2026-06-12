"""FeishuManager — tool dispatch + filesystem persistence.

Storage layout:
    working_dir/feishu/{alias}/inbox/{uuid}/message.json
    working_dir/feishu/{alias}/sent/{uuid}/message.json
    working_dir/feishu/{alias}/contacts.json   open_id -> {alias, name, chat_id}
    working_dir/feishu/{alias}/read.json       list of read compound IDs
    working_dir/feishu/{alias}/state.json      bot_info

Compound message ID format: {alias}:{chat_id}:{feishu_message_id}
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .service import FeishuService

log = logging.getLogger(__name__)


def _load_notification_header_template() -> str:
    return resources.files(__package__).joinpath("notification_header.md").read_text(
        encoding="utf-8"
    )


_NOTIFICATION_HEADER_TEMPLATE = _load_notification_header_template()

# Emoji reactions for different states
# Feishu supported emoji types: OK, THUMBSUP, SMILE, HEART, THANKS, etc.
REACTION_SEEN = "OK"        # Message received — "got it"
REACTION_DONE = "THUMBSUP"  # Response sent — "done"


class TypingIndicatorManager:
    """Manages automatic typing feedback for Feishu chats.

    Since Feishu has no native typing indicator API (unlike Telegram's
    sendChatAction), this sends a temporary "typing..." message that is
    deleted when the response is ready. Best-effort — never blocks or fails.
    """

    def __init__(self) -> None:
        self._active_chats: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def start_typing(
        self, account: Any, chat_id: str, receive_id: str, receive_id_type: str,
    ) -> str | None:
        """Send a typing feedback message. Returns the feishu_message_id of the
        temporary message, or None on failure.

        Args:
            account: FeishuAccount instance.
            chat_id: The chat_id to associate the typing message with.
            receive_id: The receive_id (open_id or chat_id) for sending.
            receive_id_type: "open_id" or "chat_id".
        """
        key = (account.alias, chat_id)
        with self._lock:
            if key in self._active_chats:
                return None  # Already typing
            try:
                result = account.send_text(
                    receive_id, receive_id_type, "⏳ ...",
                )
                msg_id = result.get("message_id", "")
                self._active_chats[key] = {
                    "message_id": msg_id,
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                }
                return msg_id
            except Exception as e:
                log.debug("Typing indicator failed for %s:%s: %s",
                          account.alias, chat_id, e)
                return None

    def stop_typing(self, account: Any, chat_id: str) -> bool:
        """Delete the typing feedback message for a chat.

        Returns True when an active typing entry existed and deletion was
        attempted, even if the best-effort delete call itself failed.
        """
        key = (account.alias, chat_id)
        with self._lock:
            info = self._active_chats.pop(key, None)
        if info and info.get("message_id"):
            try:
                account.delete_message(info["message_id"])
            except Exception as e:
                log.debug("Failed to delete typing message for %s:%s: %s",
                          account.alias, chat_id, e)
            return True
        return False

    def stop_typing_by_receive(
        self, account: Any, receive_id: str, receive_id_type: str,
    ) -> None:
        """Fallback cleanup when the chat_id key isn't known.

        Used by _send on p2p (open_id) sends that fail before the chat_id
        comes back from the API, so the indicator started under the real
        chat_id at receive-time still gets cleaned up. Best-effort and
        non-failing.
        """
        with self._lock:
            matching = [
                key for key, info in self._active_chats.items()
                if key[0] == account.alias
                and info.get("receive_id") == receive_id
                and info.get("receive_id_type") == receive_id_type
            ]
            removed = [(key, self._active_chats.pop(key)) for key in matching]
        for key, info in removed:
            msg_id = info.get("message_id")
            if not msg_id:
                continue
            try:
                account.delete_message(msg_id)
            except Exception as e:
                log.debug(
                    "Failed to delete typing message for %s (receive_id=%s): %s",
                    key, receive_id, e,
                )

    def stop_all(self, accounts: dict | None = None) -> None:
        """Stop all typing indicators and delete temp messages.

        Args:
            accounts: Optional dict of alias -> FeishuAccount for cleanup.
                      If provided, temp messages are deleted before clearing.
                      If None, just clears the tracking dict (best-effort).
        """
        with self._lock:
            chats = dict(self._active_chats)
            self._active_chats.clear()

        if accounts:
            for (alias, _chat_id), info in chats.items():
                msg_id = info.get("message_id")
                if msg_id:
                    acct = accounts.get(alias)
                    if acct:
                        try:
                            acct.delete_message(msg_id)
                        except Exception as e:
                            log.debug(
                                "Failed to delete typing message %s on shutdown: %s",
                                msg_id, e,
                            )


# Global typing indicator manager
_typing_manager = TypingIndicatorManager()


# Module-level cache for WhisperModel instances to avoid reloading weights
_whisper_model_cache: dict[str, Any] = {}


def _get_whisper_model(model_name: str) -> Any:
    """Get or create a cached WhisperModel instance."""
    if model_name not in _whisper_model_cache:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is required for Feishu voice transcription; "
                "reinstall lingtai so its required dependencies are present"
            ) from e
        _whisper_model_cache[model_name] = WhisperModel(
            model_name, device="cpu", compute_type="int8"
        )
    return _whisper_model_cache[model_name]


def _transcribe_voice(audio_path: str, model_name: str = "base") -> dict:
    """Transcribe a voice/audio file using faster-whisper.

    Returns a dict with 'text' (transcript) and metadata, or an error dict.
    Uses cached WhisperModel to avoid reloading weights on every call.
    """
    try:
        whisper_model = _get_whisper_model(model_name)
        segments_iter, info = whisper_model.transcribe(audio_path)
        segments_list = list(segments_iter)

        transcript_segments = []
        for seg in segments_list:
            entry = {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            }
            transcript_segments.append(entry)

        full_text = " ".join(s["text"] for s in transcript_segments).strip()

        return {
            "text": full_text,
            "language": info.language,
            "language_probability": round(info.language_probability, 3),
            "duration": round(info.duration, 2),
            "segments": transcript_segments,
        }
    except Exception as e:
        log.warning("Voice transcription failed: %s", e)
        return {"error": str(e)}

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "send", "check", "read", "reply", "search",
                "delete", "edit",
                "contacts", "add_contact", "remove_contact",
                "accounts",
            ],
            "description": (
                "send: send a text message to a user or chat "
                "(receive_id, receive_id_type, text; optional account, placeholder). "
                "If placeholder is true, sends text as a placeholder message "
                "immediately and returns its compound message_id so the agent "
                "can call edit later with the final result. "
                "check: list recent conversations with unread counts "
                "(optional account). "
                "read: read messages from a specific chat "
                "(chat_id; optional limit, account). "
                "reply: reply to a specific message "
                "(message_id from read results, text). "
                "search: search inbox messages by regex "
                "(query; optional account, chat_id). "
                "delete: delete a bot message (message_id). "
                "edit: edit a bot message (message_id, text). "
                "contacts: list saved contacts (optional account). "
                "add_contact: save a contact "
                "(open_id, alias; optional name, chat_id). "
                "remove_contact: remove a contact (alias or open_id). "
                "accounts: list configured app accounts."
            ),
        },
        "account": {
            "type": "string",
            "description": (
                "App account alias (optional — defaults to first configured account)"
            ),
        },
        "receive_id": {
            "type": "string",
            "description": (
                "Recipient ID — open_id, user_id, email, or chat_id "
                "depending on receive_id_type"
            ),
        },
        "receive_id_type": {
            "type": "string",
            "enum": ["open_id", "user_id", "email", "chat_id", "union_id"],
            "description": (
                "Type of receive_id. Use 'open_id' for individual users "
                "(format: ou_xxx), 'chat_id' for group chats (format: oc_xxx). "
                "Defaults to 'open_id'."
            ),
        },
        "chat_id": {
            "type": "string",
            "description": "Feishu chat ID (oc_xxx for groups, or open_id for p2p)",
        },
        "text": {
            "type": "string",
            "description": "Message text content",
        },
        "message_id": {
            "type": "string",
            "description": (
                "Compound message ID returned by read/check: "
                "{alias}:{chat_id}:{feishu_message_id}"
            ),
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
        "open_id": {
            "type": "string",
            "description": "Feishu open_id for a user (ou_xxx)",
        },
        "alias": {
            "type": "string",
            "description": "Human-friendly contact alias",
        },
        "name": {
            "type": "string",
            "description": "Display name for a contact",
        },
        "placeholder": {
            "type": "boolean",
            "description": (
                "send only — send 'text' as a placeholder message immediately "
                "and return its compound message_id so the agent can call "
                "edit later with the final result. "
                "Use for long-running responses (>5s) to avoid the perception "
                "of silence."
            ),
            "default": False,
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "Feishu (Lark) bot client — interact with Feishu users and group chats. "
    "MCP OWNERSHIP: this MCP belongs to the orchestrator (admin). If you are "
    "an avatar (your admin block is empty or all admin privileges are false), "
    "do not attempt to configure or reconfigure this MCP — your orchestrator "
    "manages it, and if the network needs this MCP to reach you the wiring "
    "is propagated to your session automatically. "
    "Use 'send' for outgoing text messages (specify receive_id + receive_id_type). "
    "'check' to see recent conversations with unread counts. "
    "'read' to read messages from a specific chat (returns compound message IDs). "
    "'reply' to respond to a message (use compound ID from read results). "
    "'search' to find messages by keyword or regex. "
    "'delete' to delete a bot message (message_id). "
    "'edit' to edit a sent message (message_id, text). "
    "'contacts' to manage saved contacts (open_id aliases). "
    "'accounts' to list configured app accounts. "
    "Voice/audio messages are automatically transcribed using Whisper (local) "
    "and delivered as text. "
    "Rich feedback: automatic 'seen' emoji reaction (OK) on message receipt, "
    "'done' emoji reaction (THUMBSUP) after response is sent, "
    "and placeholder messages for long-running tasks."
)


class FeishuManager:
    """Tool handler + filesystem manager for the Feishu addon."""

    def __init__(
        self,
        service: "FeishuService",
        *,
        working_dir: Path,
        on_inbound: "Callable[[dict], None]",
    ) -> None:
        self._service = service
        self._working_dir = Path(working_dir)
        self._on_inbound = on_inbound
        # Duplicate send protection: (alias, receive_id, text) -> count
        self._last_sent: dict[tuple[str, str, str], int] = {}
        self._dup_free_passes = 2
        # Incoming event dedupe: per-account FIFO of recently-seen
        # feishu_message_id values. Protects against lark-oapi WS
        # reconnect redelivery (issue #5). Bounded; oldest evicted first.
        self._seen_msg_ids: dict[str, OrderedDict[str, None]] = {}
        self._dedupe_lock = threading.Lock()
        self._dedupe_limit = 1000

    def _account_dir(self, alias: str) -> Path:
        return self._working_dir / "feishu" / alias

    def _resolve_account(self, args: dict) -> str:
        return args.get("account") or self._service.default_account.alias

    @staticmethod
    def _parse_compound_id(compound_id: str) -> tuple[str, str, str]:
        """Parse '{alias}:{chat_id}:{feishu_message_id}' -> (alias, chat_id, msg_id)."""
        parts = compound_id.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid Feishu message ID format: {compound_id!r}")
        return parts[0], parts[1], parts[2]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._service.start()

    def stop(self) -> None:
        # Clean up any orphan typing indicator messages before stopping
        try:
            _typing_manager.stop_all(
                {alias: self._service.get_account(alias)
                 for alias in self._service.list_accounts()}
            )
        except Exception:
            pass
        self._service.stop()

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        try:
            if action == "send":
                return self._send(args)
            elif action == "check":
                return self._check(args)
            elif action == "read":
                return self._read(args)
            elif action == "reply":
                return self._reply(args)
            elif action == "search":
                return self._search(args)
            elif action == "delete":
                return self._delete(args)
            elif action == "edit":
                return self._edit(args)
            elif action == "contacts":
                return self._contacts(args)
            elif action == "add_contact":
                return self._add_contact(args)
            elif action == "remove_contact":
                return self._remove_contact(args)
            elif action == "accounts":
                return self._accounts()
            else:
                return {"error": f"Unknown feishu action: {action!r}"}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Incoming messages — called by FeishuService via on_message callback
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, account_alias: str, feishu_msg_id: str) -> bool:
        """Record `feishu_msg_id` for `account_alias` and report whether
        it was already seen. Bounded FIFO per account; oldest evicted.
        """
        with self._dedupe_lock:
            seen = self._seen_msg_ids.get(account_alias)
            if seen is None:
                seen = OrderedDict()
                self._seen_msg_ids[account_alias] = seen
            if feishu_msg_id in seen:
                return True
            seen[feishu_msg_id] = None
            while len(seen) > self._dedupe_limit:
                seen.popitem(last=False)
            return False

    def on_incoming(self, account_alias: str, data: object) -> None:
        """Persist an incoming Feishu message event to disk and notify agent."""
        try:
            event = getattr(data, "event", None)
            if event is None:
                return
            message = getattr(event, "message", None)
            sender = getattr(event, "sender", None)
            if message is None or sender is None:
                return

            feishu_msg_id: str = getattr(message, "message_id", "") or ""
            if feishu_msg_id and self._is_duplicate_event(
                account_alias, feishu_msg_id,
            ):
                log.debug(
                    "feishu dedupe: dropping replayed event %s on %s",
                    feishu_msg_id, account_alias,
                )
                return
            chat_id: str = getattr(message, "chat_id", "") or ""
            chat_type: str = getattr(message, "chat_type", "p2p") or "p2p"
            msg_type: str = getattr(message, "message_type", "text") or "text"
            content_str: str = getattr(message, "content", "{}") or "{}"
            create_time: str = getattr(message, "create_time", "") or ""
            parent_id: str = getattr(message, "parent_id", "") or ""

            sender_id = getattr(sender, "sender_id", None)
            open_id: str = (
                (getattr(sender_id, "open_id", "") or "") if sender_id else ""
            )

            # Parse content based on message type
            text = ""
            media_info: dict | None = None
            content_data: dict = {}
            try:
                content_data = json.loads(content_str)
            except (json.JSONDecodeError, AttributeError):
                content_data = {}

            if msg_type == "text":
                text = content_data.get("text", "")
            elif msg_type == "audio":
                # Audio/voice message — will be transcribed below
                text = ""
            elif msg_type == "image":
                text = content_data.get("text", "") or "[Image]"
            elif msg_type == "file":
                text = content_data.get("text", "") or "[File]"
            elif msg_type == "sticker":
                text = "[Sticker]"
            elif msg_type == "interactive":
                text = content_data.get("text", "") or "[Interactive card]"
            elif msg_type == "post":
                # Rich text — extract plain text from the post content
                post_content = content_data.get("content", {})
                title = content_data.get("title", "")
                # Flatten the post content to extract text
                text_parts = []
                if title:
                    text_parts.append(title)
                if isinstance(post_content, dict):
                    for _lang, paragraphs in post_content.items():
                        if isinstance(paragraphs, list):
                            for para in paragraphs:
                                if isinstance(para, list):
                                    for elem in para:
                                        if isinstance(elem, dict) and elem.get("tag") == "text":
                                            text_parts.append(elem.get("text", ""))
                text = " ".join(text_parts).strip() or "[Rich text message]"
            else:
                text = content_data.get("text", "") or f"[{msg_type} message]"

            if create_time:
                try:
                    ts = int(create_time) / 1000
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                except (ValueError, OSError):
                    date_str = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
            else:
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            compound_id = f"{account_alias}:{chat_id}:{feishu_msg_id}"

            payload = {
                "id": compound_id,
                "feishu_message_id": feishu_msg_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": msg_type,
                "from_open_id": open_id,
                "text": text,
                "date": date_str,
                "parent_id": parent_id,
                "media": None,
                "voice_transcript": None,
            }

            msg_uuid = str(uuid4())
            acct_dir = self._account_dir(account_alias)
            msg_dir = acct_dir / "inbox" / msg_uuid
            msg_dir.mkdir(parents=True, exist_ok=True)

            # Rich feedback: Add "seen" reaction (OK emoji) immediately
            account = None
            try:
                account = self._service.get_account(account_alias)
            except (KeyError, Exception) as e:
                log.warning("Failed to get account %s for feedback: %s",
                            account_alias, e)

            if account and feishu_msg_id:
                try:
                    account.add_reaction(feishu_msg_id, REACTION_SEEN)
                except Exception as e:
                    log.debug("Failed to add 'seen' reaction: %s", e)

            # Rich feedback: Start typing indicator
            if account and chat_id:
                # Determine receive_id for sending the typing indicator
                if chat_type == "p2p":
                    typing_receive_id = open_id
                    typing_receive_id_type = "open_id"
                else:
                    typing_receive_id = chat_id
                    typing_receive_id_type = "chat_id"
                _typing_manager.start_typing(
                    account, chat_id, typing_receive_id, typing_receive_id_type,
                )

            # Handle audio/voice messages: download and transcribe
            if msg_type == "audio":
                file_key = content_data.get("file_key", "")
                if file_key and account:
                    try:
                        log.info("Downloading audio message %s (file_key=%s)",
                                 feishu_msg_id, file_key)
                        filename, audio_data = account.get_message_resource(
                            feishu_msg_id, file_key, "file",
                        )
                        # Save to attachments directory
                        att_dir = msg_dir / "attachments"
                        att_dir.mkdir(parents=True, exist_ok=True)
                        # Ensure proper audio extension
                        if not any(filename.endswith(ext) for ext in
                                   (".ogg", ".opus", ".mp3", ".wav", ".m4a")):
                            filename = filename + ".ogg"
                        filepath = att_dir / filename
                        filepath.write_bytes(audio_data)
                        media_info = {
                            "type": "audio",
                            "filename": filename,
                            "path": str(filepath),
                            "size": len(audio_data),
                            "file_key": file_key,
                        }
                        payload["media"] = media_info

                        # Transcribe with Whisper
                        log.info("Transcribing voice message from %s (%s)",
                                 account_alias, open_id)
                        transcript = _transcribe_voice(str(filepath))
                        if "error" not in transcript:
                            text = transcript.get("text", "")
                            payload["text"] = text
                            payload["voice_transcript"] = {
                                "text": text,
                                "language": transcript.get("language"),
                                "duration": transcript.get("duration"),
                                "segments": transcript.get("segments"),
                            }
                            log.info("Voice transcription successful: %s chars",
                                     len(text))
                        else:
                            text = (
                                f"[Voice message received — transcription "
                                f"failed: {transcript.get('error', 'unknown')}]"
                            )
                            payload["text"] = text
                            log.warning("Voice transcription failed: %s",
                                        transcript.get("error"))
                    except Exception as e:
                        log.warning("Audio download/transcription failed: %s", e)
                        text = f"[Voice message received — processing failed: {e}]"
                        payload["text"] = text

            # Persist to disk
            (msg_dir / "message.json").write_text(
                json.dumps(payload, indent=2, default=str),
                encoding="utf-8",
            )

            if open_id:
                self._upsert_contact(account_alias, open_id, chat_id)

        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "on_incoming processing error (%s): %s", account_alias, exc
            )
            return

        # Forward to host via LICC. Body is a conversation preview showing
        # the last 10 rounds with a guidance header; agent uses
        # feishu(action="check"|"read") for the full conversation. Metadata
        # carries routing keys.
        display_name = self._get_contact_name(account_alias, open_id) or open_id
        try:
            preview = self._build_conversation_preview(
                account_alias, chat_id, compound_id,
            )
        except Exception as exc:
            log.warning("_build_conversation_preview failed: %s", exc)
            preview = text[:300].replace("\n", " ") if text else ""
            if len(text or "") > 300:
                preview += "..."

        log.info(
            "feishu_received account=%s sender=%r id=%s",
            account_alias, display_name, compound_id,
        )

        # Enhance subject for voice messages
        subject = f"feishu message from {display_name} via {account_alias}"
        if payload.get("voice_transcript"):
            subject = f"feishu voice message from {display_name} via {account_alias} (transcribed)"

        try:
            self._on_inbound({
                "from": display_name,
                "subject": subject,
                "body": preview if preview else "(no text — see media or callback)",
                "metadata": {
                    "message_id": compound_id,
                    "account": account_alias,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "from_open_id": open_id,
                    "preview_truncated": len(text or "") > 300,
                    "full_length": len(text or ""),
                    "has_media": payload.get("media") is not None,
                    "is_voice_transcript": payload.get("voice_transcript") is not None,
                    "voice_duration": (
                        payload.get("voice_transcript", {}).get("duration")
                        if payload.get("voice_transcript") else None
                    ),
                    "message_type": msg_type,
                },
                "wake": True,
            })
        except Exception as e:
            log.error("on_inbound callback failed for feishu msg %s: %s",
                      compound_id, e)
        # Note: typing indicator continues until _send() is called by the agent.
        # _send() stops typing when it sends the response.

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _list_messages(self, account: str, folder: str = "inbox") -> list[dict]:
        """Load all messages from a folder, sorted by date (newest first)."""
        folder_dir = self._account_dir(account) / folder
        if not folder_dir.is_dir():
            return []
        messages = []
        for msg_dir in folder_dir.iterdir():
            msg_file = msg_dir / "message.json"
            if msg_dir.is_dir() and msg_file.is_file():
                try:
                    data = json.loads(msg_file.read_text(encoding="utf-8"))
                    data["_dir"] = str(msg_dir)
                    messages.append(data)
                except (json.JSONDecodeError, OSError):
                    continue
        messages.sort(key=lambda m: m.get("date") or m.get("sent_at") or "", reverse=True)
        return messages

    def _build_conversation_preview(
        self,
        account_alias: str,
        chat_id: str,
        current_compound_id: str,
        max_messages: int = 10,
    ) -> str:
        """Build a markdown conversation preview of the last *max_messages* rounds.

        Scans inbox/ and sent/ dirs for messages matching *chat_id*, sorts by
        date ascending, takes the tail, and prepends a guidance header that
        tells the receiving agent how to interpret the preview. Reply lines
        are quoted beneath their parent (truncated to 50 chars).
        """
        now = datetime.now(timezone.utc)

        def _rel_time(date_str: str) -> str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                return date_str or "?"
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

        acct_dir = self._account_dir(account_alias)
        messages: list[dict] = []
        for folder in ("inbox", "sent"):
            folder_dir = acct_dir / folder
            if not folder_dir.is_dir():
                continue
            for msg_dir in folder_dir.iterdir():
                msg_file = msg_dir / "message.json"
                if not (msg_dir.is_dir() and msg_file.is_file()):
                    continue
                try:
                    data = json.loads(msg_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("chat_id") != chat_id:
                    continue
                data["_folder"] = folder
                messages.append(data)

        messages.sort(key=lambda m: m.get("date") or m.get("sent_at") or "")
        messages = messages[-max_messages:]

        by_id: dict[str, dict] = {m.get("id", ""): m for m in messages}

        def _sender_name(m: dict) -> str:
            if m.get("_folder") == "sent":
                return "me"
            open_id = m.get("from_open_id", "") or ""
            return self._get_contact_name(account_alias, open_id) or open_id or "unknown"

        lines: list[str] = []
        for m in messages:
            cid = m.get("id", "")
            rel = _rel_time(m.get("date") or m.get("sent_at") or "")
            sender = _sender_name(m)
            text = m.get("text", "") or ""
            if m.get("media"):
                media_type = m["media"].get("type", "media")
                text = text or f"[{media_type}]"
            text_display = text.replace("\n", " ")

            line = f"[{rel}] #{cid} {sender}: {text_display}"
            lines.append(line)

            parent_id = m.get("parent_id")
            if parent_id:
                id_parts = cid.split(":", 2)
                if len(id_parts) == 3:
                    parent_compound = f"{id_parts[0]}:{id_parts[1]}:{parent_id}"
                    orig = by_id.get(parent_compound)
                    if orig:
                        orig_rel = _rel_time(orig.get("date", ""))
                        orig_text = orig.get("text", "") or ""
                        orig_snippet = orig_text[:50]
                        if len(orig_text) > 50:
                            orig_snippet += "…"
                        lines.append(
                            f"  ↳ [{orig_rel}] #{parent_compound}: {orig_snippet}"
                        )

        header = _NOTIFICATION_HEADER_TEMPLATE.format(channel="Feishu").rstrip("\n")
        tail = f"**Conversation — last {len(messages)} messages (chat {chat_id})**"
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

    def _read_ids(self, account: str) -> set[str]:
        path = self._account_dir(account) / "read.json"
        if path.is_file():
            try:
                return set(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                return set()
        return set()

    def _mark_read(self, account: str, compound_ids: list[str]) -> None:
        ids = self._read_ids(account)
        ids.update(compound_ids)
        acct_dir = self._account_dir(account)
        acct_dir.mkdir(parents=True, exist_ok=True)
        target = acct_dir / "read.json"
        fd, tmp = tempfile.mkstemp(dir=str(acct_dir), suffix=".tmp")
        try:
            os.write(fd, json.dumps(sorted(ids)).encode())
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _load_contacts(self, account: str) -> dict:
        path = self._account_dir(account) / "contacts.json"
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_contacts(self, account: str, contacts: dict) -> None:
        acct_dir = self._account_dir(account)
        acct_dir.mkdir(parents=True, exist_ok=True)
        target = acct_dir / "contacts.json"
        fd, tmp = tempfile.mkstemp(dir=str(acct_dir), suffix=".tmp")
        try:
            os.write(fd, json.dumps(contacts, indent=2).encode())
            os.close(fd)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _upsert_contact(
        self, account: str, open_id: str, chat_id: str = ""
    ) -> None:
        contacts = self._load_contacts(account)
        existing = contacts.get(open_id, {})
        if not existing.get("chat_id") and chat_id:
            existing["chat_id"] = chat_id
        contacts[open_id] = existing
        self._save_contacts(account, contacts)

    def _get_contact_name(self, account: str, open_id: str) -> str:
        contacts = self._load_contacts(account)
        info = contacts.get(open_id, {})
        return info.get("name") or info.get("alias") or ""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _send(self, args: dict) -> dict:
        account = self._resolve_account(args)
        receive_id = args.get("receive_id", "")
        receive_id_type = args.get("receive_id_type", "open_id")
        text = args.get("text", "")
        placeholder = bool(args.get("placeholder", False))

        if not receive_id:
            return {"error": "receive_id is required"}
        if not text:
            return {"error": "text is required"}

        dup_key = (account, receive_id, text)
        count = self._last_sent.get(dup_key, 0)
        if count >= self._dup_free_passes:
            return {
                "status": "blocked",
                "warning": "Identical message already sent. Think twice before repeating.",
            }

        acct = self._service.get_account(account)
        # chat_id for typing cleanup — resolved after send, but if
        # receive_id_type is "chat_id" we already know it.
        chat_id: str = receive_id if receive_id_type == "chat_id" else ""

        try:
            result = acct.send_text(receive_id, receive_id_type, text)

            self._last_sent[dup_key] = count + 1

            feishu_msg_id = result.get("message_id", "")
            chat_id = result.get("chat_id", receive_id)
            compound_id = f"{account}:{chat_id}:{feishu_msg_id}"
            sent_uuid = str(uuid4())
            sent_dir = self._account_dir(account) / "sent" / sent_uuid
            sent_dir.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sent_record = {
                "id": compound_id,
                "feishu_message_id": feishu_msg_id,
                "to": {"receive_id": receive_id, "receive_id_type": receive_id_type},
                "chat_id": chat_id,
                "text": text,
                "sent_at": now_iso,
                # `date` mirrors `sent_at` so sent records sort alongside
                # inbox records in _check/_read merges.
                "date": now_iso,
                "status": "placeholder" if placeholder else "sent",
            }
            (sent_dir / "message.json").write_text(
                json.dumps(sent_record, indent=2, default=str),
                encoding="utf-8",
            )

            response: dict[str, Any] = {
                "status": "sent",
                "message_id": compound_id,
            }
            if placeholder:
                response["placeholder"] = True
                response["hint"] = (
                    f"Placeholder sent — call feishu(action='edit', "
                    f"message_id='{compound_id}', text=<final>) when ready."
                )

            return response
        finally:
            # Always clean up typing indicator, even if send_text or
            # downstream logic throws. For chat_id-type receives we
            # already know the key; for open_id we get it from the result
            # (or fall back to a receive_id-based lookup if send failed
            # before the API returned the real chat_id).
            if chat_id:
                _typing_manager.stop_typing(acct, chat_id)
            else:
                _typing_manager.stop_typing_by_receive(
                    acct, receive_id, receive_id_type,
                )

    @staticmethod
    def _is_outgoing_record(m: dict) -> bool:
        return "to" in m or m.get("status") in {"sent", "placeholder"}

    def _check(self, args: dict) -> dict:
        account = self._resolve_account(args)
        # Merge inbox + sent so post-molt agents see their own replies and
        # don't re-send. Sort newest first so the first entry per chat is
        # the most recent — that drives `last_*` fields.
        inbox = self._list_messages(account, "inbox")
        sent = self._list_messages(account, "sent")
        messages = inbox + sent
        messages.sort(key=lambda m: m.get("date") or m.get("sent_at") or "", reverse=True)
        read_ids = self._read_ids(account)

        conversations: dict[str, dict] = {}
        for msg in messages:
            cid = msg.get("chat_id", "")
            if cid not in conversations:
                if self._is_outgoing_record(msg):
                    last_from_open_id = ""
                    name = "me"
                else:
                    last_from_open_id = msg.get("from_open_id", "")
                    name = self._get_contact_name(account, last_from_open_id)
                conversations[cid] = {
                    "chat_id": cid,
                    "chat_type": msg.get("chat_type", "p2p"),
                    "last_from_open_id": last_from_open_id,
                    "last_from_name": name,
                    "last_text": (msg.get("text") or "")[:100],
                    "last_date": msg.get("date", ""),
                    "total": 0,
                    "unread": 0,
                }
            conversations[cid]["total"] += 1
            # Only inbound messages can be unread.
            if (
                not self._is_outgoing_record(msg)
                and msg.get("id")
                and msg["id"] not in read_ids
            ):
                conversations[cid]["unread"] += 1

        return {
            "status": "ok",
            "total": len(messages),
            "conversations": list(conversations.values()),
        }

    def _read(self, args: dict) -> dict:
        account = self._resolve_account(args)
        chat_id = args.get("chat_id", "")
        limit = args.get("limit", 10)

        if not chat_id:
            return {"error": "chat_id is required"}

        # Merge inbox + sent so post-molt agents see their own outgoing
        # replies and avoid duplicate sends.
        inbox = self._list_messages(account, "inbox")
        sent = self._list_messages(account, "sent")
        combined = inbox + sent
        combined.sort(key=lambda m: m.get("date") or m.get("sent_at") or "", reverse=True)
        filtered = [m for m in combined if m.get("chat_id") == chat_id]
        recent = filtered[:limit]

        # Only mark inbound messages as read; sent records have no unread state.
        compound_ids = [
            m["id"] for m in recent if m.get("id") and not self._is_outgoing_record(m)
        ]
        if compound_ids:
            self._mark_read(account, compound_ids)

        cleaned = []
        for m in recent:
            outgoing = self._is_outgoing_record(m)
            name = (
                "me" if outgoing
                else self._get_contact_name(account, m.get("from_open_id", ""))
            )
            cleaned.append({
                "id": m.get("id"),
                "feishu_message_id": m.get("feishu_message_id"),
                "chat_id": m.get("chat_id"),
                "chat_type": m.get("chat_type"),
                "from_open_id": m.get("from_open_id"),
                "from_name": name,
                "to": m.get("to"),
                "message_type": m.get("message_type"),
                "text": m.get("text"),
                "date": m.get("date"),
                "parent_id": m.get("parent_id"),
                "media": m.get("media"),
                "voice_transcript": m.get("voice_transcript"),
                "_direction": "outgoing" if outgoing else "incoming",
            })

        return {"status": "ok", "messages": cleaned}

    def _reply(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        text = args.get("text", "")
        if not compound_id:
            return {"error": "message_id is required"}
        if not text:
            return {"error": "text is required"}

        alias, _chat_id, feishu_msg_id = self._parse_compound_id(compound_id)
        acct = self._service.get_account(alias)

        try:
            result = acct.reply_text(feishu_msg_id, text)

            new_msg_id = result.get("message_id", "")
            new_chat_id = result.get("chat_id", _chat_id)
            new_compound = f"{alias}:{new_chat_id}:{new_msg_id}"
            sent_uuid = str(uuid4())
            sent_dir = self._account_dir(alias) / "sent" / sent_uuid
            sent_dir.mkdir(parents=True, exist_ok=True)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            sent_record = {
                "id": new_compound,
                "feishu_message_id": new_msg_id,
                "reply_to": compound_id,
                "to": {"receive_id": new_chat_id, "receive_id_type": "chat_id"},
                "chat_id": new_chat_id,
                "text": text,
                "sent_at": now_iso,
                "date": now_iso,
                "status": "sent",
            }
            (sent_dir / "message.json").write_text(
                json.dumps(sent_record, indent=2, default=str),
                encoding="utf-8",
            )

            # Rich feedback: Add "done" reaction (THUMBSUP) to the original message
            try:
                acct.add_reaction(feishu_msg_id, REACTION_DONE)
            except Exception as e:
                log.debug("Failed to add 'done' reaction: %s", e)

            return {"status": "sent", "message_id": new_compound}
        finally:
            # Always clean up typing indicator, even if reply_text or
            # downstream logic throws. Some historical compound IDs can have
            # an empty chat_id segment, leaving no usable cleanup key.
            if not _chat_id:
                log.debug(
                    "Skipping reply typing cleanup with no chat_id for %s",
                    compound_id,
                )
            elif not _typing_manager.stop_typing(acct, _chat_id):
                log.debug(
                    "No reply typing indicator found for %s:%s:%s",
                    alias, _chat_id, feishu_msg_id,
                )

    def _search(self, args: dict) -> dict:
        query = args.get("query", "")
        if not query:
            return {"error": "query is required"}
        account = self._resolve_account(args)
        target_chat = args.get("chat_id", "")

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        messages = self._list_messages(account, "inbox")
        matches = []
        for msg in messages:
            if target_chat and msg.get("chat_id") != target_chat:
                continue
            name = self._get_contact_name(account, msg.get("from_open_id", ""))
            searchable = " ".join([
                msg.get("from_open_id", ""),
                name,
                msg.get("text", ""),
            ])
            if pattern.search(searchable):
                matches.append({
                    "id": msg.get("id"),
                    "from_open_id": msg.get("from_open_id"),
                    "from_name": name,
                    "chat_id": msg.get("chat_id"),
                    "date": msg.get("date"),
                    "text": msg.get("text"),
                })

        return {"status": "ok", "total": len(matches), "messages": matches}

    def _delete(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        if not compound_id:
            return {"error": "message_id is required"}
        alias, _chat_id, feishu_msg_id = self._parse_compound_id(compound_id)
        acct = self._service.get_account(alias)
        acct.delete_message(feishu_msg_id)
        return {"status": "deleted", "message_id": compound_id}

    def _edit(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        text = args.get("text", "")
        if not compound_id:
            return {"error": "message_id is required"}
        if not text:
            return {"error": "text is required"}
        alias, _chat_id, feishu_msg_id = self._parse_compound_id(compound_id)
        acct = self._service.get_account(alias)
        acct.update_message(feishu_msg_id, text)
        return {"status": "edited", "message_id": compound_id}

    def _contacts(self, args: dict) -> dict:
        account = self._resolve_account(args)
        return {"status": "ok", "contacts": self._load_contacts(account)}

    def _add_contact(self, args: dict) -> dict:
        account = self._resolve_account(args)
        open_id = args.get("open_id", "")
        alias = args.get("alias", "")
        if not open_id:
            return {"error": "open_id is required"}
        if not alias:
            return {"error": "alias is required"}
        contacts = self._load_contacts(account)
        contacts[open_id] = {
            "alias": alias,
            "name": args.get("name", alias),
            "chat_id": args.get("chat_id", ""),
        }
        self._save_contacts(account, contacts)
        return {"status": "added", "open_id": open_id, "alias": alias}

    def _remove_contact(self, args: dict) -> dict:
        account = self._resolve_account(args)
        open_id = args.get("open_id", "")
        alias = args.get("alias", "")
        contacts = self._load_contacts(account)

        if open_id and open_id in contacts:
            del contacts[open_id]
            self._save_contacts(account, contacts)
            return {"status": "removed", "open_id": open_id}
        elif alias:
            to_remove = [
                oid for oid, v in contacts.items() if v.get("alias") == alias
            ]
            for oid in to_remove:
                del contacts[oid]
            if to_remove:
                self._save_contacts(account, contacts)
                return {"status": "removed", "open_ids": to_remove}
        return {"error": "Contact not found"}

    def _accounts(self) -> dict:
        return {
            "status": "ok",
            "accounts": self._service.list_accounts(),
            "details": self._service.account_details(),
            "identity_path": str(self._service.identity_path()),
        }
