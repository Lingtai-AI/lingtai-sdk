"""Tests for graceful inherit fallback in capability setup().

When a capability's resolved provider isn't in its supported list, it should
either fall back to its declared agnostic fallback or silently skip
registration. Never raise."""
import logging
from unittest.mock import MagicMock

import pytest


def _stub_agent(language: str = "en"):
    a = MagicMock()
    a._config = MagicMock()
    a._config.language = language
    a._tool_handlers = {}
    a._tool_schemas = {}
    a._capability_managers = {}
    a._log_events = []
    # service._base_url = None so resolve_media_host returns None (no real LLM)
    a.service = MagicMock()
    a.service._base_url = None
    def _log(event, **kw):
        a._log_events.append((event, kw))
    a._log = _log
    # add_tool is what setup() typically calls — record it on the stub
    def _add_tool(name, **kw):
        a._tool_handlers[name] = kw.get("handler")
        a._tool_schemas[name] = kw.get("schema")
    a.add_tool = _add_tool
    a.add_capability = MagicMock()
    return a


def test_web_search_unknown_provider_falls_back_to_duckduckgo():
    """web_search with provider='deepseek' (unknown) falls back to duckduckgo."""
    from lingtai.capabilities.web_search import setup as ws_setup
    a = _stub_agent()
    # Pretend agent's main LLM is deepseek (which has no web search service)
    ws_setup(a, provider="deepseek", api_key=None)
    # Must NOT raise; should have registered web_search via duckduckgo
    assert "web_search" in a._tool_handlers


def test_vision_unknown_provider_silently_skips():
    """vision with provider='deepseek' (no vision, no fallback) skips registration."""
    from lingtai.capabilities.vision import setup as vision_setup
    a = _stub_agent()
    result = vision_setup(a, provider="deepseek", api_key=None, api_key_env="X")
    assert "vision" not in a._tool_handlers
    events = [e for e, _ in a._log_events]
    assert "capability_skipped" in events


def test_web_search_supported_provider_uses_it(monkeypatch):
    """web_search with provider='gemini' (supported) uses gemini, no fallback."""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-test")
    from lingtai.capabilities.web_search import setup as ws_setup
    a = _stub_agent()
    ws_setup(a, provider="gemini", api_key="sk-test")
    assert "web_search" in a._tool_handlers


def test_web_search_no_provider_uses_duckduckgo():
    """web_search with no provider arg defaults to duckduckgo (existing behavior)."""
    from lingtai.capabilities.web_search import setup as ws_setup
    a = _stub_agent()
    ws_setup(a)
    assert "web_search" in a._tool_handlers
