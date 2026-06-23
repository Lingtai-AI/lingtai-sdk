"""Live synchronous Codex Responses-over-WebSocket transport (EXPERIMENTAL).

This is the real wire driver for the experimental websocket path mirrored from
the official Codex CLI (repo openai/codex, tag ``rust-v0.130.0``). It is GATED
behind ``LINGTAI_CODEX_WS`` and only reached on a live run — the unit tests in
``tests/test_codex_ws_session.py`` inject a fake transport and never import this
module. It is deliberately kept out of the hot ``adapter`` import path and only
loaded lazily by ``_default_codex_ws_transport_factory``.

Design notes / source citations (all in the official source clone):

  * URL: ``wss://chatgpt.com/backend-api/codex/responses`` — the HTTP base with
    ``https`` swapped to ``wss`` (``provider.rs:92-103``).
  * Handshake header ``OpenAI-Beta: responses_websockets=2026-02-06``
    (``client.rs:142``) and the captured per-turn ``x-codex-turn-state``
    (``client.rs:134`` / ``responses_websocket.rs:438-445``).
  * Frames: JSON text frames; ``{"type":"response.create", ...}`` and
    ``{"type":"response.processed","response_id":...}`` (``common.rs:269-277``).
  * Stop on ``response.completed`` (``responses_websocket.rs:574-684``).
  * A handshake ``426 UPGRADE_REQUIRED`` (or any connect failure) maps to a
    fallback to HTTP (``client.rs:1361-1364``) — surfaced here as
    ``_CodexWsFallback``.

The events yielded by :meth:`stream` are lightweight objects shaped like the
OpenAI SDK Responses stream events (``.type``, ``.delta``, ``.item``,
``.response``) so the adapter's existing event loop consumes them unchanged.

NOTE: This live driver has NOT been exercised against the real Codex backend in
this pass (no authenticated calls were made). The parent should validate it live
after an auth refresh — see the patch report's live-validation plan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator

from lingtai_kernel.logging import get_logger

logger = get_logger()


def _to_event(payload: dict[str, Any]) -> Any:
    """Map a parsed websocket JSON frame into an SDK-shaped stream event.

    Recursively wraps nested dicts as SimpleNamespace so attribute access
    (``event.response.id``, ``event.item.type`` …) works like the SDK objects.
    """

    def wrap(value: Any) -> Any:
        if isinstance(value, dict):
            return SimpleNamespace(**{k: wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [wrap(v) for v in value]
        return value

    return wrap(payload)


class SyncCodexWebsocketTransport:
    """One Codex websocket connection, driven synchronously.

    Lifecycle matches the adapter's expectations:
      * :meth:`connect` performs the handshake and returns the captured
        ``x-codex-turn-state`` (or ``None``); raises ``_CodexWsFallback`` on a
        426 / connection / auth failure.
      * :meth:`stream` sends one ``response.create`` frame and yields events
        until ``response.completed``.
      * :meth:`send_response_processed` sends the post-completion ack frame.
    """

    def __init__(self, *, url: str, headers: dict[str, str], open_timeout: float = 30.0):
        self._url = url
        self._headers = dict(headers)
        self._open_timeout = open_timeout
        self._conn = None

    def connect(self, *, headers: dict[str, str] | None = None) -> str | None:
        # Import here so the dependency is only required on the live path; the
        # adapter factory already guards import availability, but re-raise as a
        # fallback signal to be safe.
        from lingtai.llm.openai.adapter import _CODEX_TURN_STATE_HEADER, _CodexWsFallback

        try:
            from websockets.sync.client import connect as ws_connect
            from websockets.exceptions import InvalidStatus
        except Exception as exc:  # pragma: no cover - guarded upstream too
            raise _CodexWsFallback(f"websockets unavailable: {exc}") from exc

        hdrs = dict(self._headers)
        if headers:
            hdrs.update(headers)
        try:
            self._conn = ws_connect(
                self._url,
                additional_headers=hdrs,
                open_timeout=self._open_timeout,
            )
        except Exception as exc:  # pragma: no cover - live only
            # A 426 UPGRADE_REQUIRED (or 401, or any handshake failure) means
            # "do not use websockets" — fall back to HTTP. ``InvalidStatus``
            # carries the response; surface its status for diagnostics.
            status = None
            try:
                status = getattr(getattr(exc, "response", None), "status_code", None)
            except Exception:
                status = None
            # Do not interpolate ``exc`` into the fallback message: some
            # websocket implementations attach the request/headers to handshake
            # exceptions, and those headers may include the bearer token.
            raise _CodexWsFallback(
                f"websocket handshake failed (status={status}): {type(exc).__name__}"
            ) from exc

        # Best-effort capture of x-codex-turn-state from the handshake response
        # headers. The attribute path varies across websockets versions, so try
        # the known locations and degrade gracefully (no turn-state replay) if
        # none are present.
        return self._capture_turn_state(_CODEX_TURN_STATE_HEADER)

    def _capture_turn_state(self, header_name: str) -> str | None:  # pragma: no cover - live only
        conn = self._conn
        candidates = []
        resp = getattr(conn, "response", None)
        if resp is not None:
            candidates.append(resp)
        proto = getattr(conn, "protocol", None)
        if proto is not None:
            candidates.append(getattr(proto, "handshake_response", None))
            candidates.append(getattr(proto, "response", None))
        for resp in candidates:
            headers = getattr(resp, "headers", None)
            if headers is None:
                continue
            try:
                value = headers.get(header_name)
            except Exception:
                value = None
            if value:
                return value
        return None

    def stream(self, frame: dict[str, Any]) -> Iterator[Any]:  # pragma: no cover - live only
        from lingtai.llm.openai.adapter import _CodexWsFallback

        conn = self._conn
        if conn is None:
            raise _CodexWsFallback("websocket connection is closed")
        conn.send(json.dumps(frame))
        for message in conn:
            if isinstance(message, (bytes, bytearray)):
                message = message.decode("utf-8", errors="replace")
            try:
                payload = json.loads(message)
            except Exception as exc:
                raise _CodexWsFallback(f"bad websocket frame: {exc}") from exc
            if isinstance(payload, dict):
                event_type = str(payload.get("type") or "")
                if event_type == "error" or event_type.endswith(".error"):
                    error = payload.get("error")
                    error_type = error.get("type") if isinstance(error, dict) else None
                    status = payload.get("status")
                    parts = [event_type or "unknown"]
                    if error_type:
                        parts.append(str(error_type))
                    if status is not None:
                        parts.append(f"status={status}")
                    # Do not include the whole payload: live error frames may echo
                    # request details, and requests may contain prompts or headers.
                    raise _CodexWsFallback(
                        f"websocket error frame: {'; '.join(parts)}"
                    )
            event = _to_event(payload)
            yield event
            if isinstance(payload, dict) and payload.get("type") == "response.completed":
                break

    def send_response_processed(self, response_id: str) -> None:  # pragma: no cover - live only
        conn = self._conn
        if conn is None:
            return
        try:
            conn.send(json.dumps({"type": "response.processed", "response_id": response_id}))
        except Exception as exc:
            logger.debug("codex ws response.processed send failed: %s", exc)

    def close(self) -> None:  # pragma: no cover - live only
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
