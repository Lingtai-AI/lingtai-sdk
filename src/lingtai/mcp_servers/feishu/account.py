"""FeishuAccount — single app credential, WebSocket listener + REST sender.

One daemon thread per account runs the lark-oapi WebSocket client.
Constructor stores config only — no connections, no threads.
start() spawns the WebSocket thread and initialises the REST client.
stop() signals the thread to stop and joins it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# lark_oapi is lazy-imported so the module stays importable without
# the optional dependency installed.
lark: Any = None

# The lark_oapi.ws.client module, captured lazily. Stored as a module global so
# tests can inject a fake SDK module (see tests/test_ws_event_loop.py). The real
# SDK keeps its own module-level ``loop`` attribute that ``Client.start()`` uses
# directly — see ``_ThreadLocalLoop`` and ``_ws_loop`` for why that matters.
_sdk_ws_client_module: Any = None


def _import_lark() -> Any:
    global lark
    if lark is None:
        import lark_oapi as _lark
        lark = _lark
    return lark


def _get_sdk_ws_client_module() -> Any:
    """Return the ``lark_oapi.ws.client`` module (or an injected fake).

    Tests set ``_sdk_ws_client_module`` directly to a stand-in that exposes the
    same ``loop`` attribute contract as the real SDK module.
    """
    global _sdk_ws_client_module
    if _sdk_ws_client_module is None:
        import lark_oapi.ws.client as _ws_client
        _sdk_ws_client_module = _ws_client
    return _sdk_ws_client_module


class _ThreadLocalLoop:
    """Per-thread proxy that stands in for ``lark_oapi.ws.client.loop``.

    The SDK captures ``loop = asyncio.get_event_loop()`` at *import time* into a
    module global, and ``Client.start()`` calls ``loop.run_until_complete(...)``
    on that global. When the SDK is imported on the main MCP thread (while
    ``asyncio.run(serve())`` is active), the global captures the already-running
    main loop, so ``run_until_complete`` raises
    ``RuntimeError: This event loop is already running`` and inbound messages
    never arrive (issue #113).

    Setting a thread-current loop does not help: ``start()`` ignores it and uses
    the module global. So we replace the module global with this proxy, which
    forwards every attribute access to the *calling thread's* bound loop. Each WS
    thread binds its own fresh loop, so concurrent accounts never share or
    clobber a loop, even though the SDK exposes a single module-global name.

    The original loop is preserved as a fallback so any code path that touches
    the global from an unbound thread keeps working unchanged.
    """

    def __init__(self, fallback: Any) -> None:
        self._local = threading.local()
        self._fallback = fallback

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._local.loop = loop

    def unbind(self) -> None:
        self._local.loop = None

    def _resolve(self) -> Any:
        loop = getattr(self._local, "loop", None)
        return loop if loop is not None else self._fallback

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally, so our own
        # attributes (_local, _fallback, bind, ...) are unaffected.
        return getattr(self._resolve(), name)


_install_lock = threading.Lock()


def _install_thread_local_sdk_loop(sdk: Any) -> _ThreadLocalLoop:
    """Ensure ``sdk.loop`` is a ``_ThreadLocalLoop`` and return it.

    Idempotent and thread-safe: the first caller swaps the module-global loop
    for a proxy (preserving the original as the fallback); subsequent callers
    reuse the same proxy. Returns the proxy so the caller can ``bind``/``unbind``
    its own thread loop.
    """
    with _install_lock:
        current = getattr(sdk, "loop", None)
        if isinstance(current, _ThreadLocalLoop):
            return current
        proxy = _ThreadLocalLoop(fallback=current)
        sdk.loop = proxy
        return proxy


class FeishuAccount:
    """Manages a single Feishu (Lark) app credential — WS polling + REST sending."""

    def __init__(
        self,
        alias: str,
        app_id: str,
        app_secret: str,
        allowed_users: list[str] | None,
        on_message: Callable[[str, Any], None] | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.alias = alias
        self._app_id = app_id
        self._app_secret = app_secret
        self._allowed_users: set[str] | None = (
            set(allowed_users) if allowed_users else None
        )
        self._on_message = on_message
        self._state_dir = state_dir

        self._ws_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._rest_client: Any = None
        self._stop_event = threading.Event()
        self._bot_info: dict | None = None
        self._last_verified_at: str | None = None

        self._load_state()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Build REST client, register WS event handler, start polling thread."""
        if self._ws_thread is not None:
            return

        _lark = _import_lark()

        # REST client (for sending)
        self._rest_client = (
            _lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )

        # Store minimal bot info — full bot info API path varies by SDK version
        self._bot_info = {"app_id": self._app_id}
        self._last_verified_at = datetime.now(timezone.utc).isoformat()
        self._save_state()

        # Event handler
        def _handle_message(data: Any) -> None:
            try:
                self._process_event(data)
            except Exception as exc:
                logger.warning(
                    "Feishu event processing error (%s): %s", self.alias, exc
                )

        event_handler = (
            _lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_handle_message)
            .build()
        )

        # WebSocket client — start() blocks, run in daemon thread
        self._stop_event.clear()
        self._ws_client = _lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=event_handler,
            log_level=_lark.LogLevel.INFO,
        )

        self._ws_thread = threading.Thread(
            target=self._ws_loop,
            daemon=True,
            name=f"feishu-ws-{self.alias}",
        )
        self._ws_thread.start()
        logger.info(
            "Feishu account '%s' started (app_id=%s)",
            self.alias,
            self._app_id,
        )

    def _ws_loop(self) -> None:
        """Run the blocking WebSocket client in a background thread.

        lark-oapi captures ``loop = asyncio.get_event_loop()`` into a *module
        global* (``lark_oapi.ws.client.loop``) at import time, and
        ``Client.start()`` calls ``loop.run_until_complete(...)`` on that global
        — it never re-reads the thread-current loop. Imported on the main MCP
        thread under ``asyncio.run(serve())``, that global is the already-running
        main loop, so ``run_until_complete`` raises
        ``RuntimeError: This event loop is already running`` and inbound messages
        are never delivered (issue #113).

        Fix: give this thread a fresh loop and make the SDK's module-global
        ``loop`` resolve to it for the duration of ``start()`` via a per-thread
        proxy (``_ThreadLocalLoop``). The proxy is installed once and shared, so
        multiple accounts each running their own WS thread get an independent
        loop without clobbering a single global. The thread binding is removed in
        ``finally`` and the fresh loop is closed.
        """
        sdk = _get_sdk_ws_client_module()
        proxy = _install_thread_local_sdk_loop(sdk)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        proxy.bind(loop)
        try:
            self._ws_client.start()
        except Exception as e:
            if not self._stop_event.is_set():
                logger.warning(
                    "Feishu WS client exited unexpectedly (%s): %s",
                    self.alias, e,
                )
        finally:
            proxy.unbind()
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)

    def stop(self) -> None:
        """Signal the WebSocket thread to stop."""
        self._stop_event.set()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception:
                pass
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5.0)
            self._ws_thread = None

    # ------------------------------------------------------------------
    # Event processing
    # ------------------------------------------------------------------

    def _process_event(self, data: Any) -> None:
        """Filter by allowed_users and dispatch to on_message callback."""
        event = getattr(data, "event", None)
        if event is None:
            return

        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None) if sender else None
        open_id: str = getattr(sender_id, "open_id", "") if sender_id else ""

        if self._allowed_users is not None and open_id not in self._allowed_users:
            return

        if self._on_message:
            self._on_message(self.alias, data)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
    ) -> dict:
        """Send a plain-text message. Returns created Message fields as dict."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = self._rest_client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu send_text failed: code={response.code} msg={response.msg}"
            )
        data = response.data
        return {
            "message_id": getattr(data, "message_id", ""),
            "chat_id": getattr(data, "chat_id", ""),
            "create_time": getattr(data, "create_time", ""),
        }

    def reply_text(self, message_id: str, text: str) -> dict:
        """Reply to a specific message by Feishu message_id."""
        from lark_oapi.api.im.v1 import (
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = self._rest_client.im.v1.message.reply(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu reply_text failed: code={response.code} msg={response.msg}"
            )
        data = response.data
        return {
            "message_id": getattr(data, "message_id", ""),
            "chat_id": getattr(data, "chat_id", ""),
        }

    # ------------------------------------------------------------------
    # File download (voice, audio, images, documents)
    # ------------------------------------------------------------------

    def get_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str = "file",
    ) -> tuple[str, bytes]:
        """Download a resource file from a message.

        Args:
            message_id: Feishu message ID (om_xxx).
            file_key: The file_key from the message content.
            resource_type: "file", "image", or "video".

        Returns:
            (filename, content_bytes) tuple.
        """
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        response = self._rest_client.im.v1.message_resource.get(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu get_message_resource failed: "
                f"code={response.code} msg={response.msg}"
            )
        filename = response.file_name or f"{file_key}.ogg"
        content = response.file.read()
        return filename, content

    # ------------------------------------------------------------------
    # Reactions (emoji responses on messages)
    # ------------------------------------------------------------------

    def add_reaction(self, message_id: str, emoji_type: str) -> bool:
        """Add an emoji reaction to a message.

        Args:
            message_id: Feishu message ID (om_xxx).
            emoji_type: Emoji type string (e.g. "OK", "THUMBSUP", "SMILE").

        Returns:
            True on success.
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(
                    Emoji.builder().emoji_type(emoji_type).build()
                )
                .build()
            )
            .build()
        )
        response = self._rest_client.im.v1.message_reaction.create(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu add_reaction failed: "
                f"code={response.code} msg={response.msg}"
            )
        return True

    # ------------------------------------------------------------------
    # Message editing & deletion
    # ------------------------------------------------------------------

    def update_message(self, message_id: str, text: str) -> dict:
        """Edit a sent text message with new content.

        Uses the PATCH endpoint to update message content.
        Only text messages can be edited this way.

        Args:
            message_id: Feishu message ID (om_xxx).
            text: New text content.

        Returns:
            Response dict (empty on success since PATCH returns no body).
        """
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        response = self._rest_client.im.v1.message.patch(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu update_message failed: "
                f"code={response.code} msg={response.msg}"
            )
        return {}

    def delete_message(self, message_id: str) -> bool:
        """Delete a message sent by the bot.

        Args:
            message_id: Feishu message ID (om_xxx).

        Returns:
            True on success.
        """
        from lark_oapi.api.im.v1 import DeleteMessageRequest

        request = (
            DeleteMessageRequest.builder()
            .message_id(message_id)
            .build()
        )
        response = self._rest_client.im.v1.message.delete(request)
        if not response.success():
            raise RuntimeError(
                f"Feishu delete_message failed: "
                f"code={response.code} msg={response.msg}"
            )
        return True

    @property
    def allowed_users_count(self) -> int | None:
        """Return the allow-list size without exposing user IDs."""
        if self._allowed_users is None:
            return None
        return len(self._allowed_users)

    def public_identity(self) -> dict[str, Any]:
        """Non-secret Feishu app identity observed from config/state.

        This intentionally exposes only stable public app metadata. It never
        includes app secrets, individual open_ids/user_ids, chat IDs, messages,
        or webhook/encryption secrets.
        """
        info = self._bot_info or {}
        identity = {
            "alias": self.alias,
            "app_id": info.get("app_id") or self._app_id,
            "last_verified_at": self._last_verified_at,
        }
        return {k: v for k, v in identity.items() if v is not None}

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _state_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / "state.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._bot_info = data.get("bot_info")
            self._last_verified_at = data.get("last_verified_at")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load Feishu state: %s", e)

    def _save_state(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "bot_info": self._bot_info,
            "last_verified_at": self._last_verified_at,
        }
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
