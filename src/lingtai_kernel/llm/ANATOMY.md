---
related_files:
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/llm/service.py
  - src/lingtai_kernel/ANATOMY.md
  - src/lingtai_kernel/llm/__init__.py
  - src/lingtai_kernel/llm/base.py
  - src/lingtai_kernel/llm/interface.py
  - src/lingtai_kernel/llm/service.py
  - src/lingtai_kernel/llm/streaming.py
  - tests/test_llm_service.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# llm

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues/mail/PR proposals; do not silently fix.

Provider-agnostic LLM protocol layer. This folder defines the canonical chat log, normalized response/tool schema types, streaming accumulation, and ABCs the kernel uses; concrete provider adapters live in the wrapper package under `src/lingtai/llm/`.

## Components

- `llm/__init__.py` — public re-export surface for `ChatSession`, `LLMResponse`, `ToolCall`, `FunctionSchema`, and `LLMService` (`llm/__init__.py:2-10`).
- `llm/base.py` — normalized dataclasses plus `ChatSession` ABC.
  - `ToolCall`, `UsageMetadata`, `LLMResponse`, and `FunctionSchema` define tool calls, token usage, provider responses, and tool schemas (`llm/base.py:25-115`).
  - `ChatSession` requires an `interface` property and `send()` accepting text, tool results, or `None` (`lingtai_kernel/llm/base.py:122-250`), then supplies default helpers for history/state, usage totals, streaming fallback, tool-result commits, tool/system updates, reset, interaction id, context window, and context-overflow recovery (`lingtai_kernel/llm/base.py:251-490`).
  - **`send()` signature contract** — adapters accept three message shapes: `str` (new user text → `add_user_message`), `list[ToolResultBlock]` (tool returns → `add_tool_results`), and `None` (the "continue from wire" signal — caller has already pre-staged the canonical interface, e.g. via `_inject_notification_pair`; the adapter must skip the input-append step and send the wire as-is). On API error the error-path `drop_trailing` must be guarded so a `None` send does not corrupt the pre-staged wire. See `lingtai/llm/openai/ANATOMY.md` and `lingtai/llm/anthropic/ANATOMY.md` for adapter-side details, and `base_agent/turn.py:_handle_tc_wake` for the call site that drives a turn off the existing wire.
  - **`pre_request_hook`** (`llm/base.py:155`) — optional callable adapters fire after committing the message to the canonical `ChatInterface` but before the API call. Historically the kernel installed `BaseAgent._drain_tc_inbox_for_hook` here to drain the involuntary tool-call inbox mid-turn. Post-`.notification/`-redesign (`fadbabf` / `d2da97e`) the hook is still installed but the queue is always empty in production — ACTIVE notifications now defer to the post-turn IDLE synthetic-pair path instead of a send-time prefix hook. Default `None` — adapters that don't install treat the call as a no-op. Phase 3 will remove the hook. See root `ANATOMY.md` "Notifications" for the full picture, including the canonical-vs-server-state regime distinction.
- `llm/interface.py` — canonical conversation representation.
  - Content blocks: `TextBlock`, `ToolCallBlock`, `ToolResultBlock`, `ThinkingBlock`; `ContentBlock` union and `content_block_from_dict()` (`llm/interface.py:35-228`).
  - `InterfaceEntry` is one role+content row with id, role, timestamp, provider metadata, model/provider, usage, and optional tool snapshot (`llm/interface.py:230-294`).
  - `ChatInterface` is the append-only source of truth for history (`llm/interface.py:296`). It appends system/user/assistant/tool-result entries (`llm/interface.py:522-660`), enforces/repairs tool-call pairing (`llm/interface.py:345-521`), removes strict synthetic pairs (`llm/interface.py:662-767`), prunes history (`llm/interface.py:857-930`), estimates tokens (`llm/interface.py:932-980`), and supports compaction summaries (`llm/interface.py:981-1057`).
- `llm/service.py` — `LLMService` ABC: `model`, `provider`, `create_session()`, `generate()`, and `make_tool_result()` (`llm/service.py:16-70`).
- `llm/streaming.py` — `StreamingAccumulator`, which gathers streaming text/thought/tool-call deltas and finalizes to `LLMResponse` (`llm/streaming.py:16-69`, `llm/streaming.py:145-170`). It supports sequential tool-call assembly (`llm/streaming.py:71-84`), index-keyed deltas (`llm/streaming.py:88-117`), atomic tool calls (`llm/streaming.py:121-126`), and `_finalize_tool()` (`llm/streaming.py:173-180`).

## Connections

- `base_agent/` imports kernel LLM types for service injection, tool execution, and synthetic history repair (`base_agent/__init__.py:29-36`, `base_agent/__init__.py:773`, `base_agent/__init__.py:1034`, `base_agent/__init__.py:1375`).
- `session.py` imports `ChatSession`, `FunctionSchema`, `LLMResponse`, and `LLMService` to own session lifecycle and token/context bookkeeping (`session.py:12-17`).
- `tool_executor.py` consumes `ToolCall` (`tool_executor.py:8`); `tc_inbox.py` (legacy, dormant — preserved for back-compat until Phase 3) consumes `ToolCallBlock`/`ToolResultBlock` for synthetic pairs (`tc_inbox.py:33`). The same canonical block types are now used by `BaseAgent._inject_notification_pair` to splice synthesized `notification(action="check")` `(call, result)` pairs into the wire, replacing the legacy queue path.
- `intrinsics/psyche/` and `intrinsics/soul/` use canonical blocks/interfaces for molt replay and soul-flow consultation (`intrinsics/psyche/_molt.py:13`, `intrinsics/soul/inquiry.py:15`, `intrinsics/soul/consultation.py:196`, `intrinsics/soul/consultation.py:347`, `intrinsics/soul/consultation.py:498`).
- Outbound from this folder is minimal: `ChatInterface.estimate_context_tokens()` lazy-imports `token_counter.count_tokens` (`llm/interface.py:943`).
- Wrapper boundary: `src/lingtai/llm/service.py` provides the concrete `LLMService` subclass (`src/lingtai/llm/service.py:25`); wrapper adapters import kernel types, but the kernel does not import the wrapper.

## Composition

- **Parent:** `src/lingtai_kernel/` (see `ANATOMY.md`).
- **Subfolders:** none.
- **Siblings:** `session.py` persists and compacts `ChatInterface`; `token_ledger.py` persists usage; `intrinsics/` manufactures synthetic LLM blocks for psyche/soul/email flows.

## State

- **Ephemeral:** `ChatInterface._entries`, `_next_id`, current system/tools, and `_pending_system` live in memory for one session (`llm/interface.py:305-318`).
- **Ephemeral:** `StreamingAccumulator` stores partial text, tool args, thoughts, and usage until `finalize()` (`llm/streaming.py:39-69`).
- **Persistent writes:** none in this folder. `session.py` writes `history/chat_history.jsonl`; token/state persistence happens in sibling modules that consume these types.

## Notes

- `add_system()` defers system/tool updates while the tail has unanswered tool calls so strict providers do not see a system entry between assistant tool calls and user tool results (`llm/interface.py:522-572`).
- `close_pending_tool_calls()` closes unanswered tail tool calls by first accepting optional real recovered `ToolResultBlock`s from a recovery lookup and then synthesizing abort placeholders for any remaining misses (`llm/interface.py:445-521`, `llm/interface.py:99-186`).
- `StreamingAccumulator` intentionally supports three provider styles in one place: sequential, index-keyed, and atomic tool calls (`llm/streaming.py:71-126`).
