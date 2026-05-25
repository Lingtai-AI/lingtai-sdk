# src/lingtai/llm/openai/

OpenAI adapter — wraps the `openai` SDK for Chat Completions and Responses APIs, with Codex OAuth variant.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 3 | Re-exports `OpenAIAdapter`, `OpenAIChatSession` |
| `adapter.py` | 1597 | 5 classes + helpers: `OpenAIChatSession`, `OpenAIResponsesSession`, `OpenAIAdapter`, `CodexResponsesSession`, `CodexOpenAIAdapter` |
| `defaults.py` | 7 | `DEFAULTS` dict: `api_compat="openai"`, `use_responses_api=True` |

### adapter.py class map

| Class | Lines | Role |
|-------|-------|------|
| `OpenAIChatSession` | 399–891 | Chat Completions session with context overflow auto-recovery |
| `OpenAIResponsesSession` | 899–1081 | Responses API session with server-side `previous_response_id` chaining |
| `OpenAIAdapter` | 1089–1352 | `LLMAdapter` implementation; dispatches to Completions or Responses path |
| `CodexResponsesSession` | 1360–1539 | Stateless Responses variant for ChatGPT-OAuth `/backend-api/codex` |
| `CodexOpenAIAdapter` | 1543–1597 | Adapter variant that builds `CodexResponsesSession` |

### adapter.py helpers

| Function | Lines | Role |
|----------|-------|------|
| `_codex_responses_trace_path()` / `_codex_responses_trace_record()` | 46–141 | Opt-in Codex Responses stream diagnostic trace helpers; safe metadata only, default off |
| `_build_http_timeout()` | 143–157 | `httpx.Timeout` per-phase caps (connect≤30s, read≤60s, pool=10s) |
| `_build_tools()` | 165–179 | `FunctionSchema` → OpenAI CC tool format (`{type, function: {name, description, parameters}}`) |
| `_build_responses_tools()` | 189–213 | `FunctionSchema` → Responses API flat format (`{type, name, description, parameters}`); scrubs disallowed top-level JSON-Schema combinators (`allOf`, `oneOf`, etc.) |
| `_parse_tool_calls()` | 216–233 | Raw SDK `tool_calls` → `list[ToolCall]` |
| `_parse_response()` | 236–280 | ChatCompletion → `LLMResponse` (extracts reasoning from `reasoning_content` or `reasoning`) |
| `_handle_responses_reasoning_event()` | 303–346 | Responses stream reasoning-summary event handler; accumulates `summary_text` deltas/done fallback without raw reasoning text |
| `_parse_responses_api_response()` | 349–391 | Responses API output → `LLMResponse` (handles `message`, `function_call`, `reasoning` output items) |

## Connections

- **Base class** — `OpenAIAdapter` extends `LLMAdapter` (`from lingtai.llm.base import LLMAdapter`, line 29).
- **Kernel types** — imports `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall`, `UsageMetadata` from `lingtai_kernel.llm.base`.
- **Interface converters** — imports `to_openai` and `to_responses_input` from `lingtai.llm.interface_converters` (line 31).
- **Streaming** — imports `StreamingAccumulator` from `lingtai_kernel.llm.streaming` (line 32).
- **HTTP client** — imports `httpx` for timeout construction (line 16); `openai` SDK for all API calls (line 17).
- **Subclass hooks** — `_session_class` (line 987) for Completions path; `_adapter_extra_body()` (line 1173) for provider-specific `extra_body`.

## Composition

### Two session paths

The adapter forks at `create_chat()` (line 1015):
1. **Responses API** (`_create_responses_session`, line 1055) — when `use_responses=True` AND (`base_url` is None OR `force_responses=True`). Builds `OpenAIResponsesSession`.
2. **Chat Completions** (`_create_completions_session`, line 1115) — fallback for compatible providers. Builds `self._session_class` (subclass-overridable).

Both paths return sessions wrapped via `_wrap_with_gate()` for rate limiting.

### Chat Completions session flow (`OpenAIChatSession.send`, line 438)

1. Record user input into `ChatInterface` (str → `add_user_message`; list → `add_tool_results`)
2. `_build_kwargs()`: enforce tool pairing → serialize via `_build_messages()` → `_pair_orphan_tool_calls()` wire guard
3. `_run_with_overflow_recovery(_do_call)` — retries with context trimming on 400 context-length errors
4. On success: record assistant response into interface via `_record_assistant_response()`

### Responses API session flow (`OpenAIResponsesSession.send`, line 853)

1. `_convert_input(message)` → Responses API input items
2. Chain `previous_response_id` if available
3. Call `client.responses.create(**kwargs)`
4. Store `response_id` for next turn

### Codex stateless flow (`CodexResponsesSession.send_stream`, line 1266)

1. Record message into canonical `ChatInterface`
2. `to_responses_input(interface)` — replay full conversation as input items
3. Force `stream=True`, `store=False`; omit `previous_response_id`
4. After success: record assistant response into interface for next replay

