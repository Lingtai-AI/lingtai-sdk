"""Tests for Telegram MCP rich-formatting pass-through (issue #301).

Proves that the in-kernel Telegram MCP manager forwards Bot API
formatting options (parse_mode / entities / caption_entities /
link_preview_options / disable_web_page_preview) through to the account
wrapper for send / reply / edit / media-caption paths, and that callers
who supply none of these options get exactly the previous behaviour.
"""
from __future__ import annotations

import json
from pathlib import Path

from lingtai.mcp_servers.telegram.account import TelegramAccount
from lingtai.mcp_servers.telegram.manager import SCHEMA, TelegramManager


class FakeAccount:
    alias = "mybot"

    def __init__(self) -> None:
        self.calls: list = []

    def send_message(
        self,
        chat_id,
        text,
        reply_markup=None,
        reply_to_message_id=None,
        **kwargs,
    ):
        self.calls.append(
            ("send_message", chat_id, text, reply_markup, reply_to_message_id, kwargs)
        )
        return {"message_id": 111}

    def send_photo(
        self,
        chat_id,
        path,
        caption=None,
        reply_to_message_id=None,
        **kwargs,
    ):
        self.calls.append(
            ("send_photo", chat_id, path, caption, reply_to_message_id, kwargs)
        )
        return {"message_id": 333}

    def send_document(
        self,
        chat_id,
        path,
        caption=None,
        reply_to_message_id=None,
        **kwargs,
    ):
        self.calls.append(
            ("send_document", chat_id, path, caption, reply_to_message_id, kwargs)
        )
        return {"message_id": 222}

    def edit_message(self, **kwargs):
        self.calls.append(("edit_message", kwargs))
        return {"ok": True}

    def set_message_reaction(self, *args, **kwargs):
        # Best-effort reaction call in _send; record nothing meaningful.
        return True


class FakeService:
    def __init__(self) -> None:
        self.default_account = FakeAccount()

    def get_account(self, alias):
        assert alias == "mybot"
        return self.default_account


def _manager(tmp_path):
    service = FakeService()
    manager = TelegramManager(
        service, working_dir=Path(tmp_path), on_inbound=lambda _: None
    )
    return manager, service.default_account


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_exposes_rich_formatting_fields():
    props = SCHEMA["properties"]
    for field in (
        "parse_mode",
        "entities",
        "caption_entities",
        "link_preview_options",
        "disable_web_page_preview",
    ):
        assert field in props

    assert "" in props["parse_mode"]["enum"]
    assert "plain text" in props["parse_mode"]["description"]


def test_schema_allows_empty_chat_action():
    props = SCHEMA["properties"]
    assert "" in props["chat_action"]["enum"]
    assert "empty string" in props["chat_action"]["description"]


def test_schema_guides_generated_charts_to_documents():
    props = SCHEMA["properties"]
    media_description = props["media"]["description"]
    action_description = props["action"]["description"]

    assert "charts" in media_description
    assert "generated artifacts" in media_description
    assert "type='document'" in media_description
    assert "type='photo'" in media_description
    assert "crop" in media_description
    assert "compress" in media_description
    assert "charts" in action_description
    assert "media.type='document'" in action_description


def test_skill_file_exists_with_valid_frontmatter():
    """The manual ships as a standard skill file (SKILL.md) in the Telegram MCP
    package folder, with YAML frontmatter exposing at least name + description."""
    from lingtai.mcp_servers.telegram.manager import (
        _SKILL_BODY,
        _SKILL_FRONTMATTER,
        _SKILL_PATH,
    )

    skill_path = Path(_SKILL_PATH)
    assert skill_path.name == "SKILL.md"
    assert skill_path.is_file()
    # parent folder is the Telegram MCP package
    assert skill_path.parent.name == "telegram"

    assert _SKILL_FRONTMATTER.get("name")
    assert _SKILL_FRONTMATTER.get("description")
    # body is the markdown after the frontmatter fence (no leading '---')
    assert _SKILL_BODY.strip()
    assert not _SKILL_BODY.lstrip().startswith("---")


