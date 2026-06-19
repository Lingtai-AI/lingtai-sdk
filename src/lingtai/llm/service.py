"""LLMService — concrete implementation of the kernel ABC.

Adapter-based LLM access: adapter registry, session management,
and one-shot generation.

Decoupled from any app-specific config system:
- API key resolution via injected ``key_resolver`` callable (defaults to env vars)
- Provider defaults via injected ``provider_defaults`` dict
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from typing import Any

from lingtai_kernel.llm.base import (
    ChatSession,
    FunctionSchema,
    LLMResponse,
)
from lingtai_kernel.llm.interface import ChatInterface, ToolResultBlock
from lingtai_kernel.llm.service import LLMService as LLMServiceABC

from .base import LLMAdapter


def _generate_session_id() -> str:
    """Generate a unique lingtai session ID."""
    return f"st_{uuid.uuid4().hex[:12]}"


def _generate_tool_call_id() -> str:
    """Generate a LingTai-issued tool-call correlation id.

    Format: ``tc_<unix_seconds>_<4-hex>``. Stamped onto every tool-result
    dict by ``LLMService.make_tool_result`` so the agent has a stable,
    provider-agnostic id for each tool call <-> result pair.

    Distinct from the *provider*-issued id (Anthropic ``tool_use_id`` /
    OpenAI ``tool_call_id``) which still flows through the wire protocol
    via the ``tool_call_id`` kwarg — that id is for the LLM's API server,
    this one is for the agent's reasoning layer.
    """
    import time
    return f"tc_{int(time.time())}_{uuid.uuid4().hex[:4]}"


# Fields from manifest.llm that adapter factories may consult via
# LLMService._provider_defaults. Keep this list opt-in (rather than
# splatting the whole manifest.llm dict) so the surface area between
# init.json and adapter construction stays auditable.
#
# api_compat in particular MUST propagate: the custom-provider factory
# (lingtai/llm/_register.py:_custom) dispatches between OpenAI/Anthropic/
# Gemini wire protocols based on it. Dropping it silently routes
# api_compat="anthropic" custom providers (e.g. local GLM proxies) to
# OpenAIAdapter, which then explodes on raw.choices access. See
# Lingtai-AI/lingtai#112 for the full failure trace.
_PROVIDER_DEFAULTS_PASS_THROUGH_KEYS = ("api_compat",)
_PROVIDER_DEFAULTS_PRESERVE_NONE_KEYS = ("compact_threshold",)


def build_provider_defaults_from_manifest_llm(
    llm: dict,
    *,
    max_rpm: int,
) -> dict | None:
    """Convert a manifest.llm block into LLMService.provider_defaults.

    Returns ``{provider_name: defaults_dict}`` (scoped to the agent's
    configured provider so other providers stay unaffected), or ``None``
    when no fields are set — preserving the historical behavior where
    callers passed ``provider_defaults=None`` for the unconfigured case.
    """
    provider_key = llm["provider"].lower()
    per_provider: dict = {}
    if max_rpm > 0:
        per_provider["max_rpm"] = max_rpm
    user_headers = llm.get("default_headers")
    if isinstance(user_headers, dict) and user_headers:
        # Pass-through; LLMService._default_headers_for honors caller-supplied
        # headers and fills only the gaps with provider policy.
        per_provider["default_headers"] = dict(user_headers)
    for key in _PROVIDER_DEFAULTS_PASS_THROUGH_KEYS:
        value = llm.get(key)
        if value is not None:
            per_provider[key] = value
    for key in _PROVIDER_DEFAULTS_PRESERVE_NONE_KEYS:
        if key in llm:
            per_provider[key] = llm[key]
    return {provider_key: per_provider} if per_provider else None


class LLMService(LLMServiceABC):
    """Concrete LLM service — adapter registry, session management, generation.

    Responsibilities:
    - Adapter factory: constructs adapters via class-level registry
    - Session registry: assigns lingtai session IDs, tracks active sessions
    - One-shot gateway: routes generate() through the same tracking path
    - Token accounting: centralizes per-session usage tracking via interface

    Does NOT:
    - Wrap ChatSession.send() — backend calls that directly
    - Handle fallback/retry — errors surface to the backend
    - Add business logic — pure delegation + bookkeeping

    Decoupling parameters:
    - ``key_resolver``: callable(provider) -> api_key | None.
      Defaults to reading ``{PROVIDER}_API_KEY`` from the environment.
    - ``provider_defaults``: dict mapping provider name to defaults dict
      (model, base_url, api_compat, etc.).  Defaults to empty dict.
    """

    _adapter_registry: dict[str, Callable[..., LLMAdapter]] = {}

    @classmethod
    def register_adapter(cls, name: str, factory: Callable[..., LLMAdapter]) -> None:
        """Register an adapter factory by provider name.

        The factory receives keyword arguments: model, defaults, api_key,
        base_url, max_rpm.
        """
        cls._adapter_registry[name.lower()] = factory

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        key_resolver: Callable[[str], str | None] | None = None,
        provider_defaults: dict | None = None,
        context_window: int = 1_000_000,
    ) -> None:
        self._provider = provider.lower()
        self._model = model
        self._context_window = context_window
        self._base_url = base_url
        self._key_resolver = key_resolver or (lambda p: os.environ.get(f"{p.upper()}_API_KEY"))
        self._provider_defaults = provider_defaults or {}
        self._adapters: dict[tuple[str, str | None], LLMAdapter] = {}
        self._adapter_lock = threading.Lock()
        self._adapters[(self._provider, base_url)] = self._create_adapter(self._provider, api_key, base_url)
        self._sessions: dict[str, ChatSession] = {}

    def _create_adapter(self, provider: str, api_key: str | None, base_url: str | None) -> LLMAdapter:
        key_kw: dict = {"api_key": api_key} if api_key is not None else {}
        defaults = self._get_provider_defaults(provider)
        effective_url = base_url or (defaults.get("base_url") if defaults else None)
        url_kw: dict = {"base_url": effective_url} if effective_url is not None else {}
        max_rpm = defaults.get("max_rpm", 0) if defaults else 0
        rpm_kw: dict = {"max_rpm": max_rpm} if max_rpm > 0 else {}

        # Provider-specific default headers (e.g. Kimi requires honest UA per ToS).
        headers_kw: dict = {}
        default_headers = self._default_headers_for(provider, defaults)
        if default_headers:
            headers_kw["default_headers"] = default_headers

        p = provider.lower()
        factory = self._adapter_registry.get(p)
        if factory is None:
            raise RuntimeError(
                f"No adapter registered for provider {provider!r}. "
                f"Registered: {', '.join(sorted(self._adapter_registry)) or '(none)'}. "
                f"If using lingtai, ensure 'import lingtai' runs before creating LLMService."
            )

        return factory(
            model=self._model,
            defaults=defaults,
            **key_kw, **url_kw, **rpm_kw, **headers_kw,
        )

    def _default_headers_for(self, provider: str, defaults: dict | None) -> dict | None:
        """Return provider-specific default HTTP headers, if any.

        Caller-supplied headers in *defaults* (under the ``default_headers``
        key) win; provider-policy defaults only fill in what the caller did
        not specify. For Kimi we set ``User-Agent`` to honestly identify
        ourselves — Kimi's ToS forbids spoofing other coding tools'
        User-Agents, and accounts can be suspended for it.
        """
        caller_headers: dict = {}
        if defaults and isinstance(defaults.get("default_headers"), dict):
            caller_headers = dict(defaults["default_headers"])

        if provider.lower() == "kimi" and "User-Agent" not in caller_headers:
            caller_headers["User-Agent"] = "LingTai-Agent/1.0"

        return caller_headers or None

    # --- Adapter cache ---

    def get_adapter(self, provider: str, base_url: str | None = None) -> LLMAdapter:
        """Return cached adapter for *provider* + *base_url*, creating one on demand.

        The cache is keyed by ``(provider, base_url)`` so the same provider
        with different base URLs (e.g. OpenRouter vs local vLLM) gets separate
        adapter instances.

        Raises RuntimeError if the API key for *provider* is not configured.
        """
        provider = provider.lower()
        cache_key = (provider, base_url)

        # Fast path — no lock needed for reads of an already-cached adapter
        if cache_key in self._adapters:
            return self._adapters[cache_key]
        # When no base_url specified, find any cached adapter for this provider
        if base_url is None:
            for (p, _url), adapter in self._adapters.items():
                if p == provider:
                    return adapter

        # Slow path — lock to prevent duplicate adapter creation
        with self._adapter_lock:
            # Double-check after acquiring lock
            if cache_key in self._adapters:
                return self._adapters[cache_key]
            if base_url is None:
                for (p, _url), adapter in self._adapters.items():
                    if p == provider:
                        return adapter

            # Need to create a new adapter — check API key first
            api_key = self._key_resolver(provider)
            if api_key is None:
                raise RuntimeError(
                    f"API key for provider {provider!r} is not configured. "
                    f"Set the appropriate environment variable or .env entry."
                )

            # For on-demand adapters without explicit base_url, check provider defaults
            effective_base_url = base_url
            if effective_base_url is None:
                defaults = self._get_provider_defaults(provider)
                effective_base_url = defaults.get("base_url") if defaults else None
            adapter = self._create_adapter(provider, api_key, effective_base_url)
            self._adapters[cache_key] = adapter
            return adapter

    def _get_provider_defaults(self, provider_name: str) -> dict | None:
        """Get defaults for a provider from the injected provider_defaults dict."""
        return self._provider_defaults.get(provider_name)

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    # --- Session management ---

    def create_session(
        self,
        system_prompt: str,
        tools: list[FunctionSchema] | None = None,
        *,
        model: str | None = None,
        thinking: str = "default",
        agent_type: str = "",
        tracked: bool = True,
        interaction_id: str | None = None,
        json_schema: dict | None = None,
        force_tool_call: bool = False,
        provider: str | None = None,
        interface: ChatInterface | None = None,
        context_window: int | None = None,
    ) -> ChatSession:
        """Start a new multi-turn conversation.

        Returns a ChatSession with a .session_id assigned.
        If *interface* is provided, restores an existing conversation history.

        Args:
            context_window: Override the service-level context window for this
                session.  Falls back to the value passed at LLMService construction.
        """
        adapter = self.get_adapter(provider) if provider else self.get_adapter(self._provider, self._base_url)
        session_model = model or self._model
        ctx_window = context_window or self._context_window
        chat = adapter.create_chat(
            model=session_model,
            system_prompt=system_prompt,
            tools=tools,
            thinking=thinking,
            interaction_id=interaction_id,
            json_schema=json_schema,
            force_tool_call=force_tool_call,
            interface=interface,
            context_window=ctx_window,
        )
        if tracked:
            chat.session_id = _generate_session_id()
            chat._agent_type = agent_type
            chat._tracked = True
            self._sessions[chat.session_id] = chat
        else:
            chat.session_id = ""
            chat._tracked = False
        return chat

    def get_session(self, session_id: str) -> ChatSession | None:
        """Look up an active session by ID."""
        return self._sessions.get(session_id)

    # --- One-shot generation ---

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        max_output_tokens: int | None = None,
        provider: str | None = None,
    ) -> LLMResponse:
        """Single-turn generation."""
        adapter = self.get_adapter(provider) if provider else self.get_adapter(self._provider, self._base_url)
        gen_model = model or self._model
        response = adapter.generate(
            model=gen_model,
            contents=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            json_schema=json_schema,
            max_output_tokens=max_output_tokens,
        )
        return response

    # --- Tool results ---

    def make_tool_result(
        self, tool_name: str, result: dict, *, tool_call_id: str | None = None,
        provider: str | None = None,
    ) -> ToolResultBlock:
        """Build a canonical ToolResultBlock.

        Stamps a LingTai-issued ``_tool_call_id`` onto the result dict so the
        agent sees a uniform correlation id regardless of which provider
        underneath issued the wire-protocol id. The provider's id flows
        through the ``tool_call_id`` kwarg untouched — it is what the LLM's
        API server uses for tool_use <-> tool_result pairing on the wire.
        """
        if isinstance(result, dict):
            result["_tool_call_id"] = _generate_tool_call_id()
        adapter = self.get_adapter(provider) if provider else self.get_adapter(self._provider, self._base_url)
        return adapter.make_tool_result_message(
            tool_name, result, tool_call_id=tool_call_id,
        )