## State

- **`OpenAIChatSession._interface`** — canonical `ChatInterface`, single source of truth. Mutated in-place: `add_user_message`, `add_tool_results`, `add_assistant_message`, `drop_trailing`.
- **`OpenAIChatSession._request_timeout`** — per-request HTTP timeout set by caller before dispatch (line 319). Prevents race between watchdog and SDK.
- **`OpenAIResponsesSession._response_id`** — server-side session chain pointer. Updated after each `send()` / streamed response.
- **`CodexResponsesSession._response_id`** — transient debug aid only; never threaded into next request (line 1538).
- **Codex Responses trace** — opt-in diagnostics write JSONL metadata to `logs/codex_responses_trace.jsonl` when `LINGTAI_CODEX_RESPONSES_TRACE=1` (override path with `LINGTAI_CODEX_RESPONSES_TRACE_PATH`). Default off; stores event/item shapes, lengths/hashes, usage, and accumulator counts, not raw content.
- **`OpenAIAdapter._client`** — shared `openai.OpenAI` instance. `_client_kwargs` stored for session `reset()`.
- **`OpenAIAdapter._session_class`** — class var, subclasses override (e.g. DeepSeek and MiMo inject `reasoning_content` round-trip fallbacks).

## Notes

### Provider-specific shape conversions

| Canonical block | Chat Completions wire | Responses API wire |
|----------------|----------------------|-------------------|
| `ToolCallBlock` | `{type: "function", id, function: {name, arguments: <json-str>}}` on assistant message `tool_calls` array | `{type: "function_call", call_id, name, arguments: <json-str>}` as top-level output item |
| `ToolResultBlock` | `{role: "tool", tool_call_id, content}` as separate message | `{type: "function_call_output", call_id, output}` as top-level input item |
| `TextBlock` | `content` string on assistant message | `{type: "output_text", text}` inside message content |
| `ThinkingBlock` | Emitted as `reasoning_content` on assistant message (DeepSeek and MiMo thinking-mode round-trip; other CC providers ignore the field). Captured back from `message.reasoning_content` / `message.reasoning` into a ThinkingBlock by `_record_assistant_response` (non-streaming) and the streaming finalize path. | Replayed as a top-level `{type: "reasoning", summary: [{type: "summary_text", text: ...}]}` item before assistant text/calls by `to_responses_input` (`../interface_converters.py:233-258`) so stateless Codex can retain summarized reasoning context. Responses streaming captures `response.reasoning_summary_text.*` into thoughts and Codex persists those thoughts as ThinkingBlocks before tool calls. |

### Context overflow auto-recovery

`OpenAIChatSession._run_with_overflow_recovery()` is inherited from `ChatSession` (`lingtai_kernel/llm/base.py:384`) and wraps any API call in a retry loop:
- Detects 400 `context_length_exceeded` via `_is_context_overflow_error()` (line 276) — checks both canonical OpenAI code and loose string heuristics for compatible vendors.
- `_trim_context_one_round()` (`lingtai_kernel/llm/base.py:303`) drops ~10% of non-system entries from the FRONT of the interface. Snaps cut point to never split `assistant[ToolCallBlock]` from `user[ToolResultBlock]`.
- Max 10 rounds (`lingtai_kernel/llm/base.py:291`). On successful recovery, injects a `[kernel]` molt notice via `_inject_overflow_notice()` (`lingtai_kernel/llm/base.py:363`).

### Wire-layer orphan guard

`_pair_orphan_tool_calls()` (line 376) scans the serialized message list for `assistant.tool_calls` without matching `role=tool` messages. Synthesizes placeholder tool results with `[synthesized placeholder — real result was not in context at send time]`. Logs warnings for investigation. Does NOT mutate canonical interface.

