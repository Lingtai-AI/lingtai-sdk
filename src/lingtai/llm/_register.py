"""Register all built-in LLM adapter factories with LLMService.

Each factory uses lazy imports so provider SDKs are only loaded when first used.
Each factory receives (model, defaults, **kw) from _create_adapter() and maps
to the adapter's actual constructor signature.
"""
from __future__ import annotations


def register_all_adapters() -> None:
    from lingtai.llm.service import LLMService

    def _gemini(*, model=None, defaults=None, api_key=None, max_rpm=0, **_kw):
        from .gemini.adapter import GeminiAdapter
        kw: dict = {}
        if api_key is not None: kw["api_key"] = api_key
        if max_rpm > 0: kw["max_rpm"] = max_rpm
        if model: kw["default_model"] = model
        return GeminiAdapter(**kw)

    def _anthropic(*, model=None, defaults=None, **kw):
        from .anthropic.adapter import AnthropicAdapter
        kw.pop("model", None)
        return AnthropicAdapter(**{k: v for k, v in kw.items() if v is not None})

    def _openai(*, model=None, defaults=None, **kw):
        from .openai.adapter import OpenAIAdapter
        kw.pop("model", None)
        return OpenAIAdapter(**{k: v for k, v in kw.items() if v is not None})

    def _minimax(*, model=None, defaults=None, **kw):
        from .minimax.adapter import MiniMaxAdapter
        kw.pop("model", None)
        return MiniMaxAdapter(**{k: v for k, v in kw.items() if v is not None})

    def _openrouter(*, model=None, defaults=None, **kw):
        from .openrouter.adapter import OpenRouterAdapter
        kw.pop("model", None)
        return OpenRouterAdapter(**{k: v for k, v in kw.items() if v is not None})

    def _custom(*, model=None, defaults=None, **kw):
        from .custom.adapter import create_custom_adapter
        kw.pop("model", None)
        compat = defaults.get("api_compat", "openai") if defaults else "openai"
        return create_custom_adapter(api_compat=compat, **{k: v for k, v in kw.items() if v is not None})

    LLMService.register_adapter("gemini", _gemini)
    LLMService.register_adapter("anthropic", _anthropic)
    LLMService.register_adapter("openai", _openai)
    LLMService.register_adapter("minimax", _minimax)
    LLMService.register_adapter("openrouter", _openrouter)
    LLMService.register_adapter("custom", _custom)

    def _codex(*, model=None, defaults=None, **kw):
        from .openai.adapter import CodexOpenAIAdapter
        from lingtai.auth.codex import CodexTokenManager
        kw.pop("model", None)
        kw.pop("api_key", None)  # ignore env-resolved key
        kw.pop("base_url", None)  # we set our own
        mgr = CodexTokenManager()
        adapter = CodexOpenAIAdapter(
            api_key=mgr.get_access_token(),
            base_url="https://chatgpt.com/backend-api/codex",
            use_responses=True,
            force_responses=True,
        )
        # Store the token manager so we can refresh before each API call.
        # The openai SDK's client.api_key is mutable — we update it in-place.
        adapter._codex_token_mgr = mgr
        _orig_create_chat = adapter.create_chat
        def _refreshing_create_chat(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_create_chat(*a, **kwa)
        adapter.create_chat = _refreshing_create_chat
        _orig_generate = adapter.generate
        def _refreshing_generate(*a, **kwa):
            adapter._client.api_key = mgr.get_access_token()
            return _orig_generate(*a, **kwa)
        adapter.generate = _refreshing_generate
        return adapter

    LLMService.register_adapter("codex", _codex)

    def _deepseek(*, model=None, defaults=None, **kw):
        from .deepseek.adapter import DeepSeekAdapter
        kw.pop("model", None)
        return DeepSeekAdapter(**{k: v for k, v in kw.items() if v is not None})

    LLMService.register_adapter("deepseek", _deepseek)

    def _zhipu(*, model=None, defaults=None, **kw):
        from .zhipu.adapter import ZhipuAdapter
        kw.pop("model", None)
        return ZhipuAdapter(**{k: v for k, v in kw.items() if v is not None})

    for name in ("glm", "zhipu"):
        LLMService.register_adapter(name, _zhipu)

    # Providers routed through the generic custom adapter
    for name in ("grok", "qwen", "kimi", "mimo"):
        LLMService.register_adapter(name, _custom)
