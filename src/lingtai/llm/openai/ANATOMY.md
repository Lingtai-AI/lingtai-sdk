---
related_files:
  - docs/references/codex-http-anatomy-investigation.md
  - src/lingtai/auth/ANATOMY.md
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/_register.py
  - src/lingtai/llm/interface_converters.py
  - src/lingtai/llm/identity_headers.py
  - src/lingtai/llm/openai/__init__.py
  - src/lingtai/llm/openai/adapter.py
  - src/lingtai/llm/openai/codex_ws.py
  - src/lingtai/llm/openai/defaults.py
  - src/lingtai/llm/service.py
  - tests/test_codex_prompt_cache_key.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
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
| `CodexResponsesSession` | `adapter.py:2223` | Responses session for ChatGPT-backed Codex running the `full`/`incremental` additive continuation state machine over a selectable transport (REST default, WebSocket opt-in): `store=false` always, encrypted reasoning include/replay, encrypted-reasoning self-heal, cache-affinity headers, honest client/account identity, and honest Codex metadata envelope. |
| `CodexOpenAIAdapter` | `adapter.py:2017` | Codex provider specialization: forces Responses mode, derives stable per-agent cache/session ids plus a LingTai installation id from the configured anchor, and wires account/metadata hints into Codex sessions. |

### adapter.py helpers