def test_schema_exposes_manual_action():
    props = SCHEMA["properties"]
    assert "manual" in props["action"]["enum"]
    action_description = props["action"]["description"]
    assert "manual:" in action_description
    assert "progressive-disclosure" in action_description
    # The frontmatter/catalog entry is injected into the schema: the action
    # description advertises the skill name + its description.
    from lingtai.mcp_servers.telegram.manager import _SKILL_FRONTMATTER

    assert _SKILL_FRONTMATTER["name"] in action_description
    # a distinctive phrase from the skill description survives injection
    assert "media.type='document'" in action_description


def test_manual_action_returns_usage_guidance(tmp_path):
    manager, _account = _manager(tmp_path)
    result = manager.handle({"action": "manual"})

    assert result["status"] == "ok"
    manual = result["manual"]
    assert isinstance(manual, str) and manual.strip()

    # Minimal manual contract: return the main SKILL.md body plus its absolute
    # path and parsed metadata. Concrete asset/reference catalogs stay in the
    # SKILL.md text rather than expanding the tool payload/schema.
    assert set(result) == {"status", "action", "skill", "metadata", "path", "manual"}
    assert result["skill"]
    assert result["metadata"].get("name") == result["skill"]
    assert result["metadata"].get("description")
    skill_path = Path(result["path"])
    assert skill_path.is_absolute()
    assert skill_path.name == "SKILL.md"
    assert skill_path.is_file()
    assert "assets" not in result
    assert "references" not in result

    # the returned body is read from SKILL.md, not a hardcoded string
    from lingtai.mcp_servers.telegram.manager import _SKILL_BODY

    assert manual == _SKILL_BODY

    lowered = manual.lower()
    # document / photo / chart guidance
    assert "document" in lowered
    assert "photo" in lowered
    assert "chart" in lowered
    # progressive disclosure framing
    assert "progressive disclosure" in lowered
    # the other facets the manual should cover
    assert "placeholder" in lowered
    assert "reply" in lowered
    assert "parse_mode" in manual
    assert "chat_action" in manual
    assert "error" in lowered


# ---------------------------------------------------------------------------
# Manager pass-through (the acceptance-critical parse_mode path lives here)
# ---------------------------------------------------------------------------


def test_send_passes_parse_mode_entities_and_link_preview(tmp_path):
    manager, account = _manager(tmp_path)
    entities = [{"type": "bold", "offset": 0, "length": 4}]
    link_preview_options = {"is_disabled": True}

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "bold link",
        "parse_mode": "HTML",
        "entities": entities,
        "link_preview_options": link_preview_options,
        "disable_web_page_preview": True,
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "bold link",
        None,
        None,
        {
            "parse_mode": "HTML",
            "entities": entities,
            "link_preview_options": link_preview_options,
            "disable_web_page_preview": True,
        },
    )]


def test_send_without_formatting_is_unchanged(tmp_path):
    """Backward compatibility: a plain send forwards no formatting kwargs."""
    manager, account = _manager(tmp_path)

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "plain",
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "plain",
        None,
        None,
        {},  # no rich-text options leaked in
    )]


def test_empty_parse_mode_is_treated_as_plain_text(tmp_path):
    """Optional parse_mode serialized as "" should mean omitted/plain text."""
    manager, account = _manager(tmp_path)

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "plain",
        "parse_mode": "",
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "plain",
        None,
        None,
        {},
    )]
    sent_files = list((Path(tmp_path) / "telegram" / "mybot" / "sent").glob("*/message.json"))
    assert len(sent_files) == 1
    assert json.loads(sent_files[0].read_text())["parse_mode"] is None


def test_empty_chat_action_with_text_is_treated_as_omitted(tmp_path):
    """Optional chat_action serialized as "" should not block text sends."""
    manager, account = _manager(tmp_path)

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "plain",
        "chat_action": "",
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "plain",
        None,
        None,
        {},
    )]


def test_empty_parse_mode_reply_is_treated_as_plain_text(tmp_path):
    manager, account = _manager(tmp_path)

    result = manager._reply({
        "message_id": "mybot:123:456",
        "text": "plain reply",
        "parse_mode": "",
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "plain reply",
        None,
        456,
        {},
    )]


