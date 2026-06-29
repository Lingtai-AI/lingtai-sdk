"""Tests for lingtai.llm.service."""

import inspect
import os

from lingtai.llm.service import LLMService


def test_context_window_stored():
    """context_window should be accepted and stored."""
    sig = inspect.signature(LLMService.__init__)
    assert "context_window" in sig.parameters


def test_adapter_base_class_has_no_multimodal_methods():
    """LLMAdapter ABC should not define multimodal convenience methods."""
    from lingtai.llm.base import LLMAdapter
    # These methods were removed — they live on individual adapters only
    for method in ("web_search", "generate_vision", "generate_image",
                   "generate_music", "text_to_speech",
                   "transcribe", "analyze_audio"):
        assert not hasattr(LLMAdapter, method), f"LLMAdapter still has {method}"


def test_llm_service_has_no_multimodal_methods():
    """LLMService should not define multimodal routing methods."""
    for method in ("web_search", "generate_vision", "make_multimodal_message",
                   "generate_image", "generate_music", "text_to_speech",
                   "transcribe", "analyze_audio"):
        assert not hasattr(LLMService, method), f"LLMService still has {method}"


def test_llm_service_has_no_provider_config():
    """LLMService should not accept provider_config parameter."""
    sig = inspect.signature(LLMService.__init__)
    assert "provider_config" not in sig.parameters


def test_no_get_context_limit():
    """get_context_limit should no longer exist — context window is caller-provided."""
    import lingtai.llm.service as mod
    assert not hasattr(mod, "get_context_limit")
    assert not hasattr(mod, "CONTEXT_WINDOWS")
    assert not hasattr(mod, "DEFAULT_CONTEXT_WINDOW")


# ---------------------------------------------------------------------------
# build_provider_defaults_from_manifest_llm
#
# Regression: Lingtai-AI/lingtai#112 Bug A — cli.py and agent.py constructed
# `per_provider` inline and silently dropped `api_compat`, causing custom
# anthropic-compat proxies to be routed through OpenAIAdapter and crash on
# raw.choices access. The helper exists so the two call sites stay in sync.
# ---------------------------------------------------------------------------

from lingtai.llm.service import build_provider_defaults_from_manifest_llm


def test_build_provider_defaults_propagates_api_compat():
    """The whole point: api_compat from manifest.llm reaches the bucket."""
    out = build_provider_defaults_from_manifest_llm(
        {"provider": "custom", "api_compat": "anthropic", "model": "GLM-5.1"},
        max_rpm=60,
    )
    assert out == {"custom": {"max_rpm": 60, "api_compat": "anthropic"}}


def test_build_provider_defaults_returns_none_when_nothing_set():
    """Preserve historical: empty defaults pass through as None, not {}."""
    out = build_provider_defaults_from_manifest_llm(
        {"provider": "openai", "model": "gpt-5"},
        max_rpm=0,
    )
    assert out is None


def test_build_provider_defaults_includes_default_headers():
    out = build_provider_defaults_from_manifest_llm(
        {
            "provider": "openai",
            "model": "gpt-5",
            "default_headers": {"X-Foo": "bar"},
        },
        max_rpm=60,
    )
    assert out == {
        "openai": {"max_rpm": 60, "default_headers": {"X-Foo": "bar"}},
    }


def test_build_provider_defaults_lowercases_provider_key():
    """Bucket key must match the lowercased lookup used by the adapter factory."""
    out = build_provider_defaults_from_manifest_llm(
        {"provider": "Custom", "api_compat": "anthropic", "model": "GLM-5.1"},
        max_rpm=0,
    )
    assert out == {"custom": {"api_compat": "anthropic"}}


def test_build_provider_defaults_skips_none_api_compat():
    """Don't pollute the bucket with explicit Nones."""
    out = build_provider_defaults_from_manifest_llm(
        {"provider": "openai", "model": "gpt-5", "api_compat": None},
        max_rpm=60,
    )
    assert out == {"openai": {"max_rpm": 60}}


# ---------------------------------------------------------------------------
# Effective api_key memory (daemon credential inheritance, Lingtai-AI/lingtai)
#
# A parent agent on a preset/custom endpoint resolves its api_key from a
# *noncanonical* env slot (e.g. provider=custom with api_key_env=LLM_API_KEY)
# and passes it directly to LLMService(api_key=...). The default key_resolver
# only ever reads the canonical {PROVIDER}_API_KEY (CUSTOM_API_KEY), so an
# inheriting caller (the no-preset daemon path) that re-derives the key via
# parent_service._key_resolver(provider) loses it. LLMService must therefore
# remember the direct key handed to its boot adapter.
# ---------------------------------------------------------------------------


def _register_recording_adapter(provider: str):
    """Register a hermetic adapter that records its construction kwargs.

    Returns the list the factory appends a (kwargs) dict to on each build, so
    a test can assert on what api_key/base_url the boot adapter received
    without touching real provider wiring.
    """
    calls: list[dict] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return object()  # opaque adapter — service never calls into it here

    LLMService.register_adapter(provider, _factory)
    return calls


def test_direct_api_key_remembered_as_effective_key(monkeypatch):
    """A directly-supplied api_key is remembered, even from a noncanonical env.

    Mirrors a parent on provider=custom whose key came from LLM_API_KEY: the
    canonical CUSTOM_API_KEY is absent, the resolver would return None, but the
    service was handed a real key and must expose it for daemon inheritance.
    """
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    _register_recording_adapter("custom")

    svc = LLMService(
        provider="custom",
        model="glm-5.1",
        api_key="sk-from-noncanonical-LLM_API_KEY",
        base_url="https://proxy.example/v1",
        key_resolver=lambda p: os.environ.get(f"{p.upper()}_API_KEY"),
    )

    assert svc.api_key == "sk-from-noncanonical-LLM_API_KEY"
    # base_url/provider/model unchanged by this fix.
    assert svc.provider == "custom"
    assert svc.model == "glm-5.1"
    assert svc._base_url == "https://proxy.example/v1"


def test_api_key_property_does_not_call_resolver_when_no_direct_key(monkeypatch):
    """The api_key property is direct-only; daemon falls back to resolver itself."""

    _register_recording_adapter("custom")
    calls: list[str] = []

    def resolver(provider: str) -> str:
        calls.append(provider)
        return "sk-canonical"

    svc = LLMService(
        provider="custom",
        model="glm-5.1",
        key_resolver=resolver,
    )

    assert svc.api_key is None
    assert calls == []


def test_direct_api_key_reaches_boot_adapter_unchanged(monkeypatch):
    """Adapter-creation semantics intact: the boot adapter still gets the key."""
    monkeypatch.delenv("CUSTOM_API_KEY", raising=False)
    calls = _register_recording_adapter("custom")

    LLMService(
        provider="custom",
        model="glm-5.1",
        api_key="sk-direct",
        base_url="https://proxy.example/v1",
    )

    assert len(calls) == 1
    assert calls[0]["api_key"] == "sk-direct"
    assert calls[0]["base_url"] == "https://proxy.example/v1"
