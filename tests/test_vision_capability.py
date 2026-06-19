"""Tests for vision capability and VisionService."""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lingtai.capabilities.vision import VisionManager, setup
from lingtai.services.vision import VisionService, create_vision_service


def make_mock_service():
    svc = MagicMock()
    svc.provider = "gemini"
    svc.model = "gemini-test"
    svc._key_resolver = MagicMock(return_value="fake-key")
    return svc


def make_mock_agent(tmp_path, svc=None):
    agent = MagicMock()
    agent.service = svc or make_mock_service()
    agent._config = MagicMock()
    agent._config.language = "en"
    agent._working_dir = tmp_path
    return agent


def test_vision_added_by_setup(tmp_path):
    """setup() should register the vision tool on the agent."""
    mock_svc = MagicMock(spec=VisionService)
    agent = make_mock_agent(tmp_path)
    mgr = setup(agent, vision_service=mock_svc)
    agent.add_tool.assert_called_once()
    assert agent.add_tool.call_args[1]["schema"] is not None or agent.add_tool.call_args[0][1] is not None
    assert isinstance(mgr, VisionManager)


def test_vision_with_dedicated_service(tmp_path):
    """Vision capability should use VisionService if provided."""
    mock_vision_svc = MagicMock(spec=VisionService)
    mock_vision_svc.analyze_image.return_value = "A dog in the park"

    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)

    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"\xff\xd8\xff fake jpeg")
    result = mgr.handle({"image_path": str(img_path)})
    assert result["status"] == "ok"
    assert "dog" in result["analysis"]
    mock_vision_svc.analyze_image.assert_called_once()


def test_vision_missing_image(tmp_path):
    """Vision should return error for missing image file."""
    mock_vision_svc = MagicMock(spec=VisionService)
    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)
    result = mgr.handle({"image_path": "/nonexistent/image.png"})
    assert result.get("status") == "error"


def test_vision_relative_path(tmp_path):
    """VisionManager should resolve relative paths against working directory."""
    mock_vision_svc = MagicMock(spec=VisionService)
    mock_vision_svc.analyze_image.return_value = "An image"

    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)
    img_path = tmp_path / "photo.png"
    img_path.write_bytes(b"\x89PNG fake")
    result = mgr.handle({"image_path": "photo.png"})
    assert result["status"] == "ok"
    mock_vision_svc.analyze_image.assert_called_once_with(str(img_path), prompt="Describe what you see in this image.")


def test_vision_service_error_handled(tmp_path):
    """VisionManager should catch VisionService exceptions and return error dict."""
    mock_vision_svc = MagicMock(spec=VisionService)
    mock_vision_svc.analyze_image.side_effect = RuntimeError("API down")

    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG fake")
    result = mgr.handle({"image_path": str(img_path)})
    assert result["status"] == "error"
    assert "API down" in result["message"]


def test_vision_empty_response_is_error(tmp_path):
    """VisionManager should return error when service returns empty string."""
    mock_vision_svc = MagicMock(spec=VisionService)
    mock_vision_svc.analyze_image.return_value = ""

    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)
    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG fake")
    result = mgr.handle({"image_path": str(img_path)})
    assert result["status"] == "error"


def test_vision_setup_with_provider_and_key(tmp_path):
    """setup() should create a VisionService from provider + api_key."""
    with patch("lingtai.capabilities.vision.create_vision_service") as mock_factory:
        mock_svc = MagicMock(spec=VisionService)
        mock_factory.return_value = mock_svc

        agent = make_mock_agent(tmp_path)
        mgr = setup(agent, provider="anthropic", api_key="sk-test")

        mock_factory.assert_called_once_with("anthropic", api_key="sk-test")
        assert isinstance(mgr, VisionManager)


def test_vision_setup_resolves_api_key_env(tmp_path, monkeypatch):
    """setup() should resolve api_key_env before constructing provider services."""
    monkeypatch.setenv("VISION_TEST_API_KEY", "sk-from-env")
    with patch("lingtai.capabilities.vision.create_vision_service") as mock_factory:
        mock_svc = MagicMock(spec=VisionService)
        mock_factory.return_value = mock_svc

        agent = make_mock_agent(tmp_path)
        mgr = setup(agent, provider="zhipu", api_key_env="VISION_TEST_API_KEY")

        mock_factory.assert_called_once()
        assert mock_factory.call_args.args == ("zhipu",)
        assert mock_factory.call_args.kwargs["api_key"] == "sk-from-env"
        assert isinstance(mgr, VisionManager)


def test_vision_setup_with_codex_provider_without_api_key(tmp_path):
    """Codex vision uses ChatGPT OAuth, so setup should not require api_key."""
    with patch("lingtai.capabilities.vision.create_vision_service") as mock_factory:
        mock_svc = MagicMock(spec=VisionService)
        mock_factory.return_value = mock_svc

        agent = make_mock_agent(tmp_path)
        mgr = setup(agent, provider="codex")

        mock_factory.assert_called_once_with("codex", api_key=None)
        assert isinstance(mgr, VisionManager)


