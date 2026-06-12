"""Minimal HTTP webhook receiver for Meta WhatsApp Cloud API callbacks."""
from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .manager import WhatsAppManager
from .webhook import verify_get_challenge, verify_signature

log = logging.getLogger(__name__)

_MAX_BODY_BYTES = 1_000_000


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def _metadata_phone_number_id(payload: dict[str, Any]) -> str | None:
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) or {}
            phone_number_id = metadata.get("phone_number_id")
            if phone_number_id:
                return str(phone_number_id)
    return None


class WhatsAppWebhookServer:
    """Background stdlib HTTP server for WhatsApp webhook callbacks.

    The MCP itself still speaks stdio. Meta requires an HTTPS public callback URL
    for inbound messages; operators usually expose this local HTTP server via a
    reverse proxy/tunnel (ngrok, Cloudflare Tunnel, Caddy, nginx, etc.).
    """

    def __init__(self, manager: WhatsAppManager, *, host: str, port: int, path: str) -> None:
        self.manager = manager
        self.host = host or "127.0.0.1"
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_manager_config(cls, manager: WhatsAppManager) -> "WhatsAppWebhookServer | None":
        for account in manager.accounts.values():
            webhook = account.get("webhook") or {}
            if webhook.get("port"):
                return cls(
                    manager,
                    host=str(webhook.get("host") or "127.0.0.1"),
                    port=int(webhook["port"]),
                    path=str(webhook.get("path") or "/webhooks/whatsapp"),
                )
        return None

    def start(self) -> None:
        if self._httpd is not None:
            return

        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "LingTaiWhatsAppWebhook/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover - logging glue
                log.info("WhatsApp webhook %s - " + fmt, self.address_string(), *args)

            def _path_ok(self) -> bool:
                return urlparse(self.path).path == outer.path

            def _write(self, status: HTTPStatus, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
                data = body.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
                if not self._path_ok():
                    self._write(HTTPStatus.NOT_FOUND, "not found")
                    return
                query = {k: _first(v) or "" for k, v in parse_qs(urlparse(self.path).query).items()}
                for account in outer.manager.accounts.values():
                    ok, challenge = verify_get_challenge(query, str(account.get("verify_token") or ""))
                    if ok and challenge is not None:
                        self._write(HTTPStatus.OK, challenge)
                        return
                self._write(HTTPStatus.FORBIDDEN, "verification failed")

            def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
                if not self._path_ok():
                    self._write(HTTPStatus.NOT_FOUND, "not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self._write(HTTPStatus.BAD_REQUEST, "invalid content-length")
                    return
                if length > _MAX_BODY_BYTES:
                    self._write(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "webhook body too large")
                    return
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    self._write(HTTPStatus.BAD_REQUEST, "invalid json")
                    return

                phone_number_id = _metadata_phone_number_id(payload)
                alias = outer.manager.match_account_alias_for_phone_number_id(phone_number_id)
                if alias is None:
                    self._write(HTTPStatus.BAD_REQUEST, "unknown or missing phone_number_id")
                    return
                account = outer.manager.accounts[alias]
                app_secret = str(account.get("app_secret") or "")
                if not app_secret:
                    self._write(HTTPStatus.FORBIDDEN, "missing app_secret; cannot verify webhook signature")
                    return
                if not verify_signature(app_secret, body, self.headers.get("X-Hub-Signature-256")):
                    self._write(HTTPStatus.FORBIDDEN, "invalid signature")
                    return

                events = outer.manager.ingest_webhook(alias, payload)
                self._write(HTTPStatus.OK, json.dumps({"status": "ok", "events": len(events)}), "application/json")

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.timeout = 30
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="lingtai-whatsapp-webhook", daemon=True)
        self._thread.start()
        log.info("WhatsApp webhook receiver listening on http://%s:%s%s", self.host, self.port, self.path)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._httpd = None
        self._thread = None
