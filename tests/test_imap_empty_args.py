"""Regression tests for IMAP empty-argument ergonomics and clearer errors.

Covers the fix for empty/whitespace-only optional ``account`` and ``folder``
being treated like omitted (so the default account / ``INBOX`` are used), plus
more visible error dicts for unknown accounts and missing ``flags``.
"""
from __future__ import annotations

from pathlib import Path

from lingtai.mcp_servers.imap.manager import IMAPMailManager


class FakeAccount:
    address = "me@example.com"

    def __init__(self) -> None:
        self.checked_folders: list[str] = []
        self.searched: list[tuple[str, str]] = []
        self.moved: list[tuple[str, str, str]] = []
        self.flag_calls: list[tuple[str, str, list[str], str]] = []

    def fetch_envelopes(self, folder: str, n: int) -> list[dict]:
        self.checked_folders.append(folder)
        return []

    def search(self, folder: str, query: str) -> list[str]:
        self.searched.append((folder, query))
        return []

    def fetch_headers_by_uids(self, folder: str, uids: list[str]) -> list[dict]:
        return []

    def move_message(self, folder: str, uid: str, dest: str) -> bool:
        self.moved.append((folder, uid, dest))
        return True

    def store_flags(self, folder: str, uid: str, flags: list[str],
                    action: str = "+FLAGS") -> bool:
        self.flag_calls.append((folder, uid, flags, action))
        return True


class FakeService:
    """Fake service that, unlike the reply-attachments fake, honors the
    requested account address so the unknown-account path is exercisable."""

    def __init__(self, account: FakeAccount) -> None:
        self.default_account = account
        self._map = {account.address: account}

    def get_account(self, address):
        if address is None:
            return self.default_account
        return self._map.get(address)


def _manager(account: FakeAccount) -> IMAPMailManager:
    return IMAPMailManager(
        FakeService(account),
        working_dir=Path("/tmp/imap-agent-workdir"),
        tcp_alias="/tmp/imap-bridge",
        on_inbound=lambda payload: None,
    )


# --- empty/whitespace account treated like omitted -------------------------

def test_empty_account_uses_default_account():
    account = FakeAccount()
    result = _manager(account).handle({"action": "check", "account": ""})
    assert result["status"] == "ok"
    assert result["account"] == "me@example.com"


def test_whitespace_account_uses_default_account():
    account = FakeAccount()
    result = _manager(account).handle({"action": "check", "account": "   "})
    assert result["status"] == "ok"
    assert result["account"] == "me@example.com"


# --- empty/whitespace folder treated like omitted (INBOX) ------------------

def test_empty_folder_for_check_uses_inbox():
    account = FakeAccount()
    result = _manager(account).handle({"action": "check", "folder": ""})
    assert result["status"] == "ok"
    assert account.checked_folders == ["INBOX"]


def test_whitespace_folder_for_check_uses_inbox():
    account = FakeAccount()
    result = _manager(account).handle({"action": "check", "folder": "  \t "})
    assert result["status"] == "ok"
    assert account.checked_folders == ["INBOX"]


def test_empty_folder_for_search_uses_inbox():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "search", "query": "unseen", "folder": "",
    })
    assert result["status"] == "ok"
    assert account.searched == [("INBOX", "unseen")]


def test_whitespace_folder_for_search_uses_inbox():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "search", "query": "unseen", "folder": "   ",
    })
    assert result["status"] == "ok"
    assert account.searched == [("INBOX", "unseen")]


# --- move destination must NOT be normalized away --------------------------

def test_move_empty_destination_still_errors():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "move",
        "email_id": "me@example.com:INBOX:42",
        "folder": "",
    })
    assert "error" in result
    assert account.moved == []


def test_move_whitespace_destination_still_errors():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "move",
        "email_id": "me@example.com:INBOX:42",
        "folder": "   ",
    })
    assert "error" in result
    assert account.moved == []


# --- flag error ergonomics --------------------------------------------------

def test_flag_missing_flags_returns_helpful_error():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "flag",
        "email_id": "me@example.com:INBOX:42",
    })
    assert result.get("status") == "error"
    assert "flags is required" in result.get("error", "")
    assert "flags={'seen': true}" in result.get("error", "")
    assert account.flag_calls == []


def test_flag_empty_flags_returns_helpful_error():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "flag",
        "email_id": "me@example.com:INBOX:42",
        "flags": {},
    })
    assert result.get("status") == "error"
    assert "flags is required" in result.get("error", "")
    assert account.flag_calls == []


def test_flag_success_still_calls_store_flags():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "flag",
        "email_id": "me@example.com:INBOX:42",
        "flags": {"seen": True},
    })
    assert result["status"] == "ok"
    assert account.flag_calls == [("INBOX", "42", ["\\Seen"], "+FLAGS")]


# --- unknown account after normalization -----------------------------------

def test_unknown_account_returns_visible_error():
    account = FakeAccount()
    result = _manager(account).handle({
        "action": "check", "account": "nobody@example.com",
    })
    assert result.get("status") == "error"
    assert "nobody@example.com" in result.get("error", "")
