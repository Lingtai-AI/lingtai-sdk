---
related_files:
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/custom/__init__.py
  - src/lingtai/llm/custom/adapter.py
  - src/lingtai/llm/custom/defaults.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# src/lingtai/llm/custom

Custom adapter — factory for named provider aliases routing to OpenAI, Anthropic, or Gemini backends.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 3 | Re-exports `create_custom_adapter`, `DEFAULTS` |
| `adapter.py` | 51 | `create_custom_adapter()` factory function |
| `defaults.py` | 6 | `DEFAULTS` dict: `api_compat=openai`, `base_url=None`, `api_key_env=CUSTOM_API_KEY`, `model=""` |

## Connections

- **Delegates to**: `AnthropicAdapter`, `GeminiAdapter`, `OpenAIAdapter` — chosen by `api_compat` string.
- **Imports** are **lazy** (inside the factory function body) to avoid pulling all SDKs at import time.
- **`DEFAULTS` re-exported** from `__init__.py` for config consumers.

## Composition

### `create_custom_adapter()` — `adapter.py:20`

Factory function (not a class). Returns an `LLMAdapter` instance:

| `api_compat` | Adapter created | Requirements |
|---|---|---|
| `"gemini"` | `GeminiAdapter(api_key=..., **kwargs)` | `api_key` required, `base_url` ignored |
| `"anthropic"` | `AnthropicAdapter(api_key=..., base_url=..., **kwargs)` | `base_url` required (raises `ValueError` if missing) |
| `"openai"` (default) | `OpenAIAdapter(api_key=..., base_url=..., **kwargs)` | `base_url` required (raises `ValueError` if missing) |

Parameters:
- `api_key: str | None` — passed through to chosen adapter.
- `api_compat: str` — selects backend (`"openai"`, `"anthropic"`, `"gemini"`).
- `base_url: str | None` — provider endpoint URL.
- `default_headers: dict | None` — forwarded to all compat paths that expose SDK HTTP header configuration (OpenAI-, Anthropic-, and Gemini-compatible).
- `**kwargs` — forwarded to adapter constructor (`timeout_ms`, `max_rpm`, etc.).

### No class of its own

`custom` does **not** define its own `Adapter` or `ChatSession` subclass. The returned object is a plain instance of whichever built-in adapter matched `api_compat`.

## State

Stateless factory — no module-level mutable state (unlike `minimax.mcp_client`).

## Notes

- **Use case**: Any provider name not matching a built-in (`openai`, `anthropic`, `gemini`, `minimax`, `deepseek`, `openrouter`) routes here via config. Example configs:
  - `"bedrock"`: `{"api_compat": "anthropic", "base_url": "https://..."}`
  - `"vertex"`: `{"api_compat": "gemini"}`
  - `"openrouter"`: `{"api_compat": "openai", "base_url": "https://openrouter.ai/api/v1"}`
- **Symmetric with OpenRouter**: Both are thin shims over `OpenAIAdapter`. The difference: OpenRouter is a first-class provider with its own module and `_adapter_extra_body`; custom is a generic factory that can route to any backend.
- **`defaults.py`**: Empty model string (`""`) signals no default model — consumer must provide one.
- Git history: 3 commits.
