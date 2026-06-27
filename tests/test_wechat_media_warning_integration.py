"""Integration: _on_incoming surfaces a magic-byte mismatch warning.

When a FILE attachment downloads as encrypted/cache bytes (extension says
.pdf/.zip but content doesn't match), the conversation body landed in the
inbox must carry a clear warning and a recovery hint instead of presenting a
normal-looking file path. Valid attachments land with no warning suffix.

Ported from Lingtai-AI/lingtai-wechat#15 into the in-kernel WeChat addon.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from lingtai.mcp_servers.wechat import media as media_mod
from lingtai.mcp_servers.wechat.manager import WechatManager
from lingtai.mcp_servers.wechat.types import (
    CDNMedia, FileItem, ImageItem, MessageItem, MessageItemType, WeixinMessage,
)

ENCRYPTED_ZIP_LIKE = bytes.fromhex("241f07f6bcab69005d87b05f3c095e27") + b"\x00" * 32
VALID_PDF = b"%PDF-1.4\n%%EOF\n"
VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _manager(tmp_path: Path, events: list[dict]) -> WechatManager:
    return WechatManager(
        token="test-token",
        user_id="test-bot",
        working_dir=tmp_path,
        on_inbound=events.append,
    )


def _patch_download(monkeypatch, payload: bytes) -> None:
    """Stub download_media to write `payload` to the destination path instead
    of hitting the CDN, returning the local path like the real function."""

    async def _fake_download(cdn_media, dest_dir, filename="media"):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        dest_path.write_bytes(payload)
        return str(dest_path)

    monkeypatch.setattr(media_mod, "download_media", _fake_download)


def _file_msg(filename: str) -> WeixinMessage:
    return WeixinMessage(
        message_id=1001,
        from_user_id="wxid_alice@im.wechat",
        message_type=1,
        item_list=[
            MessageItem(
                type=MessageItemType.FILE,
                file_item=FileItem(
                    media=CDNMedia(full_url="https://cdn.example/x"),
                    file_name=filename,
                ),
            )
        ],
    )


def _image_msg() -> WeixinMessage:
    return WeixinMessage(
        message_id=1002,
        from_user_id="wxid_alice@im.wechat",
        message_type=1,
        item_list=[
            MessageItem(
                type=MessageItemType.IMAGE,
                image_item=ImageItem(media=CDNMedia(full_url="https://cdn.example/y")),
            )
        ],
    )


def _landed_body(tmp_path: Path) -> str:
    inbox = tmp_path / "wechat" / "inbox"
    dirs = [d for d in inbox.iterdir() if (d / "message.json").is_file()]
    assert len(dirs) == 1, dirs
    data = json.loads((dirs[0] / "message.json").read_text(encoding="utf-8"))
    return data["body"]


def test_mismatched_file_attachment_lands_with_warning(tmp_path, monkeypatch):
    _patch_download(monkeypatch, ENCRYPTED_ZIP_LIKE)
    events: list[dict] = []
    mgr = _manager(tmp_path, events)
    asyncio.run(mgr._on_incoming(_file_msg("archive.zip")))

    body = _landed_body(tmp_path)
    assert "[File: archive.zip" in body
    assert "WARNING" in body.upper()
    assert "save as" in body.lower()


def test_valid_file_attachment_lands_without_warning(tmp_path, monkeypatch):
    _patch_download(monkeypatch, VALID_PDF)
    events: list[dict] = []
    mgr = _manager(tmp_path, events)
    asyncio.run(mgr._on_incoming(_file_msg("report.pdf")))

    body = _landed_body(tmp_path)
    assert "[File: report.pdf" in body
    assert "WARNING" not in body.upper()


def test_valid_image_lands_without_warning(tmp_path, monkeypatch):
    # PNG bytes under a fabricated .jpg name must NOT warn.
    _patch_download(monkeypatch, VALID_PNG)
    events: list[dict] = []
    mgr = _manager(tmp_path, events)
    asyncio.run(mgr._on_incoming(_image_msg()))

    body = _landed_body(tmp_path)
    assert "[Image:" in body
    assert "WARNING" not in body.upper()


def test_non_image_under_image_item_warns(tmp_path, monkeypatch):
    _patch_download(monkeypatch, ENCRYPTED_ZIP_LIKE)
    events: list[dict] = []
    mgr = _manager(tmp_path, events)
    asyncio.run(mgr._on_incoming(_image_msg()))

    body = _landed_body(tmp_path)
    assert "[Image:" in body
    assert "WARNING" in body.upper()
