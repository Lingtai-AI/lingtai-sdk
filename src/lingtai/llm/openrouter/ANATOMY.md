---
related_files:
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/openai/adapter.py
  - src/lingtai/llm/openrouter/__init__.py
  - src/lingtai/llm/openrouter/adapter.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# src/lingtai/llm/openrouter

OpenRouter adapter — thin OpenAI-compat shim pinned to `openrouter.ai/api/v1`, opts out of reasoning text.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 0 | Empty |
| `adapter.py` | 48 | `OpenRouterAdapter(OpenAIAdapter)` — one override |

### Class

- **`OpenRouterAdapter(OpenAIAdapter)`** — `adapter.py:25` — fixed base URL + reasoning opt-out.

## Connections

- **Inherits**: `OpenAIAdapter` from `../openai/adapter.py` (1443 LOC).
- **No new imports**: Only `openai` SDK (inherited).
- **No `defaults.py`**: Not registered via config pattern — invoked directly or via `custom` factory with `api_compat="openai"`.

## Composition

### LLMAdapter ABC overrides

| Method | Line | Notes |
|--------|------|-------|
| `__init__` | 28 | Calls `super().__init__()` with `base_url=base_url or _OPENROUTER_BASE_URL` |
| `_adapter_extra_body` | 45 | Returns `{"reasoning": {"include": False}}` — tells OpenRouter to omit reasoning text from responses |

All other methods (`create_chat`, `generate`, `make_tool_result_message`, `is_quota_error`, `send`, `send_stream`) are **inherited unchanged** from `OpenAIAdapter` / `OpenAIChatSession`.

### `_adapter_extra_body` hook (`adapter.py:45-48`)

Parent `OpenAIAdapter` calls `self._adapter_extra_body()` to merge provider-specific fields into API request body. OpenRouter uses this to send `reasoning: {include: false}` — reasoning tokens are still billed, but the text is excluded from the response to save bandwidth. Set `include: True` if reasoning text is needed for logging.

## State

No additional state beyond what `OpenAIAdapter` provides.

## Notes

- **48 LOC total** — one of the thinnest adapters.
- **Same pattern as DeepSeek**: subclass `OpenAIAdapter`, override `__init__` for base URL, optionally override `_adapter_extra_body`.
- **Reasoning exclusion**: Unlike DeepSeek (which needs `reasoning_content` round-trip), OpenRouter explicitly opts **out** of reasoning text. The OpenAI response parser already reads both `reasoning_content` and `reasoning` field names if present.
- Git history: 2 commits.
