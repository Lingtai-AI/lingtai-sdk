# src/lingtai/addons/telegram/manager.py
"""TelegramManager — tool dispatch + filesystem persistence.

Storage layout:
    working_dir/telegram/{account}/inbox/{uuid}/message.json
    working_dir/telegram/{account}/inbox/{uuid}/attachments/
    working_dir/telegram/{account}/sent/{uuid}/message.json
    working_dir/telegram/{account}/contacts.json
    working_dir/telegram/{account}/read.json

Mirrors IMAPMailManager patterns with Telegram-specific adaptations.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

import logging
import threading

if TYPE_CHECKING:
    from .service import TelegramService

log = logging.getLogger(__name__)


def _load_notification_header_template() -> str:
    return resources.files(__package__).joinpath("notification_header.md").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Bundled usage manual (skill format) — SKILL.md ships in this package folder.
# action='manual' reads the full body; the YAML frontmatter is parsed and the
# name/description are injected into the tool schema as a progressive-disclosure
# catalog entry, while the full body stays behind action='manual'.
# ---------------------------------------------------------------------------

_SKILL_FILENAME = "SKILL.md"


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body markdown).

    Tiny dependency-free parser for the leading ``---`` YAML block. Handles the
    flat ``key: value`` and ``key: |``/``key: >`` block-scalar forms used by our
    skill frontmatter; it does not attempt to be a general YAML parser. Returns
    an empty mapping and the original text when no frontmatter is present.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening '---'; find the closing fence.
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n").strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    fm_lines = [ln.rstrip("\n") for ln in lines[1:end]]
    body = "".join(lines[end + 1:]).lstrip("\n")

    meta: dict[str, str] = {}
    key: str | None = None
    block_parts: list[str] = []

    def _flush() -> None:
        if key is not None:
            meta[key] = " ".join(" ".join(block_parts).split())

    for raw in fm_lines:
        if key is not None and (raw.startswith((" ", "\t")) or not raw.strip()):
            # Continuation line of a block scalar (key: | / key: >).
            block_parts.append(raw.strip())
            continue
        if ":" in raw and not raw.startswith((" ", "\t")):
            _flush()
            k, _, v = raw.partition(":")
            key = k.strip()
            block_parts = []
            v = v.strip()
            if v in ("|", ">", "|-", ">-", "|+", ">+"):
                # Block scalar — value continues on indented lines below.
                continue
            block_parts.append(v.strip("'\""))
    _flush()
    return meta, body


def _load_skill() -> tuple[dict[str, str], str, str]:
    """Load the bundled SKILL.md → (frontmatter, body, path)."""
    resource = resources.files(__package__).joinpath(_SKILL_FILENAME)
    text = resource.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    return frontmatter, body, str(resource)


_SKILL_FRONTMATTER, _SKILL_BODY, _SKILL_PATH = _load_skill()


def _manual_action_description() -> str:
    """Build the schema 'manual' action line from the skill frontmatter.

    Injects the skill name + description (the frontmatter/catalog entry) so the
    tool schema advertises the skill while the full body stays progressive-
    disclosure behind action='manual'.
    """
    name = _SKILL_FRONTMATTER.get("name", "telegram-mcp-manual")
    description = _SKILL_FRONTMATTER.get("description", "")
    return (
        f"manual: progressive-disclosure usage manual (skill '{name}') — "
        "call this (no other args) to pull the full bundled SKILL.md. "
        f"{description}"
    ).strip()


_NOTIFICATION_HEADER_TEMPLATE = _load_notification_header_template()
_NOTIFICATION_CHANNEL = "mcp.telegram"
_COMPOUND_ID_RE = re.compile(r"#([^\s:#]+:-?\d+:\d+)\b")


def _looks_like_compound_id(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 3 or not parts[0]:
        return False
    try:
        int(parts[1])
        int(parts[2])
    except ValueError:
        return False
    return True


# Emoji reactions for different states (Bot API 7.0+)
REACTION_SEEN = [{"type": "emoji", "emoji": "👀"}]      # Message received
REACTION_DONE = [{"type": "emoji", "emoji": "✅"}]       # Response sent


class TypingIndicatorManager:
    """Manages automatic typing indicators for Telegram chats.

    Sends typing indicator immediately, then re-sends every 5 seconds
    (Telegram auto-expires them). Best-effort — never blocks or fails.
    """

    def __init__(self) -> None:
        self._active_chats: dict[tuple[str, int], threading.Event] = {}
        self._lock = threading.Lock()

    def start_typing(self, account: Any, chat_id: int) -> None:
        """Start sending typing indicators for a chat."""
        key = (account.alias, chat_id)
        with self._lock:
            if key in self._active_chats:
                return  # Already typing
            stop_event = threading.Event()
            self._active_chats[key] = stop_event

        def _typing_loop() -> None:
            while not stop_event.is_set():
                try:
                    account.send_chat_action(chat_id, "typing")
                except Exception as e:
                    log.debug("Typing indicator failed for %s:%s: %s",
                              account.alias, chat_id, e)
                # Wait 4 seconds (Telegram expires at 5s)
                stop_event.wait(4.0)
            # Clean up
            with self._lock:
                self._active_chats.pop(key, None)

        thread = threading.Thread(
            target=_typing_loop,
            daemon=True,
            name=f"typing-{account.alias}-{chat_id}",
        )
        thread.start()

    def stop_typing(self, account: Any, chat_id: int) -> None:
        """Stop sending typing indicators for a chat."""
        key = (account.alias, chat_id)
        with self._lock:
            stop_event = self._active_chats.get(key)
        if stop_event:
            stop_event.set()

    def stop_all(self) -> None:
        """Stop all typing indicators."""
        with self._lock:
            for stop_event in self._active_chats.values():
                stop_event.set()
            self._active_chats.clear()


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
                "faster-whisper is required for Telegram voice transcription; "
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
                "accounts", "manual",
            ],
            "description": (
                "send: send message to a chat (chat_id, text; optional media, reply_markup, placeholder, chat_action, parse_mode/entities). "
                "For charts, reports, generated artifacts, and other files the user should open intact, prefer media.type='document'; use media.type='photo' only when an inline Telegram photo preview is desired, because photo previews may crop, compress, or display poorly for text-heavy graphics. "
                "If chat_action is set and no text/media is provided, sends a typing "
                "indicator (auto-expires after 5s) instead of a message. "
                "check: list recent conversations with unread counts (optional account). "
                "read: read messages from a chat (chat_id; optional limit). "
                "reply: reply to a specific message (message_id from read results, text; optional parse_mode/entities). "
                "search: search messages (query; optional account, chat_id). "
                "delete: delete a bot message (message_id). "
                "edit: edit a bot message (message_id, text; optional reply_markup, parse_mode/entities). "
                "contacts: list saved contacts. "
                "add_contact: save a chat alias (chat_id, alias); this does not grant inbound permission. "
                "To receive messages from that user, their Telegram user ID must also be in allowed_users. "
                "remove_contact: remove a contact (alias or chat_id). "
                "accounts: list configured bot accounts. "
                + _manual_action_description()
            ),
        },
        "account": {
            "type": "string",
            "description": "Bot account alias (optional — defaults to first configured account)",
        },
        "chat_id": {
            "type": "integer",
            "description": "Telegram chat ID",
        },
        "text": {
            "type": "string",
            "description": "Message text",
        },
        "message_id": {
            "type": "string",
            "description": "Compound message ID: {account}:{chat_id}:{message_id}",
        },
        "media": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["photo", "document", "voice", "audio"]},
                "path": {"type": "string"},
            },
            "description": (
                "Media attachment: {type: 'photo'|'document'|'voice'|'audio', path: '/path/to/file'}. "
                "For charts, HTML/SVG/PNG reports, CSVs, PDFs, and other generated artifacts that should arrive as an intact file, use type='document'. "
                "Use type='photo' only for native inline photo previews; Telegram photo delivery can crop, compress, thumbnail, or otherwise display text-heavy charts poorly. "
                "Do not paste local file paths in message text as a substitute for attaching the file."
            ),
        },
        "reply_markup": {
            "type": "object",
            "description": "Inline keyboard markup",
        },
        "parse_mode": {
            "type": "string",
            "enum": ["HTML", "MarkdownV2", "Markdown", ""],
            "description": (
                "Telegram Bot API parse_mode for rich text (send/reply/edit, "
                "and media captions). Omit or pass an empty string for plain text."
            ),
        },
        "entities": {
            "type": "array",
            "description": "Telegram MessageEntity[] for message text formatting (send/reply/edit).",
        },
        "caption_entities": {
            "type": "array",
            "description": "Telegram MessageEntity[] for media captions.",
        },
        "link_preview_options": {
            "type": "object",
            "description": "Telegram LinkPreviewOptions for text messages.",
        },
        "disable_web_page_preview": {
            "type": "boolean",
            "description": "Compatibility shortcut to disable link previews for text messages.",
        },
        "placeholder": {
            "type": "boolean",
            "description": (
                "send only — send 'text' as a placeholder message immediately "
                "and return its compound message_id so the agent can call "
                "edit later with the final result. Also fires a typing chat "
                "action so the user sees 'is typing…' while the agent works. "
                "Use for long-running responses (>5s) to avoid the perception "
                "of silence."
            ),
            "default": False,
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
            "description": "Contact alias for add_contact/remove_contact",
        },
        "chat_action": {
            "type": "string",
            "enum": ["typing", "upload_photo", "upload_document", "upload_voice", ""],
            "description": (
                "For send action only. When set and no text/media is provided, "
                "sends a chat action indicator (e.g. 'typing...') instead of a "
                "message. Auto-expires after 5 seconds — re-send periodically "
                "during long tasks to keep it visible. Omit or pass an empty "
                "string for no chat action."
            ),
        },
    },
    "required": ["action"],
}

DESCRIPTION = (
    "Telegram bot client — interact with Telegram users via Bot API. "
    "MCP OWNERSHIP: this MCP belongs to the orchestrator (admin). If you are "
    "an avatar (your admin block is empty or all admin privileges are false), "
    "do not attempt to configure or reconfigure this MCP — your orchestrator "
    "manages it, and if the network needs this MCP to reach you the wiring "
    "is propagated to your session automatically. "
    "Use 'send' for outgoing messages (text, photos, documents, inline keyboards, rich formatting). "
    "'check' to see recent conversations. "
    "'read' to read messages from a specific chat. "
    "'reply' to respond to a message (use compound ID from read results). "
    "'search' to find messages by text/sender. "
    "'delete'/'edit' to modify bot messages. "
    "'contacts' to manage saved contacts. "
    "'accounts' to list configured bot accounts. "
    "Voice messages are automatically transcribed using Whisper (local) and delivered as text. "
    "Rich feedback: automatic typing indicators, emoji reactions (👀 seen, ✅ done), "
    "and progress messages for long-running tasks."
)


class TelegramManager:
    """Tool handler + filesystem manager for the Telegram addon."""

    def __init__(
        self,
        service: "TelegramService",
        *,
        working_dir: Path,
        on_inbound: "Callable[[dict], None]",
    ) -> None:
        self._service = service
        self._working_dir = Path(working_dir)
        self._on_inbound = on_inbound
        # Duplicate send protection: (account, chat_id, text) → count
        self._last_sent: dict[tuple[str, int, str], int] = {}
        self._dup_free_passes = 2

    def _account_dir(self, account: str) -> Path:
        return self._working_dir / "telegram" / account

    def _resolve_account(self, args: dict) -> str:
        """Get account alias from args, defaulting to first account."""
        return args.get("account") or self._service.default_account.alias

    @staticmethod
    def _parse_compound_id(compound_id: str) -> tuple[str, int, int]:
        """Parse '{account}:{chat_id}:{message_id}' → (account, chat_id, message_id)."""
        parts = compound_id.split(":")
        if len(parts) != 3:
            raise ValueError(f"Invalid message ID format: {compound_id}")
        return parts[0], int(parts[1]), int(parts[2])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._service.start()

    def stop(self) -> None:
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
            elif action == "manual":
                return self._manual()
            else:
                return {"error": f"Unknown telegram action: {action}"}
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Incoming messages — called by TelegramService via on_message
    # ------------------------------------------------------------------

    def on_incoming(self, account_alias: str, update: dict) -> None:
        """Persist incoming update to disk and notify agent."""
        msg_id = str(uuid4())
        acct_dir = self._account_dir(account_alias)
        msg_dir = acct_dir / "inbox" / msg_id
        msg_dir.mkdir(parents=True, exist_ok=True)

        # Issue #8: Rich intermediate feedback
        # Get account and chat_id for typing indicator and reactions
        try:
            account = self._service.get_account(account_alias)
        except (KeyError, Exception) as e:
            log.warning("Failed to get account %s for feedback: %s", account_alias, e)
            account = None
        chat_id = None
        tg_message_id = None

        # Extract message data based on update type
        if "message" in update:
            tg_msg = update["message"]
            chat_id = tg_msg["chat"]["id"]
            tg_message_id = tg_msg["message_id"]
            compound_id = f"{account_alias}:{chat_id}:{tg_message_id}"
            sender = tg_msg.get("from", {})
            payload = {
                "id": compound_id,
                "from": sender,
                "chat": tg_msg.get("chat", {}),
                "date": datetime.fromtimestamp(
                    tg_msg.get("date", 0), tz=timezone.utc,
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "text": tg_msg.get("text") or tg_msg.get("caption") or "",
                "media": None,
                "reply_to_message_id": None,
                "callback_query": None,
            }
            # Handle reply_to
            if tg_msg.get("reply_to_message"):
                payload["reply_to_message_id"] = tg_msg["reply_to_message"]["message_id"]
            # Handle media
            self._download_media(account_alias, tg_msg, msg_dir, payload)

            # Get username before voice transcription (needed for logging)
            username = sender.get("username") or sender.get("first_name", "unknown")

            # Issue #8: Start typing indicator immediately
            if account:
                _typing_manager.start_typing(account, chat_id)

            # Issue #8: Add "seen" reaction (👀)
            if account:
                try:
                    account.set_message_reaction(chat_id, tg_message_id, REACTION_SEEN)
                except Exception as e:
                    log.debug("Failed to add 'seen' reaction: %s", e)

            # Issue #6: Transcribe voice messages
            if payload.get("media") and payload["media"].get("type") in ("voice", "audio"):
                audio_path = payload["media"].get("path")
                if audio_path and Path(audio_path).exists():
                    log.info("Transcribing voice message from %s:%s", account_alias, username)
                    transcript = _transcribe_voice(audio_path)
                    if "error" not in transcript:
                        payload["text"] = transcript.get("text", "")
                        payload["voice_transcript"] = {
                            "text": transcript.get("text", ""),
                            "language": transcript.get("language"),
                            "duration": transcript.get("duration"),
                            "segments": transcript.get("segments"),
                        }
                        log.info("Voice transcription successful: %s chars", len(payload["text"]))
                    else:
                        # Graceful fallback: indicate transcription failed
                        payload["text"] = f"[Voice message received — transcription failed: {transcript.get('error', 'unknown error')}]"
                        log.warning("Voice transcription failed: %s", transcript.get("error"))

        elif "callback_query" in update:
            cq = update["callback_query"]
            tg_msg = cq.get("message", {})
            sender = cq.get("from", {})
            chat = tg_msg.get("chat", {})
            chat_id = chat.get("id", 0)
            tg_message_id = tg_msg.get("message_id", 0)
            compound_id = f"{account_alias}:{chat_id}:{tg_message_id}"
            payload = {
                "id": compound_id,
                "from": sender,
                "chat": chat,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "text": "",
                "media": None,
                "reply_to_message_id": None,
                "callback_query": cq.get("data"),
            }
            username = sender.get("username") or sender.get("first_name", "unknown")

            # Issue #8: Start typing indicator for callback queries
            if chat_id and account:
                _typing_manager.start_typing(account, chat_id)

        elif "edited_message" in update:
            tg_msg = update["edited_message"]
            compound_id = f"{account_alias}:{tg_msg['chat']['id']}:{tg_msg['message_id']}"
            sender = tg_msg.get("from", {})
            payload = {
                "id": compound_id,
                "from": sender,
                "chat": tg_msg.get("chat", {}),
                "date": datetime.fromtimestamp(
                    tg_msg.get("edit_date", tg_msg.get("date", 0)), tz=timezone.utc,
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "text": tg_msg.get("text") or tg_msg.get("caption") or "",
                "media": None,
                "reply_to_message_id": None,
                "callback_query": None,
            }
            username = sender.get("username") or sender.get("first_name", "unknown")

            # Update existing inbox entry in-place if found
            existing_dir = self._find_inbox_by_compound_id(account_alias, compound_id)
            if existing_dir is not None:
                (existing_dir / "message.json").write_text(
                    json.dumps(payload, indent=2, default=str), encoding="utf-8",
                )
                # Clean up the unused new dir
                msg_dir.rmdir()
            else:
                log.info(
                    "telegram unmatched edited_message account=%s id=%s; skipping orphan inbox write",
                    account_alias,
                    compound_id,
                )
                try:
                    msg_dir.rmdir()
                except OSError as exc:
                    log.debug(
                        "failed to remove unused edited_message dir %s: %s",
                        msg_dir,
                        exc,
                    )
                return
        else:
            return  # unsupported update type

        # Persist (for message and callback_query types)
        if "edited_message" not in update:
            (msg_dir / "message.json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8",
            )

        # Forward to host via LICC. Body is a conversation preview showing the
        # last 10 rounds. The agent uses telegram(action="check"|"read") to
        # fetch the full conversation; metadata carries the routing keys.
        text = payload.get("text", "") or payload.get("callback_query", "") or ""
        try:
            preview = self._build_conversation_preview(
                account_alias,
                payload.get("chat", {}).get("id"),
                compound_id,
            )
        except Exception as exc:
            log.warning("_build_conversation_preview failed: %s", exc)
            preview = text[:300].replace("\n", " ")
            if len(text) > 300:
                preview += "..."

        log.info(
            "telegram_received account=%s sender=%r id=%s",
            account_alias, username, payload.get("id"),
        )

        # Update type lets agents dispatch (e.g. button press vs free text).
        if "callback_query" in update:
            update_type = "callback_query"
        elif "edited_message" in update:
            update_type = "edited_message"
        else:
            update_type = "message"

        # Issue #5: Don't wake the agent for edited messages — they are
        # typically trivial corrections (typo fixes) and not worth a wake.
        # The inbox entry is still updated in-place so the agent sees the
        # latest content on next read.
        should_wake = update_type != "edited_message"

        # Issue #6: Enhance subject for voice messages
        subject = f"telegram {update_type} from {username} via {account_alias}"
        if payload.get("voice_transcript"):
            subject = f"telegram voice message from {username} via {account_alias} (transcribed)"

        try:
            self._on_inbound({
                "from": username,
                "subject": subject,
                "body": preview if preview else "(no text — see media or callback)",
                "metadata": {
                    "type": update_type,
                    "message_id": payload.get("id"),
                    "account": account_alias,
                    "chat_id": payload.get("chat", {}).get("id"),
                    # LICC preview metadata copied into .notification/mcp.telegram.json.
                    # Keep both the legacy Telegram-specific keys above and the
                    # generic chat keys below so the producer can later clear a
                    # handled notification mirror without re-reading Telegram.
                    "platform": "telegram",
                    "conversation_ref": f"{account_alias}:{payload.get('chat', {}).get('id')}",
                    # Callback queries reuse the message_id of the inline-keyboard
                    # message, so the compound ID is not unique per callback event.
                    # Leave those mirrors for explicit handling rather than
                    # auto-clearing a fresh callback because an older callback on
                    # the same Telegram message was already read.
                    "message_ref": payload.get("id") if update_type != "callback_query" else None,
                    "has_media": payload.get("media") is not None,
                    "has_callback": payload.get("callback_query") is not None,
                    "callback_data": payload.get("callback_query"),
                    "is_voice_transcript": payload.get("voice_transcript") is not None,
                    "voice_duration": payload.get("voice_transcript", {}).get("duration") if payload.get("voice_transcript") else None,
                },
                "wake": should_wake,
            })
        except Exception as e:
            log.error("on_inbound callback failed for telegram msg %s: %s",
                      payload.get("id"), e)
        # Note: typing indicator continues until _send() is called by the agent.
        # _send() stops typing when it sends the response.

    def _download_media(
        self, account_alias: str, tg_msg: dict, msg_dir: Path, payload: dict,
    ) -> None:
        """Download photo/document/voice/audio attachments from a Telegram message."""
        file_id = None
        media_type = None
        media_meta: dict = {}

        if tg_msg.get("photo"):
            # Photos come as array of sizes — take the largest
            file_id = tg_msg["photo"][-1]["file_id"]
            media_type = "photo"
        elif tg_msg.get("document"):
            file_id = tg_msg["document"]["file_id"]
            media_type = "document"
        elif tg_msg.get("voice"):
            # Voice messages: .oga format, typically short recordings
            file_id = tg_msg["voice"]["file_id"]
            media_type = "voice"
            media_meta = {
                "duration": tg_msg["voice"].get("duration", 0),
                "mime_type": tg_msg["voice"].get("mime_type", "audio/ogg"),
            }
        elif tg_msg.get("audio"):
            # Audio files: music, longer recordings, etc.
            file_id = tg_msg["audio"]["file_id"]
            media_type = "audio"
            media_meta = {
                "duration": tg_msg["audio"].get("duration", 0),
                "mime_type": tg_msg["audio"].get("mime_type", "audio/mpeg"),
                "title": tg_msg["audio"].get("title"),
                "performer": tg_msg["audio"].get("performer"),
            }

        if file_id is None:
            return

        try:
            acct = self._service.get_account(account_alias)
            filename, data = acct.get_file(file_id)
            att_dir = msg_dir / "attachments"
            att_dir.mkdir(parents=True, exist_ok=True)
            filepath = att_dir / filename
            filepath.write_bytes(data)
            payload["media"] = {
                "type": media_type,
                "filename": filename,
                "path": str(filepath),
                "size": len(data),
                **media_meta,
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "Failed to download media: %s", e,
            )

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
        messages.sort(key=lambda m: m.get("date", ""), reverse=True)
        return messages

    def _find_inbox_by_compound_id(self, account: str, compound_id: str) -> Path | None:
        """Find an existing inbox message dir by compound ID. Returns dir Path or None."""
        inbox_dir = self._account_dir(account) / "inbox"
        if not inbox_dir.is_dir():
            return None
        for msg_dir in inbox_dir.iterdir():
            msg_file = msg_dir / "message.json"
            if msg_dir.is_dir() and msg_file.is_file():
                try:
                    data = json.loads(msg_file.read_text(encoding="utf-8"))
                    if data.get("id") == compound_id:
                        return msg_dir
                except (json.JSONDecodeError, OSError):
                    continue
        return None

    def _build_conversation_preview(
        self,
        account_alias: str,
        chat_id: int,
        current_compound_id: str,
        max_messages: int = 10,
    ) -> str:
        """Build a markdown conversation preview of the last *max_messages* rounds.

        Scans inbox/ and sent/ dirs for messages matching *chat_id*, sorts by
        date ascending, takes the tail, and formats each line as:

            [relative_time] #compound_id sender_name: text

        If a message has reply_to_message_id the quoted message is shown
        indented beneath it (truncated to 50 chars).
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
                msg_chat_id = None
                msg_id = data.get("id", "")
                if msg_id:
                    parts = msg_id.split(":")
                    if len(parts) == 3:
                        try:
                            msg_chat_id = int(parts[1])
                        except ValueError:
                            pass
                if msg_chat_id != chat_id:
                    continue
                data["_folder"] = folder
                messages.append(data)

        # Sort by date ascending
        def _sort_key(m: dict) -> str:
            return m.get("date") or ""

        messages.sort(key=_sort_key)

        # Take last max_messages
        messages = messages[-max_messages:]

        # Build lookup by compound_id for reply quoting
        by_id: dict[str, dict] = {m.get("id", ""): m for m in messages}

        def _sender_name(m: dict) -> str:
            if m.get("_folder") == "sent":
                return "me"
            frm = m.get("from") or {}
            return frm.get("username") or frm.get("first_name") or "unknown"

        lines: list[str] = []
        for m in messages:
            cid = m.get("id", "")
            rel = _rel_time(m.get("date", ""))
            sender = _sender_name(m)
            text = m.get("text", "") or m.get("callback_query", "") or ""
            if m.get("media"):
                media_type = m["media"].get("type", "media")
                text = text or f"[{media_type}]"
            text_display = text.replace("\n", " ")

            line = f"[{rel}] #{cid} {sender}: {text_display}"
            lines.append(line)

            # Reply quoting
            reply_id_raw = m.get("reply_to_message_id")
            if reply_id_raw:
                # Reconstruct compound id for the reply target
                id_parts = cid.split(":")
                if len(id_parts) == 3:
                    reply_compound = f"{id_parts[0]}:{id_parts[1]}:{reply_id_raw}"
                    orig = by_id.get(reply_compound)
                    if orig:
                        orig_rel = _rel_time(orig.get("date", ""))
                        orig_text = (
                            orig.get("text", "") or orig.get("callback_query", "") or ""
                        )
                        orig_snippet = orig_text[:50]
                        if len(orig_text) > 50:
                            orig_snippet += "…"
                        lines.append(
                            f"  ↳ [{orig_rel}] #{reply_compound}: {orig_snippet}"
                        )

        header = _NOTIFICATION_HEADER_TEMPLATE.format(channel="Telegram").rstrip("\n")
        tail = f"**Conversation — last {len(messages)} messages (chat {chat_id})**"
        prefix = f"{header}\n\n{tail}"
        conversation = "\n".join(lines)
        body = f"{prefix}\n{conversation}" if conversation else prefix
        if len(body) > 10000:
            # Keep the guidance header and the newest end of the conversation.
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

    def _notification_message_ids(self, payload: dict) -> set[str] | None:
        """Return Telegram compound IDs referenced by an MCP notification mirror.

        New Telegram events publish ``message_ref`` in LICC preview metadata.
        Older notifications only have the bounded conversation preview body,
        whose lines include ``#account:chat:message`` anchors; parse those as a
        best-effort migration path so stale mirrors can be cleared after read.

        Returns ``None`` when any preview entry lacks an identifiable Telegram
        message ID. In that case clearing the coalesced notification could hide
        another unread event, so the mirror is left for explicit handling.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        previews = data.get("previews") if isinstance(data, dict) else None
        if not isinstance(previews, list) or not previews:
            return None

        ids: set[str] = set()
        for preview in previews:
            if not isinstance(preview, dict):
                return None

            subject = preview.get("subject")
            if isinstance(subject, str) and "callback_query" in subject:
                # Telegram callback queries reuse the message_id of the inline
                # keyboard message, so a compound ID is not a unique event ID.
                # Keep these mirrors for explicit handling rather than clearing
                # a fresh callback just because an older callback on that
                # message was read.
                return None

            ref = preview.get("message_ref")
            if isinstance(ref, str) and _looks_like_compound_id(ref):
                ids.add(ref)
                continue

            # Backward compatibility for notification files produced before
            # Telegram populated the generic LICC ``message_ref`` field.
            body_preview = preview.get("preview")
            matches = (
                [
                    match
                    for match in _COMPOUND_ID_RE.findall(body_preview)
                    if _looks_like_compound_id(match)
                ]
                if isinstance(body_preview, str)
                else []
            )
            if not matches:
                return None
            ids.update(matches)
        return ids

    def _all_notification_ids_read(self, compound_ids: set[str]) -> bool:
        read_by_account: dict[str, set[str]] = {}
        for compound_id in compound_ids:
            try:
                account, _chat_id, _msg_id = self._parse_compound_id(compound_id)
            except ValueError:
                return False
            if account not in read_by_account:
                read_by_account[account] = self._read_ids(account)
            if compound_id not in read_by_account[account]:
                return False
        return bool(compound_ids)

    def _clear_notification_if_handled(self) -> None:
        """Clear stale ``mcp.telegram`` notification mirrors once handled.

        LICC notification files are wake-up mirrors, not Telegram's source of
        truth. If every Telegram message referenced by the current mirror is
        now in the producer's durable ``read.json`` state, remove the mirror so
        a later wake/molt does not re-deliver the same handled message.
        """
        target = self._working_dir / ".notification" / f"{_NOTIFICATION_CHANNEL}.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            log.debug("cannot inspect Telegram notification mirror: %s", exc)
            return

        notification_ids = self._notification_message_ids(payload)
        if notification_ids is None:
            return
        if not self._all_notification_ids_read(notification_ids):
            return

        try:
            from lingtai_kernel.notifications import clear as clear_notification

            clear_notification(self._working_dir, _NOTIFICATION_CHANNEL)
            log.info(
                "telegram notification mirror cleared after read: ids=%s",
                sorted(notification_ids),
            )
        except Exception as exc:
            log.debug("failed to clear Telegram notification mirror: %s", exc)

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

    # ------------------------------------------------------------------
    # Rich Feedback Helpers (Issue #8)
    # ------------------------------------------------------------------

    def send_progress_message(
        self,
        account_alias: str,
        chat_id: int,
        text: str = "Working on it...",
        reply_to_message_id: int | None = None,
    ) -> dict | None:
        """Send a progress message that can be edited later.

        Returns the compound message_id for later editing, or None on failure.
        Best-effort — never blocks or fails the main task.
        """
        try:
            acct = self._service.get_account(account_alias)
            result = acct.send_message(
                chat_id, text,
                reply_to_message_id=reply_to_message_id,
            )
            tg_message_id = result.get("message_id", 0)
            compound_id = f"{account_alias}:{chat_id}:{tg_message_id}"
            return {"status": "sent", "message_id": compound_id}
        except Exception as e:
            log.debug("Failed to send progress message: %s", e)
            return None

    def update_progress_message(
        self,
        compound_id: str,
        text: str,
    ) -> bool:
        """Edit a progress message with updated text.

        Args:
            compound_id: Compound message ID from send_progress_message()
                (format: "{account}:{chat_id}:{message_id}").
            text: New text for the progress message.

        Returns True on success, False on failure.
        Best-effort — never blocks or fails the main task.
        """
        try:
            account, chat_id, tg_msg_id = self._parse_compound_id(compound_id)
            acct = self._service.get_account(account)
            acct.edit_message(chat_id, tg_msg_id, text)
            return True
        except Exception as e:
            log.debug("Failed to update progress message: %s", e)
            return False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    _PARSE_MODES = {"HTML", "MarkdownV2", "Markdown"}

    @staticmethod
    def _normalize_parse_mode(value: Any) -> Any:
        """Treat an empty parse_mode as omitted/plain text.

        Some tool callers serialize absent optional string fields as ``""``.
        Telegram Bot API itself omits parse_mode for plain text, so normalize
        the empty string before validation and payload persistence.
        """
        if value == "":
            return None
        return value

    @staticmethod
    def _normalize_chat_action(value: Any) -> Any:
        """Treat an empty chat_action as omitted/no typing indicator.

        Optional enum-like tool arguments may be serialized as ``""`` by some
        callers.  Telegram only needs chat_action when the caller explicitly
        asks for one, so normalize an empty string before action dispatch.
        """
        if value == "":
            return None
        return value

    def _rich_text_options(self, args: dict) -> tuple[dict[str, Any], str | None]:
        """Extract Bot API rich text options for text messages from tool args.

        Returns (options, error). When nothing relevant is supplied the
        options dict is empty, so existing plain-text callers behave exactly
        as before.
        """
        opts: dict[str, Any] = {}
        parse_mode = self._normalize_parse_mode(args.get("parse_mode"))
        if parse_mode is not None:
            if parse_mode not in self._PARSE_MODES:
                return {}, "parse_mode must be one of: HTML, MarkdownV2, Markdown"
            opts["parse_mode"] = parse_mode
        if args.get("entities") is not None:
            opts["entities"] = args.get("entities")
        if args.get("link_preview_options") is not None:
            opts["link_preview_options"] = args.get("link_preview_options")
        if args.get("disable_web_page_preview") is not None:
            opts["disable_web_page_preview"] = bool(args.get("disable_web_page_preview"))
        return opts, None

    def _caption_options(self, args: dict) -> tuple[dict[str, Any], str | None]:
        """Extract Bot API rich caption options for media sends from tool args.

        If ``caption_entities`` is omitted but ``entities`` is supplied, the
        latter is treated as caption entities for convenience.
        """
        opts: dict[str, Any] = {}
        parse_mode = self._normalize_parse_mode(args.get("parse_mode"))
        if parse_mode is not None:
            if parse_mode not in self._PARSE_MODES:
                return {}, "parse_mode must be one of: HTML, MarkdownV2, Markdown"
            opts["parse_mode"] = parse_mode
        caption_entities = args.get("caption_entities")
        if caption_entities is None:
            caption_entities = args.get("entities")
        if caption_entities is not None:
            opts["caption_entities"] = caption_entities
        return opts, None

    def _send(self, args: dict) -> dict:
        account = self._resolve_account(args)
        chat_id = args.get("chat_id")
        text = args.get("text", "")
        media = args.get("media")
        # Some tool-call frontends serialize optional object fields as an empty
        # attachment object for text-only sends, e.g.
        # {"type": "document", "path": ""}. Treat that shape as absent
        # media so text-only sends do not try to upload/open an empty path.
        if media and isinstance(media, dict) and not (media.get("path") or "").strip():
            media = None
        reply_markup = args.get("reply_markup")
        chat_action = self._normalize_chat_action(args.get("chat_action"))
        placeholder = bool(args.get("placeholder", False))
        rich_text_options, rich_text_error = self._rich_text_options(args)
        caption_options, caption_error = self._caption_options(args)
        if rich_text_error or caption_error:
            return {"error": rich_text_error or caption_error}

        if not chat_id:
            return {"error": "chat_id is required"}

        # Chat action shortcut: when chat_action is set and no text/media is
        # provided, send the typing indicator instead of a message. Skips
        # duplicate-protection and sent/ persistence — chat actions are
        # ephemeral (Telegram auto-expires them after 5 seconds).
        if chat_action and not text and not media:
            acct = self._service.get_account(account)
            acct.send_chat_action(chat_id, chat_action)
            return {"status": "ok", "chat_action": chat_action}

        if not text and not media:
            return {"error": "text or media is required"}

        # Duplicate send protection
        dup_key = (account, chat_id, text)
        count = self._last_sent.get(dup_key, 0)
        if count >= self._dup_free_passes:
            return {
                "status": "blocked",
                "warning": "Identical message already sent. Think twice before repeating.",
            }

        acct = self._service.get_account(account)
        reply_to = args.get("_reply_to_message_id")

        # Placeholder mode: fire a typing action before sending so the user
        # sees "is typing…" alongside the placeholder text. Best-effort —
        # never block or fail the send if the chat action call errors.
        if placeholder:
            try:
                acct._request("sendChatAction", json={
                    "chat_id": chat_id, "action": "typing",
                })
            except Exception as e:
                log.warning(
                    "sendChatAction (placeholder typing) failed for %s:%s: %s",
                    account, chat_id, e,
                )

        # Send via Bot API
        if media:
            media_type = media.get("type")
            media_path = media.get("path", "")
            media_file = Path(media_path)
            if not media_file.is_file() or media_file.stat().st_size == 0:
                return {
                    "error": (
                        "media.path does not point to a readable, non-empty "
                        f"file: {media_path}"
                    )
                }
            if media_type == "photo":
                result = acct.send_photo(
                    chat_id, media_path, caption=text or None,
                    reply_to_message_id=reply_to,
                    **caption_options,
                )
            elif media_type == "document":
                result = acct.send_document(
                    chat_id, media_path, caption=text or None,
                    reply_to_message_id=reply_to,
                    **caption_options,
                )
            else:
                return {"error": f"Unknown media type: {media_type}"}
        else:
            result = acct.send_message(
                chat_id, text, reply_markup=reply_markup,
                reply_to_message_id=reply_to,
                **rich_text_options,
            )

        # Track for duplicate detection
        self._last_sent[dup_key] = count + 1

        # Persist to sent/
        sent_id = str(uuid4())
        sent_dir = self._account_dir(account) / "sent" / sent_id
        sent_dir.mkdir(parents=True, exist_ok=True)
        tg_message_id = result.get("message_id", 0)
        compound_id = f"{account}:{chat_id}:{tg_message_id}"
        sent_record = {
            "id": compound_id,
            "to": {"chat_id": chat_id},
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": text,
            "media": media,
            "reply_markup": reply_markup,
            "parse_mode": self._normalize_parse_mode(args.get("parse_mode")),
            "entities": args.get("entities"),
            "caption_entities": args.get("caption_entities"),
            "link_preview_options": args.get("link_preview_options"),
            "disable_web_page_preview": args.get("disable_web_page_preview"),
            "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "placeholder" if placeholder else "sent",
        }
        (sent_dir / "message.json").write_text(
            json.dumps(sent_record, indent=2, default=str), encoding="utf-8",
        )

        response: dict[str, Any] = {
            "status": "sent",
            "message_id": compound_id,
        }
        if placeholder:
            response["placeholder"] = True
            response["hint"] = (
                "Placeholder sent — call telegram(action='edit', "
                f"message_id='{compound_id}', text=<final>) when ready."
            )

        # Issue #8: Add "done" reaction (✅) to the original message if reply_to
        if reply_to:
            try:
                acct.set_message_reaction(chat_id, reply_to, REACTION_DONE)
            except Exception as e:
                log.debug("Failed to add 'done' reaction: %s", e)

        # Issue #8: Stop typing indicator now that response is sent
        _typing_manager.stop_typing(acct, chat_id)

        return response

    def _check(self, args: dict) -> dict:
        account = self._resolve_account(args)
        inbox = self._list_messages(account, "inbox")
        sent = self._list_messages(account, "sent")
        messages = inbox + sent
        messages.sort(key=lambda m: m.get("date", ""), reverse=True)
        read_ids = self._read_ids(account)

        # Group by chat_id for conversation view
        conversations: dict[int, dict] = {}
        for msg in messages:
            # Extract chat_id from inbox-style or sent-style records
            chat = msg.get("chat")
            if isinstance(chat, dict):
                cid = chat.get("id", 0)
            else:
                to = msg.get("to")
                cid = to.get("chat_id", 0) if isinstance(to, dict) else 0

            if cid not in conversations:
                conversations[cid] = {
                    "chat_id": cid,
                    "chat_type": msg.get("chat", {}).get("type", "private") if isinstance(msg.get("chat"), dict) else "private",
                    "last_from": msg.get("from") or {"is_bot": True},
                    "last_text": (msg.get("text") or "")[:100],
                    "last_date": msg.get("date", ""),
                    "total": 0,
                    "unread": 0,
                }
            conversations[cid]["total"] += 1
            if msg.get("id") and msg["id"] not in read_ids:
                conversations[cid]["unread"] += 1

        return {
            "status": "ok",
            "total": len(messages),
            "messages": list(conversations.values()),
        }

    def _read(self, args: dict) -> dict:
        account = self._resolve_account(args)
        chat_id = args.get("chat_id")
        limit = args.get("limit", 10)

        if not chat_id:
            return {"error": "chat_id is required"}

        # Merge inbox and sent messages so post-molt agents can see their
        # own outgoing messages and avoid duplicate sends.
        inbox = self._list_messages(account, "inbox")
        sent = self._list_messages(account, "sent")
        combined = inbox + sent
        combined.sort(key=lambda m: m.get("date", ""), reverse=True)

        def _chat_id_of(m: dict) -> int | None:
            """Extract chat_id from inbox-style or sent-style records."""
            chat = m.get("chat")
            if isinstance(chat, dict):
                return chat.get("id")
            to = m.get("to")
            if isinstance(to, dict):
                return to.get("chat_id")
            return None

        filtered = [m for m in combined if _chat_id_of(m) == chat_id]
        recent = filtered[:limit]

        # Mark as read
        compound_ids = [m["id"] for m in recent if m.get("id")]
        if compound_ids:
            self._mark_read(account, compound_ids)
            self._clear_notification_if_handled()

        # Strip internal fields
        cleaned = []
        for m in recent:
            cleaned.append({
                "id": m.get("id"),
                "from": m.get("from"),
                "to": m.get("to"),
                "chat": m.get("chat"),
                "date": m.get("date"),
                "text": m.get("text"),
                "media": m.get("media"),
                "callback_query": m.get("callback_query"),
                "reply_to_message_id": m.get("reply_to_message_id"),
                "_direction": "outgoing" if m.get("to") else "incoming",
            })

        return {"status": "ok", "messages": cleaned}

    def _reply(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        text = args.get("text", "")
        if not compound_id:
            return {"error": "message_id is required"}
        if not text:
            return {"error": "text is required"}

        account, chat_id, tg_msg_id = self._parse_compound_id(compound_id)
        result = self._send({
            "account": account,
            "chat_id": chat_id,
            "text": text,
            "media": args.get("media"),
            "reply_markup": args.get("reply_markup"),
            "parse_mode": self._normalize_parse_mode(args.get("parse_mode")),
            "entities": args.get("entities"),
            "caption_entities": args.get("caption_entities"),
            "link_preview_options": args.get("link_preview_options"),
            "disable_web_page_preview": args.get("disable_web_page_preview"),
            # We need to pass reply_to_message_id through
            "_reply_to_message_id": tg_msg_id,
        })
        if result.get("status") == "sent":
            self._mark_read(account, [compound_id])
            self._clear_notification_if_handled()
        return result

    def _search(self, args: dict) -> dict:
        query = args.get("query", "")
        if not query:
            return {"error": "query is required"}
        account = self._resolve_account(args)
        target_chat = args.get("chat_id")

        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        messages = self._list_messages(account, "inbox")
        matches = []
        for msg in messages:
            if target_chat and msg.get("chat", {}).get("id") != target_chat:
                continue
            searchable = " ".join([
                str(msg.get("from", {}).get("username", "")),
                str(msg.get("from", {}).get("first_name", "")),
                msg.get("text", ""),
            ])
            if pattern.search(searchable):
                matches.append({
                    "id": msg.get("id"),
                    "from": msg.get("from"),
                    "date": msg.get("date"),
                    "text": msg.get("text"),
                })

        return {"status": "ok", "total": len(matches), "messages": matches}

    def _delete(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        if not compound_id:
            return {"error": "message_id is required"}
        account, chat_id, tg_msg_id = self._parse_compound_id(compound_id)
        acct = self._service.get_account(account)
        acct.delete_message(chat_id=chat_id, message_id=tg_msg_id)
        return {"status": "deleted", "message_id": compound_id}

    def _edit(self, args: dict) -> dict:
        compound_id = args.get("message_id", "")
        text = args.get("text", "")
        if not compound_id:
            return {"error": "message_id is required"}
        if not text:
            return {"error": "text is required"}
        account, chat_id, tg_msg_id = self._parse_compound_id(compound_id)
        reply_markup = args.get("reply_markup")
        rich_text_options, rich_text_error = self._rich_text_options(args)
        caption_options, caption_error = self._caption_options(args)
        if rich_text_error or caption_error:
            return {"error": rich_text_error or caption_error}
        acct = self._service.get_account(account)

        # Detect if original message had media (caption edit vs text edit)
        is_caption = False
        sent_dir = self._account_dir(account) / "sent"
        if sent_dir.is_dir():
            for msg_dir in sent_dir.iterdir():
                msg_file = msg_dir / "message.json"
                if msg_dir.is_dir() and msg_file.is_file():
                    try:
                        data = json.loads(msg_file.read_text(encoding="utf-8"))
                        if data.get("id") == compound_id and data.get("media"):
                            is_caption = True
                            break
                    except (json.JSONDecodeError, OSError):
                        continue

        edit_options = caption_options if is_caption else rich_text_options
        acct.edit_message(
            chat_id=chat_id, message_id=tg_msg_id, text=text,
            reply_markup=reply_markup, is_caption=is_caption,
            **edit_options,
        )
        return {"status": "edited", "message_id": compound_id}

    def _contacts(self, args: dict) -> dict:
        account = self._resolve_account(args)
        return {"status": "ok", "contacts": self._load_contacts(account)}

    def _add_contact(self, args: dict) -> dict:
        account = self._resolve_account(args)
        chat_id = args.get("chat_id")
        alias = args.get("alias", "")
        if not chat_id:
            return {"error": "chat_id is required"}
        if not alias:
            return {"error": "alias is required"}
        contacts = self._load_contacts(account)
        contacts[alias] = {
            "chat_id": chat_id,
            "username": args.get("username", ""),
            "first_name": args.get("first_name", ""),
        }
        self._save_contacts(account, contacts)
        return {"status": "added", "alias": alias}

    def _remove_contact(self, args: dict) -> dict:
        account = self._resolve_account(args)
        alias = args.get("alias", "")
        chat_id = args.get("chat_id")
        contacts = self._load_contacts(account)
        if alias and alias in contacts:
            del contacts[alias]
            self._save_contacts(account, contacts)
            return {"status": "removed", "alias": alias}
        elif chat_id:
            to_remove = [k for k, v in contacts.items() if v.get("chat_id") == chat_id]
            for k in to_remove:
                del contacts[k]
            if to_remove:
                self._save_contacts(account, contacts)
                return {"status": "removed", "aliases": to_remove}
        return {"error": "Contact not found"}

    def _accounts(self) -> dict:
        return {
            "status": "ok",
            "accounts": self._service.list_accounts(),
            "details": self._service.account_details(),
            "identity_path": str(self._service.identity_path()),
        }

    # ------------------------------------------------------------------
    # Manual — progressive-disclosure usage guidance
    # ------------------------------------------------------------------
    #
    # The manual lives in this package's bundled SKILL.md (standard skill
    # format: YAML frontmatter + markdown body), loaded at import time above.
    # action='manual' returns the full skill markdown plus parsed metadata and
    # the resolved path; the frontmatter is also injected into the schema's
    # 'manual' action description as a catalog entry.

    def _manual(self) -> dict:
        return {
            "status": "ok",
            "action": "manual",
            "skill": _SKILL_FRONTMATTER.get("name", "telegram-mcp-manual"),
            "metadata": dict(_SKILL_FRONTMATTER),
            "path": _SKILL_PATH,
            "manual": _SKILL_BODY,
        }
