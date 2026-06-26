"""Bundled-skill (SKILL.md) `manual` action tests for curated MCP servers.

Each curated MCP ships a standard skill file (``SKILL.md``: YAML frontmatter +
markdown body) in its package folder, exposes an ``action='manual'`` that returns
the full body plus parsed metadata, and injects the frontmatter name/description
into its tool schema as a progressive-disclosure catalog entry. This mirrors the
Telegram MCP pattern (covered by tests/test_telegram_rich_formatting.py).

The ``manual`` action is account-independent and reads only module-level
constants, so we call it on a bare instance (``object.__new__``) without standing
up real accounts/services.
"""
from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from lingtai.mcp_servers import _skill

from lingtai.mcp_servers.feishu import manager as feishu_mgr
from lingtai.mcp_servers.wechat import manager as wechat_mgr
from lingtai.mcp_servers.imap import manager as imap_mgr
from lingtai.mcp_servers.cloud_mail import manager as cloud_mail_mgr
from lingtai.mcp_servers.whatsapp import manager as whatsapp_mgr


# (module, package-folder name, manual-method name, expected skill name)
_CASES = [
    (feishu_mgr, "feishu", "_manual", "feishu-mcp-manual"),
    (wechat_mgr, "wechat", "_handle_manual", "wechat-mcp-manual"),
    (imap_mgr, "imap", "_manual", "imap-mcp-manual"),
    (cloud_mail_mgr, "cloud_mail", "_handle_manual", "cloud-mail-mcp-manual"),
    (whatsapp_mgr, "whatsapp", "_manual", "whatsapp-mcp-manual"),
]

_MANAGER_CLS = {
    "feishu": "FeishuManager",
    "wechat": "WechatManager",
    "imap": "IMAPMailManager",
    "cloud_mail": "CloudMailManager",
    "whatsapp": "WhatsAppManager",
}


def _ids(case):
    return case[1]


def _call_manual(module, folder, method_name):
    """Call the manual handler on a bare instance (no __init__)."""
    cls = getattr(module, _MANAGER_CLS[folder])
    inst = object.__new__(cls)
    method = getattr(inst, method_name)
    # whatsapp's dispatcher passes args; others take none — support both.
    try:
        return method({"action": "manual"})
    except TypeError:
        return method()


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_skill_file_exists_with_valid_frontmatter(case):
    module, folder, _method, expected_name = case
    skill_path = Path(module._SKILL_PATH)
    assert skill_path.name == "SKILL.md"
    assert skill_path.is_file()
    # parent folder is the MCP package
    assert skill_path.parent.name == folder

    assert module._SKILL_FRONTMATTER.get("name") == expected_name
    assert module._SKILL_FRONTMATTER.get("description")
    # body is the markdown after the frontmatter fence (no leading '---')
    assert module._SKILL_BODY.strip()
    assert not module._SKILL_BODY.lstrip().startswith("---")


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_schema_exposes_manual_action(case):
    module, _folder, _method, expected_name = case
    props = module.SCHEMA["properties"]
    assert "manual" in props["action"]["enum"]
    action_description = props["action"]["description"]
    assert "manual:" in action_description
    assert "progressive-disclosure" in action_description
    # The frontmatter/catalog entry is injected: name + a phrase from the
    # skill description survive into the schema.
    assert expected_name in action_description
    assert "progressive-disclosure usage manual" in action_description


@pytest.mark.parametrize("case", _CASES, ids=_ids)
def test_manual_action_returns_usage_guidance(case):
    module, folder, method, expected_name = case
    result = _call_manual(module, folder, method)

    assert result["status"] == "ok"
    assert result["action"] == "manual"
    manual = result["manual"]
    assert isinstance(manual, str) and manual.strip()

    # Minimal manual contract: return the main SKILL.md body plus its absolute
    # path and parsed metadata. Concrete asset/reference catalogs stay in the
    # SKILL.md text rather than expanding the tool payload/schema.
    assert set(result) == {"status", "action", "skill", "metadata", "path", "manual"}
    assert result["skill"] == expected_name
    assert result["metadata"].get("name") == expected_name
    assert result["metadata"].get("description")
    skill_path = Path(result["path"])
    assert skill_path.is_absolute()
    assert skill_path.name == "SKILL.md"
    assert skill_path.is_file()
    assert "assets" not in result
    assert "references" not in result

    # the returned body is read from SKILL.md, not a hardcoded string
    assert manual == module._SKILL_BODY

    lowered = manual.lower()
    # progressive disclosure framing + the core facets every skeleton covers
    assert "progressive disclosure" in lowered
    assert "send" in lowered
    assert "read" in lowered
    assert "search" in lowered
    # side-effect / safety caveat
    assert "side effect" in lowered or "error" in lowered


def test_shared_split_frontmatter_handles_block_scalar():
    fm, body = _skill.split_frontmatter(
        "---\nname: x\ndescription: |\n  line one\n  line two\n---\nbody text\n"
    )
    assert fm["name"] == "x"
    assert fm["description"] == "line one line two"
    assert body.strip() == "body text"


def test_shared_split_frontmatter_no_frontmatter_passthrough():
    text = "no frontmatter here"
    fm, body = _skill.split_frontmatter(text)
    assert fm == {}
    assert body == text


def test_email_manuals_warn_about_external_side_effects():
    # IMAP and Cloud Mail send real outbound email — the skeleton must call out
    # the external, hard-to-undo side effect.
    for module in (imap_mgr, cloud_mail_mgr):
        lowered = module._SKILL_BODY.lower()
        assert "real" in lowered
        assert "side effect" in lowered


def test_mcp_skill_package_data_keeps_reference_and_asset_sidecars_packaged():
    """Side files are discovered from SKILL.md text, but must ship in wheels."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    package_data = tomllib.loads(pyproject.read_text())["tool"]["setuptools"]["package-data"]
    for package in [
        "lingtai.mcp_servers.telegram",
        "lingtai.mcp_servers.feishu",
        "lingtai.mcp_servers.wechat",
        "lingtai.mcp_servers.whatsapp",
        "lingtai.mcp_servers.imap",
        "lingtai.mcp_servers.cloud_mail",
    ]:
        entries = package_data[package]
        assert "SKILL.md" in entries
        assert "reference/**/*" in entries
        assert "assets/**/*" in entries
