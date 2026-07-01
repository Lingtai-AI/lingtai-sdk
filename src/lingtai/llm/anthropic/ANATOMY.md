---
related_files:
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/anthropic/__init__.py
  - src/lingtai/llm/anthropic/adapter.py
  - src/lingtai/llm/anthropic/defaults.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# src/lingtai/llm/anthropic

Anthropic Claude adapter — Messages API with prompt caching, tool use, and extended thinking.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 3 | Re-exports `AnthropicAdapter`, `AnthropicChatSession` |
| `adapter.py` | 832 | Adapter + session + helpers |
| `defaults.py` | 6 | `DEFAULTS` dict: `api_key_env=ANTHROPIC_API_KEY`, `model=claude-sonnet-4-20250514` |

### Classes

- **`AnthropicAdapter(LLMAdapter)`** — `adapter.py:653` — wraps `anthropic.Anthropic` SDK and supplies merged LingTai identity/version default headers.
- **`AnthropicChatSession(ChatSession)`** — `adapter.py:279` — per-session state, owns `ChatInterface`.

### Helper functions (module-level)

| Function | Line | Purpose |
|----------|------|---------|
| `_build_http_timeout` | 29 | Per-phase `httpx.Timeout` from `request_timeout` |
| `_build_tools` | 60 | `FunctionSchema` → Anthropic tool dicts (`input_schema`, not `parameters`) |
| `_build_system_with_cache` | 79 | System prompt → single text block with `cache_control: ephemeral` |
| `_build_system_batches_with_cache` | 97 | Multi-batch system prompt with per-batch breakpoints (≤3 markers, last batch un-marked) |
| `_parse_response` | 136 | Raw response → `LLMResponse`; extracts `text`, `tool_use`, `thinking` blocks |
| `_tool_result_to_dict` | 192 | `ToolResultBlock` → Anthropic `tool_result` dict |
| `_ensure_alternation` | 201 | Merge consecutive same-role messages (Anthropic requires strict alternation) |
| `_response_to_messages` | 241 | Raw response → Anthropic message dicts for history (round-trips thinking signatures) |

## Connections

- **Imports from `lingtai_kernel`**: `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall`, `UsageMetadata`, `ToolResultBlock`, `ChatInterface`, `TextBlock`, `ThinkingBlock`, `ToolCallBlock`, `StreamingAccumulator`
- **Imports from `lingtai`**: `LLMAdapter` (ABC in `llm/base.py`), `to_anthropic` converter (`llm/interface_converters.py`)
- **External**: `anthropic` SDK, `httpx`
- **Consumers**: `MiniMaxAdapter` inherits from `AnthropicAdapter`; `custom` factory delegates here for `api_compat="anthropic"`
- **`_build_tools`** at `adapter.py:60`: key shape difference — Anthropic uses `input_schema` (not OpenAI's nested `function.parameters`)

## Composition

### LLMAdapter ABC overrides (`AnthropicAdapter`)

| Method | Line | Notes |
|--------|------|-------|
| `create_chat` | 699 | Builds tools, tool_choice, thinking config; wraps in `_GatedSession` via `_wrap_with_gate` |
| `generate` | 768 | One-shot via `self._client.messages.create`; gated by `_gated_call` |
| `make_tool_result_message` | 813 | Returns canonical `ToolResultBlock` with `toolu_` prefix ID |
| `is_quota_error` | 823 | `isinstance(exc, anthropic.RateLimitError)` |

### ChatSession method overrides (`AnthropicChatSession`)

| Method | Line | Notes |
|--------|------|-------|
| `send` | 358 | Commits to `ChatInterface` → `to_anthropic()` → `_ensure_alternation()` → `client.messages.create` |
| `send_stream` | 450 | Uses `client.messages.stream()` context manager; accumulates via `StreamingAccumulator` |
| `commit_tool_results` | 587 | Delegates to `interface.add_tool_results()` |
| `update_tools` | 593 | Rebuilds `_tools` with `cache_tools=True`; syncs to interface |
| `update_system_prompt` | 601 | Single-block cached system prompt |
| `update_system_prompt_batches` | 606 | Multi-breakpoint caching; joins batches for interface |
| `reset` | 620 | Reconstructs session with fresh `anthropic.Anthropic` client from saved `_client_kwargs` |
| `context_window` | 643 | Returns stored `_context_window` |

### Provider-specific shape conversions

- **`ToolCallBlock` → Anthropic `tool_use`**: `interface_converters.py:to_anthropic()` serializes `ToolCallBlock` as `{"type": "tool_use", "id": ..., "name": ..., "input": ...}`. The `input` key is Anthropic's term for tool arguments (vs OpenAI's `arguments`).
- **`ToolResultBlock` → Anthropic `tool_result`**: `_tool_result_to_dict()` at `adapter.py:192` wraps in a `user` message with `tool_use_id` and stringified `content`.
- **System prompt**: Passed as separate `system` parameter (not a message), with `cache_control: ephemeral` markers.