def test_vision_setup_unsupported_provider_skips(tmp_path):
    """Unsupported providers gracefully skip: setup returns None and logs capability_skipped.

    The mock agent's `service._provider_defaults` is a MagicMock (not a dict),
    so the OpenAI-compat fallback does not engage; the graceful skip path
    runs instead. Agent.py's capability loader handles None as a no-op.
    """
    agent = make_mock_agent(tmp_path)
    result = setup(agent, provider="not-real")
    assert result is None
    agent.add_tool.assert_not_called()
    agent._log.assert_called_with(
        "capability_skipped",
        capability="vision",
        requested_provider="not-real",
        reason="no vision support for provider 'not-real' (api_compat='')",
    )


def test_vision_setup_requires_provider_or_service(tmp_path):
    """setup() without provider or service raises ValueError."""
    agent = make_mock_agent(tmp_path)
    with pytest.raises(ValueError, match="vision capability requires"):
        setup(agent)


def test_create_vision_service_codex_without_api_key(monkeypatch):
    """Codex factory should not require api_key and should use OAuth manager."""
    fake_openai = SimpleNamespace(OpenAI=MagicMock())
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with patch("lingtai.services.vision.codex.CodexTokenManager") as mock_mgr:
        svc = create_vision_service("codex")

    from lingtai.services.vision.codex import CodexVisionService

    assert isinstance(svc, CodexVisionService)
    mock_mgr.assert_called_once_with(token_path=None)


def test_create_vision_service_codex_ignores_extra_preset_kwargs(monkeypatch):
    """Codex vision should tolerate irrelevant preset kwargs like api_key_env."""
    fake_openai = SimpleNamespace(OpenAI=MagicMock())
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with patch("lingtai.services.vision.codex.CodexTokenManager") as mock_mgr:
        svc = create_vision_service(
            "codex",
            api_key_env="IGNORED",
            provider_note="from preset",
        )

    from lingtai.services.vision.codex import CodexVisionService

    assert isinstance(svc, CodexVisionService)
    mock_mgr.assert_called_once_with(token_path=None)


def test_codex_vision_service_streams_responses_api(monkeypatch, tmp_path):
    """CodexVisionService should parse streaming output_text deltas without network calls."""
    img_path = tmp_path / "chart.png"
    img_path.write_bytes(b"fake png bytes")

    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.output_text.delta", delta="A chart"),
        SimpleNamespace(type="response.output_text.delta", delta=" with candles"),
        SimpleNamespace(type="response.completed"),
    ]
    responses = MagicMock()
    responses.create.return_value = events
    client = SimpleNamespace(responses=responses)
    openai_cls = MagicMock(return_value=client)
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=openai_cls))

    with patch("lingtai.services.vision.codex.CodexTokenManager") as mock_mgr_cls:
        mock_mgr_cls.return_value.get_access_token.return_value = "oauth-token"
        from lingtai.services.vision.codex import CodexVisionService

        svc = CodexVisionService(timeout=9.5)
        result = svc.analyze_image(str(img_path), prompt="What is shown?")

    assert result == "A chart with candles"
    openai_cls.assert_called_once_with(
        api_key="oauth-token",
        base_url="https://chatgpt.com/backend-api/codex",
        timeout=9.5,
    )
    responses.create.assert_called_once()
    kwargs = responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-5.5"
    assert kwargs["instructions"]
    assert kwargs["stream"] is True
    assert kwargs["store"] is False
    assert "max_output_tokens" not in kwargs
    content = kwargs["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "What is shown?"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")

def test_create_vision_service_unknown_provider():
    """create_vision_service should raise ValueError for unknown providers."""
    with pytest.raises(ValueError, match="Unsupported vision provider"):
        create_vision_service("unknown_provider", api_key="key")


def test_vision_service_abc_cannot_instantiate():
    """VisionService ABC should not be instantiable directly."""
    with pytest.raises(TypeError):
        VisionService()


def test_vision_empty_image_path(tmp_path):
    """VisionManager should return error for empty image path."""
    mock_vision_svc = MagicMock(spec=VisionService)
    agent = make_mock_agent(tmp_path)
    mgr = VisionManager(agent, vision_service=mock_vision_svc)
    result = mgr.handle({"image_path": ""})
    assert result["status"] == "error"
    assert "image_path" in result["message"].lower() or "provide" in result["message"].lower()


def test_vision_setup_no_provider_raises(tmp_path):
    """setup() without provider or service should raise ValueError."""
    agent = make_mock_agent(tmp_path)
    with pytest.raises(ValueError, match="vision capability requires"):
        setup(agent)


def make_custom_agent(tmp_path, *, api_compat=None, base_url=None, model=None):
    """Agent whose main LLM is a `provider='custom'` relay.

    `_provider_defaults` is the real shape: ``{provider_name: defaults_dict}``,
    so the fallback must peek into the per-provider bucket to read api_compat.
    """
    svc = MagicMock()
    svc.provider = "custom"
    svc._model = model
    svc._base_url = base_url
    svc._provider_defaults = {"custom": {"api_compat": api_compat}} if api_compat else {"custom": {}}
    return make_mock_agent(tmp_path, svc=svc)


