"""Tests for the shared curated-MCP scaffolding (issue #513).

Covers the consolidated ``_entrypoint`` (stdio main) and ``_licc_compat``
(LICC client wrapper) helpers plus the per-provider re-export shims.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from lingtai.core.mcp import inbox
from lingtai.mcp_servers import _entrypoint, _licc_compat

PROVIDERS = ("imap", "telegram", "feishu", "wechat", "whatsapp", "cloud_mail")


# ---------------------------------------------------------------------------
# __main__ entrypoints
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", PROVIDERS)
def test_each_provider_exposes_callable_main(name):
    mod = importlib.import_module(f"lingtai.mcp_servers.{name}.__main__")
    assert callable(mod.main)


def test_run_stdio_server_main_invokes_asyncio_run(monkeypatch):
    captured = {}

    def fake_run(coro):
        captured["coro"] = coro

    monkeypatch.setattr(_entrypoint.asyncio, "run", fake_run)
    _entrypoint.run_stdio_server_main(lambda: "SERVE-CORO")
    # serve() is called and its result handed to asyncio.run().
    assert captured["coro"] == "SERVE-CORO"


def test_run_stdio_server_main_swallows_keyboard_interrupt(monkeypatch):
    def boom(coro):
        raise KeyboardInterrupt

    monkeypatch.setattr(_entrypoint.asyncio, "run", boom)
    # Must not propagate — Ctrl-C is a clean shutdown.
    assert _entrypoint.run_stdio_server_main(lambda: None) is None


# ---------------------------------------------------------------------------
# licc shims + constants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", PROVIDERS)
def test_each_provider_licc_reexports_push_inbox_event(name):
    licc = importlib.import_module(f"lingtai.mcp_servers.{name}.licc")
    assert callable(licc.push_inbox_event)
    # The shim must re-export the same callable as the shared wrapper.
    assert licc.push_inbox_event is _licc_compat.push_inbox_event


@pytest.mark.parametrize("name", PROVIDERS)
def test_each_provider_licc_constants_match_inbox(name):
    licc = importlib.import_module(f"lingtai.mcp_servers.{name}.licc")
    assert licc.LICC_VERSION == inbox.LICC_VERSION
    assert licc.INBOX_DIRNAME == inbox.INBOX_DIRNAME
    assert licc.TMP_SUFFIX == inbox.TMP_SUFFIX
    assert licc.EVENT_SUFFIX == inbox.EVENT_SUFFIX


# ---------------------------------------------------------------------------
# Local fallback writer (used only when the kernel helper is unavailable)
# ---------------------------------------------------------------------------

def test_fallback_push_writes_valid_event(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_licc_compat, "_kernel_push_inbox_event", None)
    monkeypatch.setenv("LINGTAI_AGENT_DIR", str(tmp_path))
    monkeypatch.setenv("LINGTAI_MCP_NAME", "imap")

    ok = _licc_compat.push_inbox_event(
        "alice@example.com", "subj", "body", metadata={"k": "v"}, wake=True,
    )
    assert ok is True

    inbox_dir = tmp_path / _licc_compat.INBOX_DIRNAME / "imap"
    events = [p for p in inbox_dir.iterdir() if p.suffix == ".json"]
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["from"] == "alice@example.com"
    assert event["subject"] == "subj"
    valid, err = inbox.validate_event(event)
    assert valid, err


def test_fallback_push_noop_without_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_licc_compat, "_kernel_push_inbox_event", None)
    monkeypatch.delenv("LINGTAI_AGENT_DIR", raising=False)
    monkeypatch.delenv("LINGTAI_MCP_NAME", raising=False)
    assert _licc_compat.push_inbox_event("s", "subj", "body") is False