def test_reply_passes_rich_formatting_options(tmp_path):
    manager, account = _manager(tmp_path)
    entities = [{"type": "code", "offset": 0, "length": 1}]

    result = manager._reply({
        "message_id": "mybot:123:456",
        "text": "x",
        "parse_mode": "MarkdownV2",
        "entities": entities,
    })

    assert result == {"status": "sent", "message_id": "mybot:123:111"}
    assert account.calls == [(
        "send_message",
        123,
        "x",
        None,
        456,
        {"parse_mode": "MarkdownV2", "entities": entities},
    )]


def test_send_media_passes_caption_entities(tmp_path):
    media_path = tmp_path / "demo.txt"
    media_path.write_text("demo", encoding="utf-8")
    manager, account = _manager(tmp_path)
    caption_entities = [{"type": "italic", "offset": 0, "length": 4}]

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "demo",
        "media": {"type": "document", "path": str(media_path)},
        "parse_mode": "HTML",
        "caption_entities": caption_entities,
    })

    assert result == {"status": "sent", "message_id": "mybot:123:222"}
    assert account.calls == [(
        "send_document",
        123,
        str(media_path),
        "demo",
        None,
        {"parse_mode": "HTML", "caption_entities": caption_entities},
    )]


def test_edit_text_passes_parse_mode(tmp_path):
    manager, account = _manager(tmp_path)

    result = manager._edit({
        "message_id": "mybot:123:456",
        "text": "<b>x</b>",
        "parse_mode": "HTML",
    })

    assert result == {"status": "edited", "message_id": "mybot:123:456"}
    assert account.calls == [(
        "edit_message",
        {
            "chat_id": 123,
            "message_id": 456,
            "text": "<b>x</b>",
            "reply_markup": None,
            "is_caption": False,
            "parse_mode": "HTML",
        },
    )]


def test_invalid_parse_mode_is_rejected(tmp_path):
    manager, account = _manager(tmp_path)

    result = manager._send({
        "account": "mybot",
        "chat_id": 123,
        "text": "hello",
        "parse_mode": "BBCode",
    })

    assert result == {"error": "parse_mode must be one of: HTML, MarkdownV2, Markdown"}
    assert account.calls == []


# ---------------------------------------------------------------------------
# Account wrapper payload shaping
# ---------------------------------------------------------------------------


class CaptureAccount(TelegramAccount):
    def __init__(self):
        # Bypass TelegramAccount.__init__ — we only exercise payload building.
        pass

    def _request(self, method, **kwargs):
        return {"method": method, "kwargs": kwargs, "message_id": 99}


def test_account_send_message_payload_includes_rich_options():
    acct = CaptureAccount()
    entities = [{"type": "bold", "offset": 0, "length": 4}]
    result = acct.send_message(
        123,
        "bold",
        parse_mode="HTML",
        entities=entities,
        link_preview_options={"is_disabled": True},
        disable_web_page_preview=False,
    )

    assert result["method"] == "sendMessage"
    payload = result["kwargs"]["json"]
    assert payload["parse_mode"] == "HTML"
    assert payload["entities"] == entities
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["disable_web_page_preview"] is False


def test_account_send_message_payload_plain_has_no_rich_options():
    """Backward compatibility at the wrapper layer."""
    acct = CaptureAccount()
    result = acct.send_message(123, "plain")

    payload = result["kwargs"]["json"]
    assert payload == {"chat_id": 123, "text": "plain"}


def test_account_send_document_serializes_caption_entities(tmp_path):
    media_path = tmp_path / "demo.txt"
    media_path.write_text("demo", encoding="utf-8")
    acct = CaptureAccount()
    caption_entities = [{"type": "code", "offset": 0, "length": 4}]

    result = acct.send_document(
        123,
        str(media_path),
        caption="demo",
        parse_mode="HTML",
        caption_entities=caption_entities,
    )

    assert result["method"] == "sendDocument"
    data = result["kwargs"]["data"]
    assert data["parse_mode"] == "HTML"
    # Multipart fields must be strings: caption_entities is JSON-serialized.
    assert json.loads(data["caption_entities"]) == caption_entities


def test_account_edit_caption_payload_includes_parse_mode():
    acct = CaptureAccount()
    result = acct.edit_message(
        123, 456, "new caption", is_caption=True, parse_mode="MarkdownV2",
    )

    assert result["method"] == "editMessageCaption"
    payload = result["kwargs"]["json"]
    assert payload["caption"] == "new caption"
    assert payload["parse_mode"] == "MarkdownV2"
