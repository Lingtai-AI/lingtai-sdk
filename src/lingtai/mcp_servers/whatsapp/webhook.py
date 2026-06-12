"""Webhook verification helpers for Meta WhatsApp Cloud API."""
from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Mapping, Any


def verify_get_challenge(query: Mapping[str, str], verify_token: str) -> tuple[bool, str | None]:
    """Validate Meta webhook GET verification query.

    Meta sends `hub.mode=subscribe`, `hub.verify_token=<token>`, and
    `hub.challenge=<opaque>`. Return `(True, challenge)` if valid.
    """
    mode = query.get("hub.mode") or query.get("mode")
    token = query.get("hub.verify_token") or query.get("verify_token")
    challenge = query.get("hub.challenge") or query.get("challenge")
    if mode == "subscribe" and hmac.compare_digest(str(token or ""), str(verify_token or "")):
        return True, challenge
    return False, None


def compute_signature(app_secret: str, body: bytes) -> str:
    digest = hmac.new(app_secret.encode("utf-8"), body, sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(app_secret: str, body: bytes, signature_header: str | None) -> bool:
    if not app_secret or not signature_header:
        return False
    expected = compute_signature(app_secret, body)
    return hmac.compare_digest(expected, signature_header)


def extract_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten WhatsApp webhook payload into message/status event dicts.

    Keeps only deterministic fields for local storage/LICC conversion. Raw
    WhatsApp objects may contain extra PII, so they are intentionally not
    retained by default.
    """
    events: list[dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) or {}
            for msg in value.get("messages", []) or []:
                wa_id = msg.get("from")
                events.append({
                    "kind": "message",
                    "wa_id": wa_id,
                    "message_id": msg.get("id"),
                    "timestamp": msg.get("timestamp"),
                    "type": msg.get("type"),
                    "text": (msg.get("text") or {}).get("body"),
                    "metadata": metadata,
                })
            for status in value.get("statuses", []) or []:
                events.append({
                    "kind": "status",
                    "wa_id": status.get("recipient_id"),
                    "message_id": status.get("id"),
                    "status": status.get("status"),
                    "timestamp": status.get("timestamp"),
                    "metadata": metadata,
                })
    return events
