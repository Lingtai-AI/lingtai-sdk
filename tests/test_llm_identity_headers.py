from __future__ import annotations

from lingtai.llm.identity_headers import merge_lingtai_identity_headers


def test_identity_header_merge_preserves_caller_headers(monkeypatch):
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    headers = merge_lingtai_identity_headers(
        {"user-agent": "Caller/1", "X-LingTai-Version": "caller-version", "X-Test": "1"}
    )

    assert headers["user-agent"] == "Caller/1"
    assert headers["X-LingTai-Version"] == "caller-version"
    assert headers["X-Test"] == "1"
    assert "User-Agent" not in headers
    assert headers["X-LingTai-Client"] == "LingTai"


def test_identity_header_merge_can_skip_user_agent(monkeypatch):
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    headers = merge_lingtai_identity_headers(user_agent=False)

    assert "User-Agent" not in headers
    assert headers["X-LingTai-Client"] == "LingTai"
    assert headers["X-LingTai-Version"] == "9.8.7"


def test_openai_adapter_builds_client_with_identity_headers(monkeypatch):
    from lingtai.llm.openai import adapter as mod

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mod.openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    mod.OpenAIAdapter(api_key="sk-test", default_headers={"X-Test": "1"})

    headers = captured["default_headers"]
    assert headers["User-Agent"] == "LingTai/9.8.7"
    assert headers["X-LingTai-Client"] == "LingTai"
    assert headers["X-LingTai-Version"] == "9.8.7"
    assert headers["X-Test"] == "1"


def test_anthropic_adapter_builds_client_with_identity_headers(monkeypatch):
    from lingtai.llm.anthropic import adapter as mod

    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mod.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    mod.AnthropicAdapter(api_key="sk-test", default_headers={"X-Test": "1"})

    headers = captured["default_headers"]
    assert headers["User-Agent"] == "LingTai/9.8.7"
    assert headers["X-LingTai-Client"] == "LingTai"
    assert headers["X-LingTai-Version"] == "9.8.7"
    assert headers["X-Test"] == "1"


def test_gemini_adapter_builds_client_with_identity_headers(monkeypatch):
    from lingtai.llm.gemini import adapter as mod

    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mod.genai, "Client", FakeClient)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    mod.GeminiAdapter(api_key="sk-test", default_headers={"X-Test": "1"})

    headers = captured["http_options"].headers
    assert "User-Agent" not in headers
    assert headers["X-LingTai-Client"] == "LingTai"
    assert headers["X-LingTai-Version"] == "9.8.7"
    assert headers["X-Test"] == "1"


def test_openai_compatible_subclasses_forward_identity_headers(monkeypatch):
    from lingtai.llm.openai import adapter as openai_mod
    from lingtai.llm.deepseek.adapter import DeepSeekAdapter
    from lingtai.llm.mimo.adapter import MimoAdapter
    from lingtai.llm.openrouter.adapter import OpenRouterAdapter
    from lingtai.llm.zhipu.adapter import ZhipuAdapter

    captured = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(openai_mod.openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    for adapter_cls in (DeepSeekAdapter, MimoAdapter, OpenRouterAdapter, ZhipuAdapter):
        adapter_cls(api_key="sk-test", default_headers={"X-Test": "1"})
        headers = captured[-1]["default_headers"]
        assert headers["User-Agent"] == "LingTai/9.8.7"
        assert headers["X-LingTai-Client"] == "LingTai"
        assert headers["X-LingTai-Version"] == "9.8.7"
        assert headers["X-Test"] == "1"


def test_minimax_adapter_forwards_identity_headers(monkeypatch):
    from lingtai.llm.anthropic import adapter as anthropic_mod
    from lingtai.llm.minimax.adapter import MiniMaxAdapter

    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic_mod.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    MiniMaxAdapter(api_key="sk-test", default_headers={"X-Test": "1"})

    headers = captured["default_headers"]
    assert headers["User-Agent"] == "LingTai/9.8.7"
    assert headers["X-LingTai-Client"] == "LingTai"
    assert headers["X-LingTai-Version"] == "9.8.7"
    assert headers["X-Test"] == "1"


def test_custom_adapter_forwards_identity_headers_to_all_compat_paths(monkeypatch):
    from lingtai.llm.anthropic import adapter as anthropic_mod
    from lingtai.llm.custom.adapter import create_custom_adapter
    from lingtai.llm.gemini import adapter as gemini_mod
    from lingtai.llm.openai import adapter as openai_mod

    openai_captured = {}
    anthropic_captured = {}
    gemini_captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            openai_captured.update(kwargs)

    class FakeAnthropic:
        def __init__(self, **kwargs):
            anthropic_captured.update(kwargs)

    class FakeGeminiClient:
        def __init__(self, **kwargs):
            gemini_captured.update(kwargs)

    monkeypatch.setattr(openai_mod.openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(anthropic_mod.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(gemini_mod.genai, "Client", FakeGeminiClient)
    monkeypatch.setattr("lingtai.llm.identity_headers.lingtai_version", lambda: "9.8.7")

    create_custom_adapter(
        api_compat="openai",
        api_key="sk-test",
        base_url="https://example.invalid/openai",
        default_headers={"X-Test": "openai"},
    )
    assert openai_captured["default_headers"]["X-LingTai-Version"] == "9.8.7"
    assert openai_captured["default_headers"]["X-Test"] == "openai"

    create_custom_adapter(
        api_compat="anthropic",
        api_key="sk-test",
        base_url="https://example.invalid/anthropic",
        default_headers={"X-Test": "anthropic"},
    )
    assert anthropic_captured["default_headers"]["X-LingTai-Version"] == "9.8.7"
    assert anthropic_captured["default_headers"]["X-Test"] == "anthropic"

    create_custom_adapter(
        api_compat="gemini",
        api_key="sk-test",
        default_headers={"X-Test": "gemini"},
    )
    gemini_headers = gemini_captured["http_options"].headers
    assert "User-Agent" not in gemini_headers
    assert gemini_headers["X-LingTai-Client"] == "LingTai"
    assert gemini_headers["X-LingTai-Version"] == "9.8.7"
    assert gemini_headers["X-Test"] == "gemini"
