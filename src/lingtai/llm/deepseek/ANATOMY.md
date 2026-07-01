---
related_files:
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/deepseek/__init__.py
  - src/lingtai/llm/deepseek/adapter.py
  - src/lingtai/llm/openai/adapter.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# src/lingtai/llm/deepseek

DeepSeek adapter — thin OpenAI-compat wrapper that satisfies DeepSeek V4 thinking mode's `reasoning_content` round-trip contract.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 0 | Empty |
| `adapter.py` | 133 | `DeepSeekAdapter`, `DeepSeekChatSession`, `_fallback_reasoning_for` |

### Classes

- **`DeepSeekAdapter(OpenAIAdapter)`** — pinned to DeepSeek endpoint, sets `_session_class`; overrides `_default_prompt_cache_key` → `lingtai-deepseek:{model}:v1` (clean provider namespace for the default-on OpenAI-compatible prompt cache key; see `../openai/ANATOMY.md`).
- **`DeepSeekChatSession(OpenAIChatSession)`** — overrides only `_build_messages`. Real reasoning round-trip is handled upstream by `interface_converters.to_openai`; this subclass only fills in a fallback when an assistant turn has no captured ThinkingBlock.

### Module-level

| Symbol | Purpose |
|--------|---------|
| `_DEEPSEEK_BASE_URL` | `"https://api.deepseek.com"` |
| `_fallback_reasoning_for` | Per-turn-unique reasoning stub for assistant turns lacking real reasoning (inlines tool name + call ids or content snippet + turn index) |

## Connections

- **Inherits**: `OpenAIAdapter` / `OpenAIChatSession` from `../openai/adapter.py`.
- **No additional imports**: Only `openai` SDK (inherited), no new external deps.
- **Hook points used**: `_build_messages` (overridden on session), `_session_class` and `_default_prompt_cache_key` (overridden on adapter).

## Composition

### LLMAdapter ABC overrides (`DeepSeekAdapter`)

| Method | Notes |
|--------|-------|
| `__init__` | Calls `super().__init__()` with `base_url=base_url or _DEEPSEEK_BASE_URL` and forwards `default_headers` so OpenAIAdapter adds LingTai identity/version headers |
| `_session_class` | Set to `DeepSeekChatSession` (parent's `create_chat` uses this) |

All other `LLMAdapter` methods (`create_chat`, `generate`, `make_tool_result_message`, `is_quota_error`) are **inherited unchanged** from `OpenAIAdapter`.

### ChatSession method overrides (`DeepSeekChatSession`)

| Method | Notes |
|--------|-------|
| `_build_messages` | Calls `super()._build_messages()` (which already emits real `reasoning_content` from any ThinkingBlock via `to_openai`). Then walks assistant messages: once any has `tool_calls`, all subsequent assistant messages without a non-empty `reasoning_content` get `_fallback_reasoning_for(msg, turn_idx)` filled in. |

`send` and `send_stream` are no longer overridden — there's no placeholder-echo to strip.

### `reasoning_content` round-trip (issue #9 fix)

Real reasoning is preserved end-to-end:

1. **Capture** — `OpenAIChatSession._record_assistant_response` (non-streaming) and the streaming finalize path read `message.reasoning_content` (or `.reasoning` for OpenRouter) and append a `ThinkingBlock` to the assistant interface entry.
2. **Replay** — `interface_converters.to_openai` emits the ThinkingBlock text back as `reasoning_content` on the wire.
3. **Fallback** — `DeepSeekChatSession._build_messages` fills in `_fallback_reasoning_for` only on assistant turns that lack real reasoning (typically: rehydrated history from before this fix, or turns where the provider returned no reasoning text). The fallback inlines tool names + call ids (or content snippet for plain-text post-tool turns) plus a turn index, which is byte-different per turn by construction.

### Why per-turn-unique matters

DeepSeek V4 has a cache fast-path that, on heavy cache hits, can collapse onto repeating substrings in the prompt. Before this fix, the kernel injected a byte-identical placeholder (`"(reasoning omitted — not preserved across turns) [turn=N]"`) on every replayed assistant turn — only the integer changed. At N=30 trials per cell on a real 200K-cached fixture, this produced **40-47% empty responses** on `deepseek-v4-pro` and `-flash` (HTTP 200 with content="" and tool_calls=null). The empty signature was sharp: `output_tokens ∈ {15, 30, 45, 105}` — exactly k× the placeholder's tokenization, indicating the model was generating copies of the placeholder string.

Replacing the placeholder with anything per-turn-byte-unique drives the empty rate to **0/30** on both models. Real reasoning is byte-different per turn by construction, which is why preserving it (rather than forging a substitute) is the structurally correct fix.

### Thinking blocks

- **Extraction**: `OpenAIChatSession._record_assistant_response` reads `message.reasoning_content` and appends `ThinkingBlock(text=...)` to the assistant entry. Streaming path captures via `acc.add_thought` from `delta.reasoning` / `delta.reasoning_content` and persists at finalize.
- **Replay**: `interface_converters.to_openai` joins `ThinkingBlock.text` from the assistant entry and emits it as `reasoning_content` on the wire.

### Authentication

- **API key only** — inherited from `OpenAIAdapter` (`openai.OpenAI(api_key=...)`).
- **Base URL**: defaults to `https://api.deepseek.com`, overridable.

## State

- `DeepSeekAdapter`: inherits `_client`, `_gate`, `_session_class`.
- `DeepSeekChatSession`: inherits all from `OpenAIChatSession`.

## Notes

- **Minimal footprint**: ~130 LOC, ~25 lines of unique logic.
- **No `defaults.py`**: DeepSeek adapter is not registered via the `DEFAULTS` config pattern — likely invoked directly by `LLMService`.
- **Pre-fix history compatibility**: rehydrated chat_history.jsonl entries that lack ThinkingBlocks (written before this fix shipped) get the per-turn-unique fallback automatically; no migration needed.
- Git history: 4 commits on the original placeholder approach (afc7ddc → a9382dc → 23599ca → 86c2a3d), then issue-#9 rewrite to preserve real reasoning end-to-end.
