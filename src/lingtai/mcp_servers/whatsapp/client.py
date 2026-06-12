"""Tiny dependency-free Meta WhatsApp Cloud API client."""
from __future__ import annotations

import json
from typing import Any
from urllib import request, error


class WhatsAppClient:
    def __init__(self, *, access_token: str, phone_number_id: str, api_version: str = "v23.0") -> None:
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.api_version = api_version or "v23.0"

    @property
    def messages_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

    def post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.access_token:
            raise ValueError("missing access_token")
        if not self.phone_number_id:
            raise ValueError("missing phone_number_id")
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.messages_url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {"status": "ok"}
        except error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Meta API HTTP {e.code}: {body}") from e

    def mark_message_read(self, message_id: str) -> dict[str, Any]:
        return self.post_message({
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        })