# ---------------------------------------------------------------------------
# Issue #114 — vision fallback for provider='custom'
# ---------------------------------------------------------------------------

def test_vision_fallback_reads_api_compat_from_provider_bucket(tmp_path):
    """C-1: api_compat is read from _provider_defaults[provider], not the outer dict.

    `_provider_defaults` is shaped {provider_name: defaults_dict}. The old code
    called defaults.get("api_compat") on the OUTER dict, which always returned
    None, so the OpenAI fallback never engaged for custom providers.
    """
    with patch("lingtai.services.vision.openai.OpenAIVisionService") as mock_cls:
        agent = make_custom_agent(
            tmp_path, api_compat="openai", base_url="http://127.0.0.1:34891/v1", model="GLM-5.1"
        )
        mgr = setup(agent, provider="custom", api_key="sk-test")

        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert kwargs["base_url"] == "http://127.0.0.1:34891/v1"
        assert kwargs["model"] == "GLM-5.1"
        assert isinstance(mgr, VisionManager)


def test_vision_fallback_anthropic_compat_routes_to_anthropic_service(tmp_path):
    """C-2: api_compat='anthropic' routes vision through AnthropicVisionService.

    Previously only the openai branch existed; anthropic-compat custom proxies
    fell through to capability_skipped even though AnthropicVisionService exists.
    """
    with patch("lingtai.services.vision.anthropic.AnthropicVisionService") as mock_cls:
        agent = make_custom_agent(
            tmp_path, api_compat="anthropic", base_url="http://127.0.0.1:34891", model="GLM-5.1"
        )
        mgr = setup(agent, provider="custom", api_key="sk-test")

        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["api_key"] == "sk-test"
        assert kwargs["base_url"] == "http://127.0.0.1:34891"
        assert kwargs["model"] == "GLM-5.1"
        assert isinstance(mgr, VisionManager)


def test_vision_fallback_honors_capability_kwargs_over_service(tmp_path):
    """C-3: explicit capability model/base_url/api_compat override the main LLM.

    The whole point of explicit kwargs in init.json is to route vision through a
    different (vision-capable) model than the text-only main LLM. The fallback
    must consult kwargs first and only fall back to service._model/._base_url.
    """
    with patch("lingtai.services.vision.openai.OpenAIVisionService") as mock_cls:
        # main LLM is GLM-5.1 (text-only) on an anthropic-compat proxy
        agent = make_custom_agent(
            tmp_path, api_compat="anthropic", base_url="http://127.0.0.1:34891", model="GLM-5.1"
        )
        # capability explicitly overrides: openai-compat vision model on the /v1 route
        mgr = setup(
            agent,
            provider="custom",
            api_key="sk-test",
            api_compat="openai",
            model="Kimi-K2.6",
            base_url="http://127.0.0.1:34891/v1",
            max_tokens=2048,
        )

        mock_cls.assert_called_once()
        kwargs = mock_cls.call_args.kwargs
        assert kwargs["model"] == "Kimi-K2.6"
        assert kwargs["base_url"] == "http://127.0.0.1:34891/v1"
        assert kwargs["max_tokens"] == 2048
        assert isinstance(mgr, VisionManager)


def test_vision_fallback_unknown_api_compat_skips_with_diagnostic(tmp_path):
    """Fallback with an unhandled api_compat skips and names api_compat in the reason."""
    agent = make_custom_agent(tmp_path, api_compat="gemini")
    result = setup(agent, provider="custom", api_key="sk-test")
    assert result is None
    agent.add_tool.assert_not_called()
    log_kwargs = agent._log.call_args.kwargs
    assert log_kwargs["capability"] == "vision"
    assert log_kwargs["requested_provider"] == "custom"
    assert "gemini" in log_kwargs["reason"]
    assert "api_compat" in log_kwargs["reason"]


def test_minimax_vision_setup_filters_inherited_api_compat(tmp_path):
    """MiniMax vision should ignore LLM transport kwargs inherited from presets.

    Regression: presets.expand_inherit copies api_compat from the main LLM into
    `vision: {provider: inherit}`. MiniMaxVisionService accepts api_host, not
    api_compat, so setup must filter provider-specific kwargs before factory
    construction.
    """
    with patch("lingtai.capabilities.vision.create_vision_service") as mock_factory:
        mock_svc = MagicMock(spec=VisionService)
        mock_factory.return_value = mock_svc

        agent = make_mock_agent(tmp_path)
        agent.service._base_url = "https://api.minimaxi.com/anthropic"
        mgr = setup(
            agent,
            provider="minimax",
            api_key="sk-test",
            api_compat="anthropic",
            base_url="https://api.minimaxi.com/anthropic",
        )

        mock_factory.assert_called_once_with(
            "minimax",
            api_key="sk-test",
            api_host="https://api.minimaxi.com",
        )
        assert isinstance(mgr, VisionManager)
