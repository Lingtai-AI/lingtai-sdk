"""Register all built-in LLM adapter factories with LLMService.

Each factory uses lazy imports so provider SDKs are only loaded when first used.
Each factory receives (model, defaults, **kw) from _create_adapter() and maps
to the adapter's actual constructor signature.
"""
from __future__ import annotations

# Official Codex REST endpoint. Used as the default ``base_url`` for the
# ``codex`` provider when the manifest/provider-defaults do not configure one.
# A configured ``base_url`` (the generic provider convention) overrides it so a
# future local ``lingtai-codex-pool`` endpoint can front the same provider
# without a separate adapter.
CODEX_OFFICIAL_BASE_URL = "https://chatgpt.com/backend-api/codex"


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
        # Honor a host-configured Responses-API compaction threshold. Absent
        # from defaults -> let OpenAIAdapter's 100k constructor default stand;
        # explicit None -> disable Responses context_management.
        adapter_kw = {k: v for k, v in kw.items() if v is not None}
        if defaults and "compact_threshold" in defaults:
            # Preserve explicit None after the general None-pruning pass above.
            adapter_kw["compact_threshold"] = defaults["compact_threshold"]
        return OpenAIAdapter(**adapter_kw)

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
        # Honor an explicitly configured endpoint (manifest ``base_url`` /
        # ``provider_defaults['base_url']``, already resolved into ``base_url``
        # by LLMService._create_adapter) so a future local ``lingtai-codex-pool``
        # can front this provider. Absent/blank -> the official Codex endpoint.
        # The pool routes later off the unchanged ``prompt_cache_key`` /
        # ``session_id`` / ``thread_id`` identity emitted below; the OAuth bearer
        # may reach localhost in that use case, which is acceptable here.
        configured_base_url = kw.pop("base_url", None)
        codex_base_url = (
            configured_base_url.strip()
            if isinstance(configured_base_url, str) and configured_base_url.strip()
            else CODEX_OFFICIAL_BASE_URL
        )
        # Per-agent Codex REST cache-affinity header config (issue #378). The
        # host wiring (service.build_provider_defaults_from_manifest_llm) passes
        # down the agent path as ``codex_session_anchor`` by default; the adapter
        # hashes it to one 8-char value used byte-identically for session_id,
        # thread_id, and prompt_cache_key, so a normal Codex agent sends stable
        # per-agent headers. ``codex_thread_salt`` is forwarded only as a legacy
        # pass-through (it no longer derives a separate thread). The adapter has
        # no per-agent identity of its own; absent these keys (e.g. a bare
        # service built in a test) it sends no session/thread headers.
        d = defaults or {}
        codex_id_kw: dict = {}
        for cfg_key in ("codex_session_id", "codex_session_anchor", "codex_thread_salt"):
            val = d.get(cfg_key)
            if val is not None:
                codex_id_kw[cfg_key] = val
        # Optional Codex-only endpoint POOL (molt-boundary shuffle). When
        # ``codex_base_urls`` carries 2+ valid endpoints, the adapter chooses one
        # at request time by (stable per-agent offset + current ``molt_count``
        # from ``<working_dir>/.agent.json``); the choice is stable within a molt
        # segment and rotates only at a molt boundary. Empty/blank -> single
        # ``base_url`` behavior above (PR #495). ``codex_molt_count`` is an
        # explicit override (tests/hosts) used instead of reading ``.agent.json``.
        # Neither affects the ``session_id`` / ``thread_id`` / ``prompt_cache_key``
        # identity the pool routes off.
        for cfg_key in ("codex_base_urls", "codex_molt_count"):
            val = d.get(cfg_key)
            if val is not None:
                codex_id_kw[cfg_key] = val
        # Per-agent Codex OAuth token file (true multiple Codex accounts). When a
        # manifest/preset sets ``codex_auth_path`` to a non-empty path, read that
        # token file instead of the shared default ``~/.lingtai-tui/codex-auth.json``.
        # Blank/whitespace is treated as omitted -> legacy default-path behavior.
        # The path is non-secret; token contents are never logged.
        auth_path = d.get("codex_auth_path")
        mgr_kw: dict = {}
        if isinstance(auth_path, str) and auth_path.strip():
            mgr_kw["token_path"] = auth_path
        mgr = CodexTokenManager(**mgr_kw)
        adapter = CodexOpenAIAdapter(
            api_key=mgr.get_access_token(),
            base_url=codex_base_url,
            use_responses=True,
            force_responses=True,
            # The user's own ChatGPT account id (sent as the ``ChatGPT-Account-ID``
            # header when present). Read from their OAuth auth data; ``None`` when
            # unavailable. Does NOT impersonate the official Codex CLI.
            codex_account_id=mgr.get_account_id(),
            **codex_id_kw,
        )
        # Store the token manager so we can refresh before each API call.
        # The openai SDK's client.api_key is mutable — we update it in-place.
        adapter._codex_token_mgr = mgr
        def _refresh_codex_auth():
            # Keep the access token current (refreshes on disk if near expiry) and
            # re-read the account id so a refresh that changes it stays current on
            # sessions built afterwards. No token/account value is logged.
            adapter._client.api_key = mgr.get_access_token()
            adapter.codex_account_id = mgr.get_account_id()
        _orig_create_chat = adapter.create_chat
        def _refreshing_create_chat(*a, **kwa):
            _refresh_codex_auth()
            return _orig_create_chat(*a, **kwa)
        adapter.create_chat = _refreshing_create_chat
        _orig_generate = adapter.generate
        def _refreshing_generate(*a, **kwa):
            _refresh_codex_auth()
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

    def _mimo(*, model=None, defaults=None, **kw):
        from .mimo.adapter import MimoAdapter
        kw.pop("model", None)
        return MimoAdapter(**{k: v for k, v in kw.items() if v is not None})

    LLMService.register_adapter("mimo", _mimo)

    def _claude_agent_sdk(*, model=None, defaults=None, **kw):
        # Experimental clean-room provider. The Claude Agent SDK authenticates
        # through the local Claude CLI login (no per-request API key), so the
        # env-resolved key and base_url are ignored.
        from .claude_agent_sdk.adapter import ClaudeAgentSDKAdapter
        kw.pop("api_key", None)
        kw.pop("base_url", None)
        adapter_kw: dict = {}
        if model:
            adapter_kw["model"] = model
        if kw.get("max_rpm"):
            adapter_kw["max_rpm"] = kw["max_rpm"]
        return ClaudeAgentSDKAdapter(**adapter_kw)

    for name in ("claude-agent-sdk", "claude_agent_sdk"):
        LLMService.register_adapter(name, _claude_agent_sdk)

    # Providers routed through the generic custom adapter
    for name in ("grok", "qwen", "kimi"):
        LLMService.register_adapter(name, _custom)