The Codex / Responses path has the same invariant: `to_responses_input` ends with `_pair_responses_orphan_function_calls` (`interface_converters.py:184-227`) which appends a synthesized `function_call_output` for any `function_call` without a matching output anywhere in the items list. Same placeholder string, same non-mutating semantics. Without this guard the provider returns `400 No tool output found for function call …` when a continuation request is built from a half-committed tool loop (issue #170).

### Streaming

- **CC streaming** (`send_stream`, line 622) — `stream=True, stream_options={include_usage: True}`. Uses `StreamingAccumulator` for text + tool deltas. Reasoning deltas captured from `delta.reasoning` or `delta.reasoning_content` (OpenRouter compatibility, lines 726-733). Overflow recovery wraps the stream open + first chunk (lines 668-690).
- **Responses streaming** (`send_stream`, line 891) — event types: `response.reasoning_summary_text.delta/done` (summary thoughts only), `response.output_text.delta`, `response.function_call_arguments.delta`, `response.output_item.added/done`, `response.completed`.
- **Codex streaming** — forces `stream=True` even on `send()` (line 1372). Full interface replay per request; captured summary thoughts are persisted as ThinkingBlocks so `to_responses_input` replays reasoning items before function calls. Optional diagnostics (`LINGTAI_CODEX_RESPONSES_TRACE=1`) append safe per-event metadata to `logs/codex_responses_trace.jsonl` without changing accumulator/persistence behavior.

### Authentication paths

- **Standard** — `api_key` passed to `openai.OpenAI(api_key=...)` at construction (line 1009).
- **Codex OAuth** — `CodexOpenAIAdapter` built by `../_register.py:54` with `CodexTokenManager.get_access_token()`. Token refreshed by monkey-patching `create_chat` and `generate` to update `adapter._client.api_key` in-place before each call.

### Tool schema conversion

- **CC path** — `_build_tools()` (line 59): `{type: "function", function: {name, description, parameters}}`.
- **Responses path** — `_build_responses_tools()` (line 83): `{type: "function", name, description, parameters}` (flat). Scrubs top-level JSON-Schema combinators (`_RESPONSES_DISALLOWED_TOP_LEVEL`, line 80) that the Responses API rejects.

### Reasoning extraction

- **CC non-streaming** (`_parse_response`, line 130) — checks `message.reasoning_content` (OpenAI native) then `message.reasoning` (OpenRouter).
- **CC streaming** — `delta.reasoning` or `delta.reasoning_content` accumulated via `acc.add_thought()` (line 733).
- **Responses non-streaming** — `reasoning` output items with `summary_text` blocks (lines 256-259).
- **Responses streaming** — `response.reasoning_summary_text.delta/done` and reasoning output-item summaries are captured as summary thoughts; raw `response.reasoning_text.*` is intentionally not persisted by default.

### Subclass hooks

- `_session_class` (line 987) — override to inject provider-specific session behavior on the CC path.
- `_adapter_extra_body()` (line 1173) — override to add `extra_body` JSON fields (e.g. OpenRouter `reasoning: {include: true}`).

### `send(None)` contract — continue from wire

All four `send` / `send_stream` paths in this file accept `None` as the "the caller has already staged the canonical interface; just talk to the LLM" signal. This is what `base_agent/turn.py:_handle_tc_wake` calls when `_sync_notifications` has spliced a synthesized `(ToolCallBlock, ToolResultBlock)` pair into the wire — from the LLM's viewpoint the agent appears to have voluntarily called `system(action="notification")` and is now responding to the result, no fake user message and no meta prefix.

Implementation: the input-dispatch ladder at the top of each method tests `if message is None: pass` first, then the existing `str` / `list` branches. The error-path `drop_trailing(lambda e: e.role == "user")` is guarded — `if message is not None: drop_trailing(...)` — so an API failure during a `send(None)` does not corrupt the pre-staged notification pair. `OpenAIResponsesSession._convert_input(None)` returns `[]` so the existing `previous_response_id` chain continues with no new input items; `CodexResponsesSession.send_stream` simply skips the append branch since it replays the full canonical interface on every request anyway.

### Pre-request hook (mid-turn tc_inbox drain — dormant)

All four `send` / `send_stream` paths in this file fire `self.pre_request_hook(self._interface)` after committing the message to the canonical interface but before the API call. Historically the kernel installed `BaseAgent._drain_tc_inbox_for_hook` here so involuntary tool-call pairs (mail notifications, soul.flow voices) spliced into the wire chat mid-turn. After the `.notification/` redesign (`fadbabf`/`d2da97e`) the hook is still installed but the queue is always empty in production; ACTIVE notifications now defer to the post-turn IDLE synthetic-pair path rather than mutating tool results at send time. Phase 3 will remove the hook entirely. Three regimes (preserved for historical context and future re-use):

- **`OpenAIChatSession.send` / `send_stream`** — canonical-interface; the hook splices into the same interface that's about to be serialized via `_build_messages()`. Spliced pair appears in this same API request. Same-turn delivery.
- **`OpenAIResponsesSession.send` / `send_stream`** — server-state via `previous_response_id`; the hook splices into `self._interface` but the wire payload comes from `_convert_input(message)` (just the new input). Spliced pair is recorded in canonical interface immediately for persistence/inspection but only reaches the LLM next turn after re-sync. Documented inline.
- **`CodexResponsesSession.send_stream`** — Codex's stateless backend replays the full canonical interface on every request (`to_responses_input(self._interface)`), so the hook delivers same-turn just like the CC path.

### Git history

16 commits. Key: context overflow recovery (`f65e395`), orphan tool_call guard (`8197fdc`), Codex stateless path (`7e88f47`, `a4bf117`), per-phase HTTP timeout caps (`81b95e2`), `cached_tokens` None coercion (`1e715ab`), `_build_messages` hook refactor (`70c0357`), pre-request hook for mid-turn tc_inbox drain (`f46b346`, now dormant), `send(None)` continue-from-wire contract (`f596ec1`).
