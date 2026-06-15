"""Regression tests for the embedded IMAP MCP reply attachment path."""
from __future__ import annotations

from pathlib import Path

from lingtai.mcp_servers.imap.manager import IMAPMailManager


class FakeAccount:
    address = "me@example.com"

    def __init__(self) -> None:
        self.sent_kwargs: dict | None = None
        self.stored_flags: list[tuple[str, str, list[str]]] = []

    def fetch_full(self, folder: str, uid: str) -> dict:
        return {
            "from": "Sender <sender@example.com>",
            "from_address": "sender@example.com",
            "subject": "Question",
            "message_id": "<orig@example.com>",
            "references": "<parent@example.com>",
        }

    def send_email(self, **kwargs):
        self.sent_kwargs = kwargs
        return None

    def store_flags(self, folder: str, uid: str, flags: list[str]) -> bool:
        self.stored_flags.append((folder, uid, flags))
        return True


class FakeService:
    def __init__(self, account: FakeAccount) -> None:
        self.default_account = account
        self._account = account

    def get_account(self, address: str | None):
        return self._account


def _manager(account: FakeAccount) -> IMAPMailManager:
    return IMAPMailManager(
        FakeService(account),
        working_dir=Path("/tmp/imap-agent-workdir"),
        tcp_alias="/tmp/imap-bridge",
        on_inbound=lambda payload: None,
    )


def test_reply_forwards_resolved_attachments_to_send_email():
    account = FakeAccount()

    result = _manager(account).handle({
        "action": "reply",
        "email_id": "me@example.com:INBOX:42",
        "message": "Here is the file.",
        "attachments": ["relative.txt", "/abs/file.pdf"],
    })

    assert result["status"] == "delivered"
    assert account.sent_kwargs is not None
    assert account.sent_kwargs["to"] == ["sender@example.com"]
    assert account.sent_kwargs["subject"] == "Re: Question"
    assert account.sent_kwargs["body"] == "Here is the file."
    assert account.sent_kwargs["attachments"] == [
        "/tmp/imap-agent-workdir/relative.txt",
        "/abs/file.pdf",
    ]
    assert account.sent_kwargs["in_reply_to"] == "<orig@example.com>"
    assert account.sent_kwargs["references"] == "<parent@example.com> <orig@example.com>"
    assert account.stored_flags == [("INBOX", "42", ["\\Answered"])]


def test_reply_keeps_attachments_none_when_absent():
    account = FakeAccount()

    result = _manager(account).handle({
        "action": "reply",
        "email_id": "me@example.com:INBOX:42",
        "message": "No file this time.",
    })

    assert result["status"] == "delivered"
    assert account.sent_kwargs is not None
    assert account.sent_kwargs["attachments"] is None
