# src/lingtai/llm/openai/

OpenAI adapter — wraps the `openai` SDK for Chat Completions and Responses APIs, with Codex OAuth variant.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 3 | Re-exports `OpenAIAdapter`, `OpenAIChatSession` |
| `adapter.py` | ~1880 | 5 classes + helpers: `OpenAIChatSession`, `OpenAIResponsesSession`, `OpenAIAdapter`, `CodexResponsesSession`, `CodexOpenAIAdapter` |
| `defaults.py` | 7 | `DEFAULTS` dict: `api_compat="openai"`, `use_responses_api=True` |

### adapter.py class map

| Class | Lines | Role |
|-------|-------|------|
| `OpenAIChatSession` | 492–1003 | Chat Completions session with context overflow auto-recovery; sends optional `prompt_cache_key` |
| `OpenAIResponsesSession` | 1006–1204 | Responses API session with server-side `previous_response_id` chaining, optional `context_management` compaction, and optional `prompt_cache_key` |
| `OpenAIAdapter` | 1207–1520 | `LLMAdapter` implementation; dispatches to Completions or Responses path; receives injected `compact_threshold`; derives the default `prompt_cache_key` via `_default_prompt_cache_key` / `_resolve_prompt_cache_key` |
| `CodexResponsesSession` | ~1594–1985 | Stateless Responses variant for ChatGPT-OAuth `/backend-api/codex`. **Normalizes** its three affinity inputs at construction into ONE *current* id (priority `prompt_cache_key` > `session_id` > `thread_id`) so `prompt_cache_key == session-id == thread-id` always. Sends `session-id` / `thread-id` REST headers (`_cache_affinity_headers`) only when a per-agent identity was supplied (a lone cache key stays body-only — header carve-out). Copies the actually-used (single) current value into `UsageMetadata.extra` (`_usage_extra`) for `token_ledger.jsonl`. Holds a rolling 8-deep cache-hit queue (`_record_cache_hit`); on 8 byte-identical positive `cached_tokens` it **rotates the current id in place** (`_rotate_current_affinity` — re-derives `_codex_affinity_id(anchor, time_fn())`, persistent, no revert) and emits a `codex_cache_affinity_rotated` event (Jason's final #406 semantics — only two rotate triggers: start/refresh and 8-call corruption) |
| `CodexOpenAIAdapter` | ~1990–2160 | Adapter variant that builds `CodexResponsesSession`; epoch-stamps the per-agent current id from the anchor + build epoch (`_codex_affinity_id(anchor, codex_epoch)`; explicit `codex_session_id` used verbatim, no stamping); `_resolve_codex_ids` returns `(id, id)` — thread tracks session — and `(None, None)` only for a bare/test adapter; `_default_prompt_cache_key` returns the SAME per-agent value (falls back to `lingtai-codex:{model}:v1` only on the bare/no-anchor path); passes `affinity_anchor` + `time_fn` down so the session can re-derive on a rotate; `_build_codex_event_sink` derives `logs/events.jsonl` (sibling of the `init.json` anchor) and passes an `event_sink` into the session (`None` on the bare/test path) |

### adapter.py helpers

| Function | Lines | Role |
|----------|-------|------|
| `_base_url_namespace()` | 102–116 | Stable namespace token for an OpenAI-compatible `base_url` (URL host, or short hash fallback) used in the default `prompt_cache_key` |
| `_codex_session_id()` | ~80–94 | Derive the stable 8-char Codex cache-affinity id (issue #378): `sha256(anchor).hexdigest()[:8]`, lowercase hex, where `anchor` MUST be a per-agent identity (the resolved `init.json` path). The same value is used byte-identically for `session-id`, `thread-id`, and the default `prompt_cache_key` on the root/main path |
| `_codex_affinity_id()` | ~120–133 | Derive the epoch-stamped **current** 8-char affinity id: `sha256(f"{anchor}:epoch:{int(epoch)}").hexdigest()[:8]`. Same helper for both rotate triggers — the start/refresh build epoch and the 8-call-corruption rotate epoch (`time_fn()`). A different epoch (a rebuild or a rotate) yields a different id; sub-second jitter is ignored |
| `_validate_compact_threshold()` | 69–83 | Validates/normalizes OpenAI Responses auto-compaction threshold; positive `int` or explicit `None` (disable) only |
| `_codex_responses_trace_path()` / `_codex_responses_trace_record()` | 63–157 | Opt-in Codex Responses stream diagnostic trace helpers; safe metadata only, default off |
| `_build_http_timeout()` | 159–173 | `httpx.Timeout` per-phase caps (connect≤30s, read≤60s, pool=10s) |
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
- **Subclass hooks** — `_session_class` (line 1215) for Completions path; `_adapter_extra_body()` (line 1446) for provider-specific `extra_body`; `_default_prompt_cache_key()` (line 1265) for the provider-namespaced cache key.

## Composition

### Two session paths

The adapter forks at `create_chat()` (line 1295):
1. **Responses API** (`_create_responses_session`, line 1335) — when `use_responses=True` AND (`base_url` is None OR `force_responses=True`). Builds `OpenAIResponsesSession`.
2. **Chat Completions** (`_create_completions_session`, line 1387) — fallback for compatible providers. Builds `self._session_class` (subclass-overridable).

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
3. Force `stream=True`, `store=False`; omit `previous_response_id`; send the current `prompt_cache_key`
4. After success: record assistant response into interface for next replay

### Prompt cache key (`prompt_cache_key`)

**Default-on for every OpenAI-compatible path.** Both `OpenAIChatSession` and `OpenAIResponsesSession` accept an optional `prompt_cache_key` and, when set, add it to the request kwargs on all send paths (Chat Completions `send` / `send_stream`; Responses `send` / `send_stream`; Codex `send_stream`). A bare directly-constructed session leaves it `None` (opt-in) — the *adapter* supplies the namespaced default:

- `OpenAIAdapter._default_prompt_cache_key(model)` derives the namespace from identity: official OpenAI (no `base_url`) → `lingtai-openai:{model}:v1`; any custom/compatible `base_url` → `lingtai-openai-compat:{host}:{model}:v1` (host from `_base_url_namespace`, hash fallback). Distinct endpoints/models never share a cache slot.
- Provider subclasses with a fixed identity override it: DeepSeek → `lingtai-deepseek:{model}:v1`, Zhipu/GLM → `lingtai-zhipu:{model}:v1`, MiMo → `lingtai-mimo:{model}:v1`. **Codex is special:** on the normal/root path `_default_prompt_cache_key` returns the SAME 8-char agent-path hash as `session-id`/`thread-id` (all three byte-identical); it falls back to `lingtai-codex:{model}:v1` only on the bare/no-anchor path. The compat probe (`reports/prompt-cache-key-openai-compat-probe-*.json`) confirmed DeepSeek/Zhipu/MiMo Chat Completions accept the field.
- `_resolve_prompt_cache_key(model)` applies the adapter's policy from the constructor kwarg `prompt_cache_key`: `None` (default) → auto-derive; an explicit string → override for every session; `False` → disable (never sent). Both `_create_completions_session` and `_create_responses_session` (and the Codex variant) pass `_resolve_prompt_cache_key(model)` into the session.

`prompt_cache_retention` is deliberately never sent — Codex rejects it (`Unsupported parameter`) and the whole OpenAI-compatible surface is kept uniform — and no Anthropic-style `cache_control` is emitted (Codex rejects `Unknown parameter`). MiniMax is Anthropic-compatible in this repo and is unaffected.

### Codex REST cache-affinity ids (`session-id` / `thread-id` / `prompt_cache_key`)

**Codex-only (Jason's final #406 semantics).** The three cache-affinity values are a **session-enforced invariant** — never independent:

```
prompt_cache_key == session-id == thread-id == <current affinity id>
```

`CodexResponsesSession.__init__` **normalizes** whatever candidates it receives (`prompt_cache_key`, `session_id`, `thread_id`) into one *current* id and uses it byte-identically for all three. Priority is `prompt_cache_key` > `session_id` > `thread_id` (the explicit request-body cache-affinity key wins). This closes the leak where an explicit `prompt_cache_key` override or a directly-constructed session could send three different values.

**The current id has exactly TWO rotation triggers and nothing else** (no per-`api_call_id`, no per-send, no molt, no clear):

1. **start/refresh** — the adapter is (re)built, stamping a fresh epoch; the current id is `_codex_affinity_id(anchor, epoch)` and is stable for the life of that adapter/session instance.
2. **8-call cache corruption** — the backend returns the SAME positive `cached_tokens` for `_CODEX_CACHE_AFFINITY_ROTATE_QUEUE_LEN` (= 8) requests in a row; the session re-derives a fresh current id (same helper, new epoch) and keeps using it for ALL subsequent requests (persistent, no revert).

The REST `/backend-api/codex/responses` endpoint does **not** accept `previous_response_id` (`Unsupported parameter`), so the cache-affinity lever — like the official Codex client — is stable HTTP headers (`session-id` / `thread-id`), sent via the SDK's per-request `extra_headers` (never request-body fields), plus the request-body `prompt_cache_key`.

- On the normal/root path the current id is `_codex_affinity_id(anchor, epoch) = sha256(f"{anchor}:epoch:{int(epoch)}").hexdigest()[:8]` (lowercase hex), where `anchor` is the per-agent durable identity (the resolved `init.json` path) and `epoch` is the adapter build time (injectable as `codex_epoch` / `time_fn`). It does **not** use the latest `api_call_id`, molt time, or generated UUIDs. Within one adapter instance it is stable across requests; a refresh/rebuild (new adapter, new epoch) changes it, and so does an 8-call corruption rotate — those are the only two triggers.
- **Header carve-out (NON-NEGOTIABLE).** `session-id` / `thread-id` route the backend cache slot and MUST be per-agent. Headers are emitted **only when an explicit `session_id`/`thread_id` was supplied** (`has_header_identity`); a *lone* `prompt_cache_key` — the model-only fallback `lingtai-codex:{model}:v1`, shared by every agent on a model — stays a **body-only** cache key and promotes **no** headers. Promoting it would collapse all agents onto one session/thread, which is exactly the bug the per-agent design exists to avoid. So `_cache_affinity_headers()` emits headers iff the session was given header identity; a bare/test session with no ids sends neither. The adapter has no per-agent identity of its own, so the host wiring passes the agent path down by default (see below).
- **Default wiring (the normal path — not opt-in, not opt-out).** For a Codex agent, `service.build_provider_defaults_from_manifest_llm(llm, ..., working_dir=...)` injects `codex_session_anchor = str((working_dir / "init.json").resolve())` (the agent path / durable identity anchor). The adapter epoch-stamps the current id from it and uses it for all three values via `_resolve_codex_ids` (returns `(id, id)` — the thread tracks the session id exactly) and `_default_prompt_cache_key` (returns the same `id`). The default wiring does **not** read the token ledger or molt time, so the same `working_dir` yields the same defaults for the same build epoch.
- The `codex_session_id` / `codex_session_anchor` / `codex_thread_salt` keys remain settable on the manifest `llm` block (allowlisted in `../service.py` `_PROVIDER_DEFAULTS_PASS_THROUGH_KEYS`) as an **internal override / testing escape hatch** — an explicit `codex_session_id` is used verbatim for all three (no epoch stamping); `codex_thread_salt` survives as a legacy pass-through but no longer derives a separate thread id. `_resolve_codex_ids(model)` returns `(None, None)` only when no anchor/id was passed down at all (the bare/test path).
- **Token-ledger dump.** `CodexResponsesSession._usage_extra` copies the **actually-used** `session-id` / `thread-id` / `prompt_cache_key` for the request into `UsageMetadata.extra` as `codex_session_id` / `codex_thread_id` / `codex_prompt_cache_key`. Because of the normalization above these three are the **same value** (the current id, or the rotated id after an 8-call corruption) — the ledger never records mismatched affinity ids. `BaseAgent._save_chat_history()` merges all non-None usage extra fields into `logs/token_ledger.jsonl` (`base_agent/__init__.py:1654`), so the ids sit beside input/output/thinking/cached token counts. Surfacing the cache key makes an 8-call-corruption rotation (below) visible in the ledger. The values are short, non-secret derived affinity ids — no request body, messages, or OAuth secret ride along. A body-only/bare session contributes only the fields whose levers were actually sent.

### Codex 8-call cache-corruption rotate (`_record_cache_hit` / `_rotate_current_affinity`)

**Codex-only (Jason's final #406 semantics).** A *stalled/corrupted* cache slot is one where the backend keeps returning the **same** `cached_tokens` request after request — the cached prefix is no longer growing, so the current affinity id is buying nothing. `CodexResponsesSession` detects this and rotates the current id in place (the second of the two rotate triggers; the first is start/refresh):

- `_record_cache_hit(cached_tokens, model=...)` runs after each `response.completed`. It appends **only positive** `cached_tokens` (a `0` is a miss, not a stalled hit) to a rolling queue of length `_CODEX_CACHE_AFFINITY_ROTATE_QUEUE_LEN` (= 8). When the queue is full and every value is byte-identical (`len(set(...)) == 1`), it calls `_rotate_current_affinity` and clears the queue (a fresh run of eight is required to re-fire).
- `_rotate_current_affinity` re-derives the current id from the **per-agent anchor + a fresh epoch**: `_codex_affinity_id(affinity_anchor, time_fn())` (the same helper as the start/refresh id, so a rotate is just a re-stamp at a new epoch). The clock is injectable (`time_fn`, defaults to `time.time`). The new id **replaces** `_current_id` / `_prompt_cache_key` / (when header-identity) `_session_id` / `_thread_id` and is used for ALL subsequent requests — **persistent, no revert**. A bare/test session with no `affinity_anchor` does not rotate (no per-agent identity to re-derive from); it just resets the window.
- `_effective_affinity()` is consulted when building each request and simply returns the current `(prompt_cache_key, headers)` — there is no one-shot/temporary state to consume.
- On a rotate the session emits a `codex_cache_affinity_rotated` event via an injected `event_sink`. `CodexOpenAIAdapter._build_codex_event_sink` wires the sink to the agent's `logs/events.jsonl` (a sibling of the `init.json` anchor; `None` on the bare/test path, so nothing is written). The event carries only safe metadata: `type`, `ts`, `provider`, `model`, `new_id_hash` (the rotated current id), `recent_cached_values` (the 8 stalled numbers), `had_stable_id` (whether the session carried a per-agent header identity before the rotate), and `reason: "stalled_cache_hits"`. It deliberately does **not** include the prior id, the anchor path, any prompt/message body, token totals, or OAuth secret. Sink/write failures are swallowed (`logger.warning`) — observability never breaks a turn. The start/refresh rotation does not emit an event (it is the ordinary construction path).

## State

- **`OpenAIChatSession._interface`** — canonical `ChatInterface`, single source of truth. Mutated in-place: `add_user_message`, `add_tool_results`, `add_assistant_message`, `drop_trailing`.
- **`OpenAIChatSession._request_timeout`** — per-request HTTP timeout set by caller before dispatch (line 319). Prevents race between watchdog and SDK.
- **`OpenAIResponsesSession._response_id`** — server-side session chain pointer. Updated after each `send()` / streamed response.
- **`CodexResponsesSession._response_id`** — transient debug aid only; never threaded into next request (line 1538).
- **`CodexResponsesSession._current_id` / `._cache_hit_queue`** — cache-affinity rotate state. `_current_id` is the current epoch-stamped affinity id used byte-identically for all three levers; `_cache_hit_queue` is a rolling window of the last 8 positive `cached_tokens`. When the window goes byte-identical, `_rotate_current_affinity` re-derives `_current_id` (and `_prompt_cache_key` / `_session_id` / `_thread_id`) from `_affinity_anchor` + a fresh `_time_fn()` epoch — persistent, no revert — and clears the window. `_affinity_anchor` (per-agent durable identity for re-derivation), `_event_sink` (rotate-event callable → `logs/events.jsonl`), and `_time_fn` (injectable clock) are set at construction.
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

- `_session_class` (line 1215) — override to inject provider-specific session behavior on the CC path.
- `_adapter_extra_body()` (line 1446) — override to add `extra_body` JSON fields (e.g. OpenRouter `reasoning: {include: true}`).
- `_default_prompt_cache_key(model)` (line 1265) — override to give a provider a clean cache namespace (DeepSeek/Zhipu/MiMo/Codex do).

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
