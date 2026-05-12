# src/lingtai/llm/

LLM adapter layer — multi-provider support with adapter registry, base classes, rate limiting, and interface converters.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 20 | Re-exports kernel types (`ChatSession`, `LLMResponse`, `ToolCall`, `FunctionSchema`, `ChatInterface`) + `LLMAdapter` from `base.py`. Triggers `register_all_adapters()` on import. |
| `_register.py` | 108 | Registers adapter factories for all providers with `LLMService.register_adapter()` |
| `api_gate.py` | 112 | `APICallGate` — RPM rate limiter with deque timestamps, `ThreadPoolExecutor`, daemon gate thread |
| `base.py` | 150 | `LLMAdapter` ABC (4 abstract methods), `_GatedSession` proxy |
| `interface_converters.py` | 335 | Bidirectional converters: `to_*` / `from_*` for Anthropic, OpenAI, OpenAI Responses API, Gemini |
| `service.py` | 313 | `LLMService` concrete class — adapter registry, session management, one-shot generation |

## Connections

- **Kernel types** — `__init__.py:3` imports `ChatSession`, `LLMResponse`, `ToolCall`, `FunctionSchema` from `lingtai_kernel.llm.base`; `ChatInterface` from `lingtai_kernel.llm.interface`.
- **ABC chain** — `LLMAdapter` (`base.py:53`) → abstract `create_chat`, `generate`, `make_tool_result_message`, `is_quota_error`. `LLMService` (`service.py:51`) extends `lingtai_kernel.llm.service.LLMService` ABC.
- **Adapter registration** — `_register.py` registers 7 dedicated factories + 6 generic-routed providers (`grok`, `qwen`, `glm`, `zhipu`, `kimi`, `mimo`) via dedicated or `_custom` factories (`_register.py:91-106`).
- **Interface converters** — imported by adapter session modules (e.g. `openai.adapter` imports `to_openai`, `to_responses_input` from `interface_converters.py:120`).
- **Rate gating** — `LLMAdapter._setup_gate(max_rpm)` creates `APICallGate`; `_wrap_with_gate()` returns `_GatedSession` proxy for sessions.

## Composition

- **Factory pattern** — `LLMService._adapter_registry` (class-level dict) maps provider name → `Callable[..., LLMAdapter]`. Each factory receives `(model, defaults, **kw)` and lazy-imports the adapter module.
- **Adapter caching** — `LLMService._adapters` keyed by `(provider, base_url)` tuple (`service.py:95`). Double-checked locking via `_adapter_lock` (`service.py:153`).
- **Session tracking** — `LLMService._sessions` dict maps `st_<12-hex>` session IDs to `ChatSession` instances (`service.py:100`). Untracked sessions get `session_id=""`.
- **Gated sessions** — `_GatedSession` (`base.py:19`) proxies `send()` and `send_stream()` through `APICallGate.submit()`. Attribute writes land on the proxy; reads fall through to inner session via `__getattr__`.
- **Codex factory** — `_register.py:54` builds `CodexOpenAIAdapter`, monkey-patches `create_chat` and `generate` to refresh OAuth tokens before each call via `CodexTokenManager.get_access_token()`.

## State

- **Class-level** — `LLMService._adapter_registry` (shared across all instances); `LLMAdapter._gate` (per-adapter instance).
- **Instance-level** — `LLMService._adapters` cache; `LLMService._sessions` registry; `APICallGate._timestamps` deque for RPM window.
- **Provider defaults** — `LLMService._provider_defaults` dict injected at construction (`service.py:96`). Drives model, base_url, max_rpm, api_compat settings.
- **Key resolution** — `LLMService._key_resolver` callable (`service.py:94`); defaults to `os.environ.get(f"{PROVIDER}_API_KEY")`.

## Notes

- **Abstract methods** — `LLMAdapter` requires: `create_chat()` (line 86), `generate()` (line 119), `make_tool_result_message()` (line 136), `is_quota_error()` (line 148).
- **Tool-call ID dual system** — Provider-issued wire IDs (e.g. Anthropic `tool_use_id`, OpenAI `tool_call_id`) flow through `tool_call_id` kwarg. LingTai issues its own `_tool_call_id` (`service.py:35`: `tc_<unix>_<4-hex>`) stamped onto every result dict for agent-level correlation.
- **Interface converters** — Four bidirectional pairs:
  - `to_anthropic`/`from_anthropic` — Anthropic Messages format (system excluded, ThinkingBlock with signature round-trip)
  - `to_openai` — Chat Completions format (tool results as `role=tool`, ThinkingBlocks emit as `reasoning_content` for DeepSeek round-trip; other OpenAI-compat providers ignore the field). One-way only — OpenAI history rehydration goes through `content_block_from_dict` on the canonical interface, not a reverse converter.
  - `to_responses_input` — Responses API input items (`function_call` / `function_call_output` shapes; non-empty ThinkingBlocks replay as `reasoning` items with `summary_text`, before assistant text/calls; `interface_converters.py:184-259`)
  - `to_gemini`/`from_gemini` — Interactions TurnParam format (`role=model`, `function_call`/`function_result`, `thought` blocks)
- **ToolCallBlock shape conversions** — Anthropic: `tool_use` with `input` dict. OpenAI CC: `function_call` with `arguments` JSON string. Responses: `function_call` with `arguments` JSON string and `call_id`. Gemini: `function_call` with `arguments` dict and `id`.
- **APICallGate mechanics** — Gate thread dequeues items, prunes timestamps >60s old, sleeps if RPM window full, dispatches to pool (`api_gate.py:71-103`). Pool size defaults to `max(2, min(32, max_rpm // 3))`.
- **Pre-request hook convention** (`f46b346`, dormant after notification redesign) — every adapter `send()` / `send_stream()` checks `self.pre_request_hook` after committing the message to the canonical `ChatInterface` and before the API call. Historically the kernel used this to drain the involuntary tool-call inbox mid-turn. Post-`.notification/`-redesign the queue is always empty; ACTIVE notifications now defer until the post-turn IDLE boundary instead of using a send-time prefix hook. Phase 3 will remove the hook. See kernel `llm/ANATOMY.md` for the ABC contract and root `ANATOMY.md` for the full notification architecture.
- **`send(None)` contract** — every adapter `send()` / `send_stream()` accepts `None` as the "continue from wire" signal: caller has already pre-staged the canonical interface (e.g. `BaseAgent._inject_notification_pair` spliced a synthesized `system(action="notification")` `(call, result)` pair); the adapter must skip the input-append step, send the wire as-is, and on API failure must NOT run `drop_trailing` (which would corrupt the pre-staged pair). Driven from `base_agent/turn.py:_handle_tc_wake` — the LLM sees the synthesized pair at the wire tail and reacts as if the agent had voluntarily called the tool.
- **Git history** — 16 commits. Key: Codex stateless path (`7e88f47`, `a4bf117`), context overflow recovery (`f65e395`), orphan tool_call guard (`8197fdc`), per-call HTTP timeout (`e279965`), pre-request hook for mid-turn tc_inbox drain (`f46b346`, now dormant), `send(None)` continue-from-wire contract (`f596ec1`).