### Thinking blocks

- **Extraction**: `adapter.py:153-156` — `block.type == "thinking"` → `ThinkingBlock(text=..., provider_data={"anthropic": {"signature": ...}})`.
- **Signature round-trip**: Thinking blocks stored in interface include `signature` field (required by Anthropic for replay in history). See `adapter.py:259-266`.
- **Budget resolution**: `_resolve_thinking_budget()` at `adapter.py:676` — `"high"` → 16384, `"low"`/`"medium"` → 2048. Budget sets `max_tokens = max(budget*2, budget+8192)`.

### Prompt caching

- **Single-block**: `_build_system_with_cache()` — one `cache_control: ephemeral` at end of system prompt.
- **Multi-batch**: `_build_system_batches_with_cache()` — breakpoints between mutation-tiers, final batch un-marked (volatile). Up to 3 markers total (Anthropic caps at 4 per request; 1 reserved for tools at `adapter.py:75`).
- **Tool cache**: `_build_tools(cache_tools=True)` puts `cache_control: ephemeral` on last tool.

### Streaming protocol

- `send_stream` uses `client.messages.stream(**kwargs)` context manager (`adapter.py:477`).
- Events: `content_block_start` (tool_use detected → `acc.start_tool`), `content_block_delta` (`text_delta` / `thinking_delta` / `input_json_delta`), `content_block_stop` (finishes current block).
- Final message from `stream.get_final_message()` provides authoritative usage including cache metrics.

### Authentication

- **API key only** — passed to `anthropic.Anthropic(api_key=...)` at `adapter.py:671`.
- **Base URL**: optional override for proxies (`adapter.py:669`).
- **No OAuth**.

## State

- `AnthropicAdapter`: holds `_client` (SDK instance), `_client_kwargs` (for session reset), `_gate` (rate limiter). Constructor merges LingTai HTTP identity headers under caller/provider `default_headers` before building the SDK client.
- `AnthropicChatSession`: holds `_client`, `_model`, `_system` (list of cached blocks), `_interface` (ChatInterface), `_tools`, `_tool_choice`, `_extra_kwargs`, `_client_kwargs`, `_context_window`, `_request_timeout`.

### Usage normalization (`adapter.py:163-173`)

Anthropic's `input_tokens` only counts tokens after the last cache breakpoint. We normalize to `input_tokens + cache_read + cache_write` so the rest of the system sees total prompt tokens (matching OpenAI/Gemini semantics). `cached_tokens` = `cache_read_input_tokens`.

### Error recovery

Both `send` and `send_stream` revert the interface on API error via `interface.drop_trailing(lambda e: e.role == "user")` at `adapter.py:396,530`.

## Notes

- **Strict alternation**: Anthropic rejects consecutive same-role messages. `_ensure_alternation()` at `adapter.py:201` merges them by combining content lists.
- **JSON schema enforcement**: Implemented as a synthetic tool with `tool_choice: {"type": "tool", "name": ...}` (`adapter.py:726-737`).
- **`client` property**: Escape hatch to raw SDK at `adapter.py:827`.
- **MiniMax inheritance**: `MiniMaxAdapter` subclasses `AnthropicAdapter` directly, overriding only `__init__` to set `base_url`. Inherits the pre-request hook automatically.
- **`send(None)` contract** (`f596ec1`): both `send` and `send_stream` accept `None` as the "continue from wire" signal — caller has already pre-staged the canonical interface (e.g. `BaseAgent._inject_notification_pair` spliced a synthesized `notification(action="check")` `(call, result)` pair). The input-dispatch ladder tests `if message is None: pass` first; the error-path `drop_trailing(lambda e: e.role == "user")` is guarded with `if message is not None` so an API failure during a `send(None)` cannot corrupt the pre-staged pair. Driven from `base_agent/turn.py:_handle_tc_wake`. From the LLM's viewpoint, the wake is indistinguishable from the agent voluntarily calling the tool itself.
- **Pre-request hook** (`f46b346`, dormant after notification redesign): both `send` and `send_stream` fire `self.pre_request_hook(self._interface)` after committing the message but before the API call. Historically used for mid-turn tc_inbox drain (canonical-interface regime, same-turn delivery). Post-`fadbabf`/`d2da97e` the hook still fires but the queue is always empty — ACTIVE notifications now defer to the post-turn IDLE synthetic-pair path rather than mutating tool results at send time. Phase 3 will remove the hook. See root `ANATOMY.md` "Notifications".
- Git history: 10 commits, active development on caching, timeout, rate gating, mid-turn hook, `send(None)` continue-from-wire contract.
