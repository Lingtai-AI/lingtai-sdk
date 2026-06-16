"""Regression tests for issue #134.

Telegram notification previews must redact credential-shaped substrings before
the conversation preview enters the agent notification body (and from there, the
agent context and persisted logs). See
``lingtai_kernel.trace_redaction.redact_text`` for the shared redactor.
"""

from __future__ import annotations

import json
from pathlib import Path

from lingtai.mcp_servers.telegram.manager import TelegramManager
from lingtai_kernel.trace_redaction import redact_text


class _DummyService:
    """Minimal stand-in; ``_build_conversation_preview`` never touches it."""


def _make_manager(tmp_path: Path) -> TelegramManager:
    return TelegramManager(
        _DummyService(),  # type: ignore[arg-type]
        working_dir=tmp_path,
        on_inbound=lambda _payload: None,
    )


def _write_inbox_message(
    tmp_path: Path,
    account: str,
    chat_id: int,
    msg_id: int,
    *,
    text: str,
    sender: str = "alice",
    date: str = "2026-06-16T12:00:00Z",
) -> None:
    compound_id = f"{account}:{chat_id}:{msg_id}"
    msg_dir = tmp_path / "telegram" / account / "inbox" / str(msg_id)
    msg_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": compound_id,
        "from": {"username": sender},
        "chat": {"id": chat_id},
        "date": date,
        "text": text,
        "media": None,
        "reply_to_message_id": None,
        "callback_query": None,
    }
    (msg_dir / "message.json").write_text(json.dumps(payload), encoding="utf-8")


def test_preview_redacts_bearer_token(tmp_path: Path) -> None:
    account, chat_id = "default", 4242
    secret = "C" * 12 + "." + "D" * 12 + "_" + "E" * 12
    _write_inbox_message(
        tmp_path, account, chat_id, 1,
        text=f"curl -H 'Authorization: Bearer {secret}' https://api.example.com",
    )
    mgr = _make_manager(tmp_path)

    preview = mgr._build_conversation_preview(account, chat_id, f"{account}:{chat_id}:1")

    assert secret not in preview
    assert "<REDACTED:" in preview
    # Surrounding non-secret prose stays readable.
    assert "curl -H" in preview
    assert "https://api.example.com" in preview


def test_preview_redacts_assorted_secret_shapes(tmp_path: Path) -> None:
    account, chat_id = "default", 99
    telegram_bot = "123456789" + ":" + "A" * 35
    openai_key = "sk-" + "B" * 40
    github_pat = "ghp_" + "F" * 36
    _write_inbox_message(tmp_path, account, chat_id, 1, text=f"bot token {telegram_bot}")
    _write_inbox_message(tmp_path, account, chat_id, 2, text=f"openai key sk: {openai_key}")
    _write_inbox_message(tmp_path, account, chat_id, 3, text=f"gh token {github_pat}")
    _write_inbox_message(tmp_path, account, chat_id, 4, text="api_key=supersecretvalue123")
    mgr = _make_manager(tmp_path)

    preview = mgr._build_conversation_preview(account, chat_id, f"{account}:{chat_id}:4")

    for secret in (telegram_bot, openai_key, github_pat, "supersecretvalue123"):
        assert secret not in preview, f"leaked secret: {secret}"
    assert preview.count("<REDACTED:") >= 4


def test_preview_preserves_ordinary_prose(tmp_path: Path) -> None:
    account, chat_id = "default", 7
    prose = "Hey, can you review the pull request and merge it by Friday? Thanks!"
    _write_inbox_message(tmp_path, account, chat_id, 1, text=prose)
    mgr = _make_manager(tmp_path)

    preview = mgr._build_conversation_preview(account, chat_id, f"{account}:{chat_id}:1")

    assert prose in preview
    assert "<REDACTED:" not in preview


def test_reply_quote_redacts_before_truncating_long_secret(tmp_path: Path) -> None:
    account, chat_id = "default", 55
    secret = "A" * 12 + "." + "B" * 60
    _write_inbox_message(
        tmp_path,
        account,
        chat_id,
        1,
        text=f"Authorization: Bearer {secret}",
    )
    _write_inbox_message(tmp_path, account, chat_id, 2, text="reply", sender="bob")
    msg_file = tmp_path / "telegram" / account / "inbox" / "2" / "message.json"
    payload = json.loads(msg_file.read_text(encoding="utf-8"))
    payload["reply_to_message_id"] = 1
    msg_file.write_text(json.dumps(payload), encoding="utf-8")
    mgr = _make_manager(tmp_path)

    preview = mgr._build_conversation_preview(account, chat_id, f"{account}:{chat_id}:2")

    assert secret not in preview
    assert "<REDACTED:" in preview
    assert "Bearer" in preview


def test_fallback_preview_redacts_before_truncating_long_secret(tmp_path: Path) -> None:
    account, chat_id = "default", 66
    secret = "C" * 12 + "." + "D" * 360
    received: list[dict] = []
    mgr = TelegramManager(
        _DummyService(),  # type: ignore[arg-type]
        working_dir=tmp_path,
        on_inbound=received.append,
    )

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("force fallback")

    mgr._build_conversation_preview = _boom  # type: ignore[method-assign]
    mgr.on_incoming(
        account,
        {
            "message": {
                "message_id": 1,
                "chat": {"id": chat_id},
                "date": 0,
                "from": {"username": "alice"},
                "text": f"Authorization: Bearer {secret}",
            }
        },
    )

    assert received
    preview = received[0]["body"]
    assert secret not in preview
    assert "<REDACTED:" in preview
    assert "Bearer" in preview
