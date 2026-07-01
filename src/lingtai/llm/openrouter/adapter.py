"""OpenRouter adapter — first-class provider wrapping their OpenAI-compat API.

OpenRouter is an OpenAI-compatible gateway to many upstream model providers
(Anthropic, DeepSeek, GLM/Zhipu, Qwen, MiniMax, Kimi, ...). On the wire it
speaks OpenAI's /chat/completions format, so this adapter inherits
``OpenAIAdapter`` and only overrides what's OpenRouter-specific:

- Fixed base_url — no user configuration needed.
- Explicitly asks OpenRouter NOT to include reasoning text in responses
  (``reasoning: {include: false}``). Reasoning tokens are billed either
  way; the text is not useful to us, so we save the bytes. Flip
  ``include`` to True if you want the reasoning text exposed via
  ``LLMResponse.thoughts`` (for logs or side channels) — the OpenAI
  response parser already reads both ``reasoning_content`` and
  ``reasoning`` field names.
"""
from __future__ import annotations

from ..openai.adapter import OpenAIAdapter


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(OpenAIAdapter):
    """OpenAI-compat adapter pinned to OpenRouter's endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_ms: int = 300_000,
        max_rpm: int = 0,
        default_headers: dict | None = None,
    ):
        # Allow base_url override for staging / self-hosted proxies, but
        # default to the public OpenRouter endpoint.
        super().__init__(
            api_key=api_key,
            base_url=base_url or _OPENROUTER_BASE_URL,
            timeout_ms=timeout_ms,
            max_rpm=max_rpm,
            default_headers=default_headers,
        )

    def _adapter_extra_body(self) -> dict:
        # Explicit opt-out: we don't want reasoning text back. Billing is
        # unaffected.
        return {"reasoning": {"include": False}}
