"""Tests for lingtai.llm.service."""

import inspect

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
