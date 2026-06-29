"""Tests for web_search capability."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lingtai.agent import Agent
from lingtai.capabilities.web_search import WebSearchManager, setup
from lingtai.services.websearch import SearchResult, SearchService, create_search_service
from tests._service_helpers import make_gemini_mock_service as make_mock_service




def test_web_search_added_by_capability(tmp_path):
    """capabilities with provider should register the web_search tool."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path,
                       capabilities={"web_search": {"provider": "duckduckgo"}})
    assert "web_search" in agent._tool_handlers


def test_web_search_with_dedicated_service():
    """web_search capability should use SearchService if provided."""
    mock_result = MagicMock()
    mock_result.title = "Python"
    mock_result.url = "https://python.org"
    mock_result.snippet = "Python programming language"
    mock_search_svc = MagicMock()
    mock_search_svc.search.return_value = [mock_result]
    agent = MagicMock()
    mgr = WebSearchManager(agent, search_service=mock_search_svc)
    result = mgr.handle({"query": "python"})
    assert result["status"] == "ok"
    assert "Python" in result["results"]
    mock_search_svc.search.assert_called_once()


def test_web_search_missing_query(tmp_path):
    """web_search should return error for missing query."""
    agent = Agent(service=make_mock_service(), agent_name="test", working_dir=tmp_path,
                       capabilities={"web_search": {"provider": "duckduckgo"}})
    result = agent._tool_handlers["web_search"]({"query": ""})
    assert result.get("status") == "error"


def test_web_search_manager_uses_search_service():
    """WebSearchManager should call search_service.search() when available."""
    mock_svc = MagicMock(spec=SearchService)
    mock_svc.search.return_value = [
        SearchResult(title="Result", url="https://example.com", snippet="A snippet")
    ]
    agent = MagicMock()
    mgr = WebSearchManager(agent, search_service=mock_svc)
    result = mgr.handle({"query": "test"})
    assert result["status"] == "ok"
    assert "Result" in result["results"]
    mock_svc.search.assert_called_once_with("test")


def test_web_search_service_exception():
    """WebSearchManager should return error if SearchService raises."""
    mock_svc = MagicMock(spec=SearchService)
    mock_svc.search.side_effect = RuntimeError("connection failed")
    agent = MagicMock()
    mgr = WebSearchManager(agent, search_service=mock_svc)
    result = mgr.handle({"query": "test"})
    assert result["status"] == "error"
    assert "connection failed" in result["message"]


def test_create_search_service_duckduckgo():
    """Factory should create DuckDuckGoSearchService."""
    from lingtai.services.websearch.duckduckgo import DuckDuckGoSearchService
    svc = create_search_service("duckduckgo")
    assert isinstance(svc, DuckDuckGoSearchService)


def test_create_search_service_requires_key():
    """Factory should raise RuntimeError for providers needing api_key when none given."""
    with pytest.raises(RuntimeError, match="requires an api_key"):
        create_search_service("anthropic")


def test_create_search_service_unknown():
    """Factory should raise ValueError for unknown provider."""
    with pytest.raises(ValueError, match="Unknown web search provider"):
        create_search_service("nonexistent", api_key="key")


def test_web_search_with_provider_kwarg(tmp_path):
    """web_search capability with provider= should create service via factory."""
    agent = Agent(
        service=make_mock_service(),
        agent_name="test",
        working_dir=tmp_path,
        capabilities={"web_search": {"provider": "duckduckgo"}},
    )
    assert "web_search" in agent._tool_handlers


def test_web_search_setup_resolves_api_key_env(monkeypatch):
    """setup() resolves api_key_env before constructing provider services."""
    monkeypatch.setenv("WEB_SEARCH_TEST_API_KEY", "sk-from-env")
    agent = MagicMock()
    agent._config.language = "en"
    agent.service._base_url = None

    with patch("lingtai.capabilities.web_search.create_search_service") as mock_factory:
        mock_factory.return_value = MagicMock(spec=SearchService)
        mgr = setup(agent, provider="gemini", api_key_env="WEB_SEARCH_TEST_API_KEY")

    assert isinstance(mgr, WebSearchManager)
    mock_factory.assert_called_once()
    assert mock_factory.call_args.args == ("gemini",)
    assert mock_factory.call_args.kwargs["api_key"] == "sk-from-env"


def test_web_search_setup_api_key_env_overrides_raw_key(monkeypatch):
    """api_key_env takes precedence over a raw api_key, matching vision."""
    monkeypatch.setenv("WEB_SEARCH_TEST_API_KEY", "sk-from-env")
    agent = MagicMock()
    agent._config.language = "en"
    agent.service._base_url = None

    with patch("lingtai.capabilities.web_search.create_search_service") as mock_factory:
        mock_factory.return_value = MagicMock(spec=SearchService)
        setup(
            agent,
            provider="gemini",
            api_key="sk-raw",
            api_key_env="WEB_SEARCH_TEST_API_KEY",
        )

    assert mock_factory.call_args.kwargs["api_key"] == "sk-from-env"


def test_inherited_web_search_env_key_registers(tmp_path, monkeypatch):
    """A provider:inherit web_search with env-only credentials boots."""
    from lingtai_kernel.presets import expand_inherit

    monkeypatch.setenv("WEB_SEARCH_TEST_API_KEY", "sk-from-env")
    capabilities = {"web_search": {"provider": "inherit"}}
    expand_inherit(
        capabilities,
        {
            "provider": "gemini",
            "api_key": None,
            "api_key_env": "WEB_SEARCH_TEST_API_KEY",
        },
    )

    with patch("lingtai.capabilities.web_search.create_search_service") as mock_factory:
        mock_factory.return_value = MagicMock(spec=SearchService)
        service = make_mock_service()
        service._base_url = None
        agent = Agent(
            service=service,
            agent_name="test",
            working_dir=tmp_path / "test",
            capabilities=capabilities,
        )

    try:
        assert agent.has_capability("web_search") is True
        assert "web_search" in agent._tool_handlers
        call = next(
            call for call in mock_factory.call_args_list
            if call.args == ("gemini",)
        )
        assert call.kwargs["api_key"] == "sk-from-env"
    finally:
        agent.stop(timeout=1.0)
