# src/lingtai/llm/claude_agent_sdk/

Clean-room completion-style LLM provider that drives the public `claude_agent_sdk`
package as a plain next-turn text generator. The SDK's own agentic tool loop is
disabled — LingTai keeps its tool loop and parses tool calls from the assistant
text upstream.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|------|-----|------|
| `__init__.py` | 3 | Re-exports `ClaudeAgentSDKAdapter`, `ClaudeAgentSDKChatSession` from `adapter.py` |
| `adapter.py` | 445 | `ClaudeAgentSDKAdapter` (LLMAdapter) + `ClaudeAgentSDKChatSession` (ChatSession); lazy SDK import, prompt rendering, response/usage collection |
| `defaults.py` | 10 | `DEFAULTS` dict — `api_compat`, no `base_url`/`api_key_env` (CLI-login auth), default `model` |

## Connections

- **Kernel types** — `adapter.py` imports `ChatSession`, `FunctionSchema`, `LLMResponse`, `ToolCall`, `UsageMetadata` from `lingtai_kernel.llm.base`; `ChatInterface`, `TextBlock`, `ToolResultBlock` from `lingtai_kernel.llm.interface`.
- **ABC** — `ClaudeAgentSDKAdapter` extends `lingtai.llm.base.LLMAdapter` (implements `create_chat`, `generate`, `make_tool_result_message`, `is_quota_error`); `ClaudeAgentSDKChatSession` extends `lingtai_kernel.llm.base.ChatSession`.
- **Registration** — `_register.py` registers `_claude_agent_sdk` factory under both `claude-agent-sdk` and `claude_agent_sdk` (`_register.py`, after `mimo`). The factory drops `api_key`/`base_url` (CLI-login auth) and lazy-imports the adapter.
- **Optional SDK** — `claude_agent_sdk` is imported only inside `_import_sdk()` on the call path; never at module import. Absent package → `RuntimeError` with install/auth guidance.

## Composition

- **Single-turn generator** — `_build_options` sets `allowed_tools=[]`, `max_turns=1`, and `setting_sources=[]`. Each `send()` produces exactly one assistant text turn.
- **Canonical interface as source of truth** — `send()` commits the incoming message, fires `pre_request_hook`, calls `enforce_tool_pairing()`, renders the whole conversation via `_build_prompt` into one role-labeled string, then appends the assistant text back.
- **Prompt rendering** — `_build_prompt` joins `conversation_entries()` as `User:` / `Assistant:` blocks (system excluded — carried in `ClaudeAgentOptions.system_prompt`) and appends a trailing `Assistant:` nudge. `_render_block` flattens text/tool-result/tool-call/thinking blocks.
- **Async bridge** — `_run_query` drives the SDK's async-generator `query()` to completion via `asyncio.run`, returning the full message list.
- **Response collection** — `_collect_response` duck-types `AssistantMessage`/`TextBlock` for text and `ResultMessage.usage` for tokens; `tool_calls` always empty. `_parse_usage` normalizes input tokens as `input + cache_read + cache_write` (Anthropic semantics).

## State

- **Session-level** — `_model`, `_system`, `_interface`, `_cwd`, `_extra_options`, `_context_window`. No persistent HTTP client (SDK manages the CLI session).
- **Adapter-level** — `_default_model`, `_cwd`, optional `_gate` (via `_setup_gate`).

## Notes

- **Default model** — defaults to Claude CLI alias `sonnet`; dated API model IDs may not be accepted by the Claude Code CLI SDK.
- **No API key** — the SDK authenticates through the local Claude CLI login. `defaults.py` omits `api_key_env`; the registry factory discards any env-resolved key/base_url.
- **`send(None)` contract** — honored: `None` means the caller pre-staged the wire; the adapter skips the input-append and does not `drop_trailing` on failure.
- **Streaming** — `send_stream` is non-streaming under the hood: it runs `send()` and emits the final text once via `on_chunk`.
- **Unsupported knobs** — `generate()` ignores `temperature`, `json_schema`, `max_output_tokens` (no SDK surface for them in this mode). `json_schema`/`force_tool_call`/`thinking` on `create_chat` are accepted for ABC compatibility but not applied.
- **Quota detection** — `is_quota_error` is best-effort string matching ("rate limit" / "429") — the SDK exposes no typed rate-limit error.
- **Tests** — `tests/test_claude_agent_sdk_adapter.py` fakes `claude_agent_sdk` in `sys.modules`; CI needs no Claude login or network. Covers missing-SDK error, text/usage extraction, role-labeled prompt, multi-turn accumulation, error revert, registration.