| Function | Lines | Role |
|----------|-------|------|
| `_base_url_namespace()` | 102–116 | Stable namespace token for an OpenAI-compatible `base_url` (URL host, or short hash fallback) used in the default `prompt_cache_key` |
| `_codex_session_id()` | ~106–122 | Derive the 8-char Codex cache-affinity id (issue #378): `sha256(f"{anchor}\0{molt_count}").hexdigest()[:8]`, lowercase hex, where `anchor` MUST be a per-agent identity (the resolved `init.json` path) and `molt_count` is the agent's current molt count. The same value is used byte-identically for `session_id`, `thread_id`, and the default `prompt_cache_key` on the root/main path. No time/epoch — stable across restarts WITHIN a molt segment, and intentionally changes at each molt boundary |
| `_codex_installation_id()` | `adapter.py:154` | Derives a UUID-shaped, non-secret LingTai installation id for Codex `client_metadata` from the same local anchor/id; never reuses `~/.codex/installation_id`. |
| `_codex_identity_headers()` | ~259–268 | Builds Codex client identity headers (`originator`, `User-Agent`): default requests identify as LingTai via the shared `llm/identity_headers.py` User-Agent helper, while the official Codex CLI-shaped identity remains an explicit local diagnostic opt-in. | `adapter.py:259`, `adapter.py:268`, `identity_headers.py:20` |
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

### Codex continuation flow (`CodexResponsesSession.send_stream`)

Two orthogonal axes drive each Codex turn:

- **Transfer mode** — `full` vs `incremental` (the strict additive
  `previous_response_id` state machine, `_codex_plan_continuation`). Transport-independent.
- **Transport** — `rest` (normal runtime, hardcoded) vs `websocket`. Selects how
  the planned request is sent, never *whether* incremental continuation is used.

**Transport selection:** REST is **hardcoded** for normal runtime
(`_CODEX_TRANSPORT_DEFAULT = "rest"`). There is intentionally **no environment
variable** that selects the transport — an inherited `LINGTAI_CODEX_WS` or
`LINGTAI_CODEX_TRANSPORT` is ignored and cannot flip the runtime to WebSocket
(live testing confirmed REST prompt-prefix caching is sufficient). The WebSocket
transport code is retained for tests / internal / future use and is reachable
**only** via an explicit `transport="websocket"` (or legacy `ws_enabled=True`)
constructor kwarg.

Flow:

1. Record message into canonical `ChatInterface`.
2. `_frozen_responses_input(interface)` — the full conversation as input items
   (with per-`call_id` output freezing for stable replay).
3. Run the shared `_codex_plan_continuation` planner: first turn / prefix mismatch
   / epoch reset → **full**; strict additive continuation → **incremental**.
4. Send via the selected transport:
   - REST `client.responses.create(...)` (default): send the full converted input
     in both modes. REST `incremental` means unchanged prefix/cache epoch, not
     wire delta, and never sends `previous_response_id`.
   - WebSocket (`codex_ws`): `full` sends the full input; `incremental` sends the
     strict-additive delta plus `previous_response_id`.
5. If Codex rejects a full replay with `The encrypted content for item ... could
   not be verified`, treat it as stale raw reasoning state: `_strip_codex_encrypted_reasoning_items`
   (`adapter.py:3242-3286`) removes replay-only `encrypted_content` anchors from
   `ThinkingBlock.provider_data`, `_reset_ws_epoch` (`adapter.py:2856-2885`) clears
   stale baselines/response-id state, and the adapter retries the same visible
   transcript once as `rest_full_self_heal` / `stateless_full_self_heal`
   (`adapter.py:3556-3640`). Summary text, assistant text, and tool calls remain
   in the canonical interface; only the opaque provider blob is dropped.
6. After success: record the assistant response into the interface and recompute
   the converter-stable delta baseline (`_ws_record_baseline_from_interface`) so the
   next turn can strict-prefix-match and stay incremental.
**Usage metadata axes:** `UsageMetadata.extra` carries `codex_transport`
(`rest`/`websocket`), `codex_transfer_mode` (`full`/`incremental`), and the
transport-qualified `codex_request_mode` (`rest_full` / `rest_incremental` /
`ws_full` / `ws_incremental` / `rest_full_fallback` / `rest_full_self_heal` / `stateless_full_self_heal`), plus the safe delta-decision
diagnostic (`codex_ws_delta_reason` and counts — never prompt/secret content). The
WS-named `codex_ws_*` diagnostic keys are reused on REST (transport-neutral
metadata); `codex_request_mode` never reads `ws_*` on a REST request.

### Prompt cache key (`prompt_cache_key`)

**Default-on for every OpenAI-compatible path.** Both `OpenAIChatSession` and `OpenAIResponsesSession` accept an optional `prompt_cache_key` and, when set, add it to the request kwargs on all send paths (Chat Completions `send` / `send_stream`; Responses `send` / `send_stream`; Codex `send_stream`). A bare directly-constructed session leaves it `None` (opt-in) — the *adapter* supplies the namespaced default:

- `OpenAIAdapter._default_prompt_cache_key(model)` derives the namespace from identity: official OpenAI (no `base_url`) → `lingtai-openai:{model}:v1`; any custom/compatible `base_url` → `lingtai-openai-compat:{host}:{model}:v1` (host from `_base_url_namespace`, hash fallback). Distinct endpoints/models never share a cache slot.
- Provider subclasses with a fixed identity override it: DeepSeek → `lingtai-deepseek:{model}:v1`, Zhipu/GLM → `lingtai-zhipu:{model}:v1`, MiMo → `lingtai-mimo:{model}:v1`. **Codex is special:** on the normal/root path `_default_prompt_cache_key` returns the SAME 8-char (agent-path, molt-count) hash as the `session_id`/`thread_id` cache-affinity headers (underscore keys, matching the Codex backend literally — a hyphenated spelling loses cache affinity; all three byte-identical); it falls back to `lingtai-codex:{model}:v1` only on the bare/no-anchor path. The compat probe (`reports/prompt-cache-key-openai-compat-probe-*.json`) confirmed DeepSeek/Zhipu/MiMo Chat Completions accept the field.
- `_resolve_prompt_cache_key(model)` applies the adapter's policy from the constructor kwarg `prompt_cache_key`: `None` (default) → auto-derive; an explicit string → override for every session; `False` → disable (never sent). Both `_create_completions_session` and `_create_responses_session` (and the Codex variant) pass `_resolve_prompt_cache_key(model)` into the session.

`prompt_cache_retention` is deliberately never sent — Codex rejects it (`Unsupported parameter`) and the whole OpenAI-compatible surface is kept uniform — and no Anthropic-style `cache_control` is emitted (Codex rejects `Unknown parameter`). MiniMax is Anthropic-compatible in this repo and is unaffected.

### Codex REST cache-affinity ids (`session_id` / `thread_id` / `prompt_cache_key`)

**Codex-only.** The three cache-affinity values are a **single per-agent id**, byte-identical, stable within a molt segment and refreshed at each molt boundary:

```
prompt_cache_key == session_id == thread_id == <per-agent (anchor, molt) id>
```

`CodexResponsesSession.__init__` **normalizes** whatever candidates it receives (`prompt_cache_key`, `session_id`, `thread_id`) into one id and uses it byte-identically for all three. Priority is `prompt_cache_key` > `session_id` > `thread_id` (the explicit request-body cache-affinity key wins). This closes the leak where explicit request-body/cache-affinity inputs or a directly-constructed session could send three different values.

**The id is a deterministic hash of the agent path AND the current molt count — no time, no epoch, no rotation within a molt, no operator override.** It is `_codex_session_id(anchor, molt_count) = sha256(f"{anchor}\0{molt_count}").hexdigest()[:8]`, where `anchor` is the per-agent durable identity (the resolved `init.json` path) and `molt_count` is read live from `<working_dir>/.agent.json`. Within a molt segment the same agent yields the same id across restarts / refresh / clear, so it keeps routing to the **same sticky-warm backend cache slot**; at each molt boundary the id intentionally changes so the molt starts on a fresh slot. **Molt does NOT rebuild the adapter**, so the id is (re)derived at request time from the live `molt_count` — never cached once at construction (`_resolve_codex_ids` / `_default_prompt_cache_key` both call `_current_codex_id()`, which reads `_current_molt_count()` afresh). (Empirically the backend routes the prompt cache to a replica off a stable session id; churning it *within* a molt re-rolls the routing and discards the warm slot — so the only intentional id change is the molt boundary. Earlier designs epoch-stamped the id on rebuild and rotated it on "stalled cache" dips, and a now-removed operator-level fixed `codex_session_id` override could pin the id; all were removed as counterproductive, along with the `codex-cache-key` request header.)

Stable HTTP headers (`session_id` / `thread_id`, underscore spelling to match Codex CLI, sent via the SDK's per-request `extra_headers` — never request-body fields) plus the request-body `prompt_cache_key` are the cache-affinity lever and are independent of the transfer mode. They ride on **every** request regardless of full vs incremental.

> **REST incremental caveat.** REST `incremental` is not a wire-delta mode. It still sends the full converted context, but labels the request as an unchanged-prefix/cache-epoch continuation so diagnostics and cache-ledger logic can distinguish it from an epoch rebuild. WebSocket remains the only Codex transport that sends delta input plus `previous_response_id`.

- **Header carve-out (NON-NEGOTIABLE).** `session_id` / `thread_id` route the backend cache slot and MUST be per-agent. Headers are emitted **only when an explicit `session_id`/`thread_id` was supplied** (`has_header_identity`); a *lone* `prompt_cache_key` — the model-only fallback `lingtai-codex:{model}:v1`, shared by every agent on a model — stays a **body-only** cache key and promotes **no** headers. Promoting it would collapse all agents onto one session/thread, which is exactly the bug the per-agent design exists to avoid. So `_cache_affinity_headers()` emits headers iff the session was given header identity; a bare/test session with no ids sends neither. The adapter has no per-agent identity of its own, so the host wiring passes the agent path down by default (see below).
- **Default wiring (the normal path — not opt-in, not opt-out).** For a Codex agent, `service.build_provider_defaults_from_manifest_llm(llm, ..., working_dir=...)` injects `codex_session_anchor = str((working_dir / "init.json").resolve())` (the agent path / durable identity anchor). The adapter hashes it together with the live `molt_count` into the id and uses it for all three values via `_resolve_codex_ids` (returns `(id, id)` — the thread tracks the session id exactly) and `_default_prompt_cache_key` (returns the same `id`), both computed fresh per request through `_current_codex_id()`. The default wiring does **not** read the token ledger, molt time, or any clock — only the agent path and the current `molt_count` — so the same `(working_dir, molt_count)` always yields the same id and a molt advances it.
- The `codex_session_anchor` / `codex_thread_salt` keys remain settable on the manifest `llm` block (allowlisted in `../service.py` `_PROVIDER_DEFAULTS_PASS_THROUGH_KEYS`) as an **internal override / testing escape hatch** — `codex_session_anchor` overrides the auto-injected agent path; `codex_thread_salt` survives as a legacy pass-through but no longer derives a separate thread id. There is no operator-level fixed-id override. `_resolve_codex_ids(model)` returns `(None, None)` only when no anchor was passed down at all (the bare/test path).
- **Token-ledger dump.** `CodexResponsesSession._usage_extra` copies the `session_id` / `thread_id` / `prompt_cache_key` for the request into `UsageMetadata.extra` as `codex_session_id` / `codex_thread_id` / `codex_prompt_cache_key`. Because of the normalization above these three are the **same value** — the ledger never records mismatched affinity ids. `BaseAgent._save_chat_history()` merges all non-None usage extra fields into `logs/token_ledger.jsonl`, so the ids sit beside input/output/thinking/cached token counts. The values are short, non-secret derived affinity ids — no request body, messages, or OAuth secret ride along. A body-only/bare session contributes only the fields whose levers were actually sent.

### Codex client-identity headers (`originator` / `User-Agent`)

The default request identity is explicit LingTai: `_CODEX_ORIGINATOR = "lingtai"` and `User-Agent: LingTai/<version>`, with the version token resolved through the shared LLM HTTP identity helper (`../identity_headers.py:20`). `_CODEX_IMPERSONATE_OFFICIAL_CLI` is reserved for explicit local protocol comparisons and switches both `originator` and `User-Agent` to the official Codex CLI-shaped values only when enabled; account-selection headers remain independent of this identity choice. `adapter.py:140`, `adapter.py:153`, `adapter.py:178`, `adapter.py:214`, `tests/test_codex_prompt_cache_key.py:113`, `tests/test_codex_prompt_cache_key.py:590`

### Codex `ChatGPT-Account-ID` header (the user's own account id)

**Codex-only.** When the user's OAuth auth data supplies an account id (`CodexTokenManager.get_account_id()`, see `../../auth/ANATOMY.md`), `CodexResponsesSession` sends it as the canonical `ChatGPT-Account-ID` header so the request is attributed to the right ChatGPT account. It does NOT change the honest `originator`/`User-Agent` identity above — no Codex-CLI impersonation. The value flows `_register.py` (`codex_account_id=mgr.get_account_id()`) → `CodexOpenAIAdapter.codex_account_id` (mutable; the OAuth-refresh monkey-patch re-reads it via `get_account_id()` so a refresh that changes the id stays current on later-built sessions) → `CodexResponsesSession(account_id=...)` (`self._account_id`). Emitted only when non-empty; otherwise the header is omitted entirely. The account id is a non-secret identifier and — unlike the affinity ids — is **NOT** copied into `UsageMetadata.extra` or any log/ledger.

### Codex honest metadata envelope

See `docs/references/codex-http-anatomy-investigation.md` for the capture history, Codex CLI comparison, and the safety rationale behind which metadata LingTai does and does not send.

When a Codex session has a stable LingTai session/thread identity, `CodexResponsesSession` adds an honest metadata envelope alongside the cache-affinity headers (`adapter.py:1730`, `adapter.py:1885`, `adapter.py:1900`). Each request gets a fresh `x-client-request-id`; `x-codex-window-id` is the LingTai window id `<session_id>:0`; `x-codex-turn-metadata` is compact JSON carrying `session_id`, `thread_id`, a generated `turn_id`, a truthful LingTai `sandbox` label, and `turn_started_at_unix_ms`; and body `client_metadata.x-codex-installation-id` is carried through `extra_body` because the OpenAI Python SDK has no typed `client_metadata` argument. This is compatibility metadata, not CLI impersonation: LingTai keeps `originator: lingtai` / `User-Agent: LingTai/<version>`, does not send `x-codex-beta-features`, and derives its installation id from LingTai state rather than from the official Codex CLI installation file.

## State

- `CodexResponsesSession._installation_id` / `_metadata_sandbox`: optional honest Codex metadata state used to build `client_metadata.x-codex-installation-id` and turn metadata without leaking local paths or claiming official CLI features.

- **`OpenAIChatSession._interface`** — canonical `ChatInterface`, single source of truth. Mutated in-place: `add_user_message`, `add_tool_results`, `add_assistant_message`, `drop_trailing`.
- **`OpenAIChatSession._request_timeout`** — per-request HTTP timeout set by caller before dispatch (line 319). Prevents race between watchdog and SDK.
- **`OpenAIResponsesSession._response_id`** — server-side session chain pointer. Updated after each `send()` / streamed response.
- **`CodexResponsesSession._response_id`** — transient debug aid only; never threaded into next request (line 1538).
- **`CodexResponsesSession._current_id`** — the single per-agent affinity id (the hash of the agent path + current molt count) handed to this session, used byte-identically for `_prompt_cache_key` / `_session_id` / `_thread_id`. Set once per session at construction — a NEW session is built for each `create_chat`, and the adapter resolves the molt-current id at that point, so a molt-advanced id reaches the next session without any in-session mutation (no rotation, no epoch, no clock).
- **Codex Responses trace** — opt-in diagnostics write JSONL metadata to `logs/codex_responses_trace.jsonl` when `LINGTAI_CODEX_RESPONSES_TRACE=1` (override path with `LINGTAI_CODEX_RESPONSES_TRACE_PATH`). Default off; stores event/item shapes, lengths/hashes, usage, and accumulator counts, not raw content.
- **`OpenAIAdapter._client`** — shared `openai.OpenAI` instance. `_client_kwargs` stored for session `reset()`. Constructor passes `default_headers=merge_lingtai_identity_headers(...)` (`adapter.py:1974`), so OpenAI-compatible HTTP requests carry non-secret LingTai identity/version headers unless a caller/provider header overrides them case-insensitively.
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

The Codex / Responses path has the same invariant: `to_responses_input` ends with `_pair_responses_orphan_function_calls` (`../interface_converters.py:190-250`) which synthesizes a `function_call_output` for any `function_call` without a matching output anywhere in the items list. Same placeholder string, same non-mutating semantics. Without this guard the provider returns `400 No tool output found for function call …` when a continuation request is built from a half-committed tool loop (issue #170).

**Placeholders go at the TAIL, not interleaved.** The guard appends its synthesized placeholders as one contiguous block at the end of the items list, in `function_call` order — it does **not** insert each placeholder immediately after its call. This is a continuation-stability fix: `to_responses_input` always emits an assistant entry's `function_call`s contiguously and all real `function_call_output`s afterwards, so interleaving a placeholder right after each call made placeholder positions drift relative to where the real outputs land. A multi-call turn resolving incrementally then broke the Codex strict-prefix continuation and forced a `*_full` request every turn — the logged `prefix_mismatch` with `mismatch_prev_type=function_call_output` vs `mismatch_cur_type=function_call`. The baseline recorder reinforces this: `_ws_record_baseline_from_interface` strips **all** synthesized placeholders (not just trailing ones) from the recorded baseline via `_strip_synthesized_orphan_outputs` (`adapter.py:512-540`), so the real, position-stable items are the only load-bearing prefix and the real tool result strictly extends the baseline. Fix applies to BOTH transports (shared converter + shared baseline recorder).

### System prompt is `instructions`, frozen per session (Responses/Codex)

On the Responses/Codex path the system prompt is **not** an `input` item — it rides in the top-level `instructions` kwarg. `to_responses_input` deliberately skips system entries (`../interface_converters.py:280-281`, documented `../interface_converters.py:256-257`); the prompt is carried separately as `instructions`.

That `instructions` value is **frozen at session construction**: `OpenAIResponsesSession.__init__` captures `self._instructions = instructions` (`adapter.py:1711`) and every send replays that same frozen value (`adapter.py:1785-1786`, `adapter.py:1818-1819`; Codex `adapter.py:3367-3368`). There is no re-read from the interface.

In-flight Responses/Codex sessions therefore have a **no-op `update_system_prompt`**: the base `ChatSession.update_system_prompt` is a default no-op (`lingtai_kernel/llm/base.py:289-293`), and only `OpenAIChatSession` overrides it to mutate the interface (`adapter.py:1429-1431`). `OpenAIResponsesSession` and `CodexResponsesSession` do **not** override it, so it stays inert on those paths.

**Behavior-code contract:** a pad / system-prompt edit mid-session changes nothing on the wire — it does **not** break the current input-prefix continuation or cache, so the warm continuation is preserved. A changed system prompt takes effect only when a **new** session is constructed (molt / refresh / restart / bootstrap), since `instructions` is re-resolved from the live system prompt at that point (`_create_responses_session` / Codex create path pass `instructions=system_prompt`, e.g. `adapter.py:2063`, `adapter.py:3939`). This is the contract surfaced to the agent by `_CODEX_SYSTEM_PROMPT_UPDATE_NOTE` (`adapter.py:2237-2245`) via the static adapter comment (`system_prompt_update_note`, `adapter.py:2279`, `adapter.py:2312`).

### Streaming

- **CC streaming** (`send_stream`, line 622) — `stream=True, stream_options={include_usage: True}`. Uses `StreamingAccumulator` for text + tool deltas. Reasoning deltas captured from `delta.reasoning` or `delta.reasoning_content` (OpenRouter compatibility, lines 726-733). Overflow recovery wraps the stream open + first chunk (lines 668-690).
- **Responses streaming** (`send_stream`, line 891) — event types: `response.reasoning_summary_text.delta/done` (summary thoughts only), `response.output_text.delta`, `response.function_call_arguments.delta`, `response.output_item.added/done`, `response.completed`.
- **Codex streaming** — forces `stream=True` even on `send()`. Runs the `full`/`incremental` planner per request over the selected transport (REST default / WebSocket opt-in): REST carries the whole converted interface in both modes; WebSocket carries the whole interface for `full` and delta + `previous_response_id` for `incremental`. Captured summary thoughts and raw encrypted reasoning items are persisted as ThinkingBlocks so `to_responses_input` replays reasoning items before function calls; if Codex later rejects a raw encrypted item as unverifiable, the adapter strips only that opaque replay state and retries once with summary/plain transcript. Optional diagnostics (`LINGTAI_CODEX_RESPONSES_TRACE=1`) append safe per-event metadata to `logs/codex_responses_trace.jsonl` without changing accumulator/persistence behavior.

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

All four `send` / `send_stream` paths in this file accept `None` as the "the caller has already staged the canonical interface; just talk to the LLM" signal. This is what `base_agent/turn.py:_handle_tc_wake` calls when `_sync_notifications` has spliced a synthesized `(ToolCallBlock, ToolResultBlock)` pair into the wire — from the LLM's viewpoint the agent appears to have voluntarily called `notification(action="check")` and is now responding to the result, no fake user message and no meta prefix.

Implementation: the input-dispatch ladder at the top of each method tests `if message is None: pass` first, then the existing `str` / `list` branches. The error-path `drop_trailing(lambda e: e.role == "user")` is guarded — `if message is not None: drop_trailing(...)` — so an API failure during a `send(None)` does not corrupt the pre-staged notification pair. `OpenAIResponsesSession._convert_input(None)` returns `[]` so the existing `previous_response_id` chain continues with no new input items; `CodexResponsesSession.send_stream` simply skips the append branch since it replays the full canonical interface on every request anyway.

### Pre-request hook (mid-turn tc_inbox drain — dormant)

All four `send` / `send_stream` paths in this file fire `self.pre_request_hook(self._interface)` after committing the message to the canonical interface but before the API call. Historically the kernel installed `BaseAgent._drain_tc_inbox_for_hook` here so involuntary tool-call pairs (mail notifications, soul.flow voices) spliced into the wire chat mid-turn. After the `.notification/` redesign (`fadbabf`/`d2da97e`) the hook is still installed but the queue is always empty in production; ACTIVE notifications now defer to the post-turn IDLE synthetic-pair path rather than mutating tool results at send time. Phase 3 will remove the hook entirely. Three regimes (preserved for historical context and future re-use):

- **`OpenAIChatSession.send` / `send_stream`** — canonical-interface; the hook splices into the same interface that's about to be serialized via `_build_messages()`. Spliced pair appears in this same API request. Same-turn delivery.
- **`OpenAIResponsesSession.send` / `send_stream`** — server-state via `previous_response_id`; the hook splices into `self._interface` but the wire payload comes from `_convert_input(message)` (just the new input). Spliced pair is recorded in canonical interface immediately for persistence/inspection but only reaches the LLM next turn after re-sync. Documented inline.
- **`CodexResponsesSession.send_stream`** — Codex's stateless backend replays the full canonical interface on every request (`to_responses_input(self._interface)`), so the hook delivers same-turn just like the CC path.

### Git history

16 commits. Key: context overflow recovery (`f65e395`), orphan tool_call guard (`8197fdc`), Codex stateless path (`7e88f47`, `a4bf117`), per-phase HTTP timeout caps (`81b95e2`), `cached_tokens` None coercion (`1e715ab`), `_build_messages` hook refactor (`70c0357`), pre-request hook for mid-turn tc_inbox drain (`f46b346`, now dormant), `send(None)` continue-from-wire contract (`f596ec1`).
