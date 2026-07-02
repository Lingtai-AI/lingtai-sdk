---
related_files:
  - src/lingtai_kernel/intrinsics/ANATOMY.md
  - src/lingtai_kernel/intrinsics/system/__init__.py
  - src/lingtai_kernel/intrinsics/system/karma.py
  - src/lingtai_kernel/intrinsics/system/preset.py
  - src/lingtai_kernel/intrinsics/system/schema.py
  - src/lingtai_kernel/intrinsics/system/summarize.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# intrinsics/system

System intrinsic — runtime, lifecycle, and synchronization. Provides the agent with refresh (hot-reload config/presets), karma-gated lifecycle actions on other agents (sleep, lull, suspend, cpr, interrupt, clear, nirvana), preset listing, and context summarization. **System no longer owns any notification verb** — voluntary notification reads and generic dismiss moved to the standalone `notification` tool (sibling package `intrinsics/notification/`, atomic actions `check`/`dismiss_channel`/`dismiss_event`/`dismiss_ref`). The system module remains the **conceptual home** of the notification *producer* surface, though — it re-exports `publish_notification` / `clear_notification` from the kernel-root `notifications.py` so any in-process producer (intrinsic, capability, or wired-in MCP server) submits through one canonical entry point.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — Package surface. Re-exports all public API for backward compatibility. Contains:
  - `get_description` / `get_schema` (re-exported from `schema.py`) — tool registration.
  - **No notification/dismiss verb.** The generic notification dismiss (formerly `_dismiss`) moved to the standalone `notification` tool. `large_tool_result` reminder events prefer a successful `system(action="summarize")` of the matching `tool_call_id`, which calls `notifications.clear_large_result_reminders` to remove the matching `large_tool_result:{tool_call_id}` event automatically. The notification tool's dismiss verbs (`dismiss_channel`/`dismiss_event`/`dismiss_ref`) may also acknowledge and remove those reminders as an escape hatch for stale or pre-molt refs that can no longer be summarized.
  - **`publish_notification` / `clear_notification`** (re-exported from `lingtai_kernel.notifications` as `submit` / `clear`) — canonical producer entry point. Importable as `from lingtai_kernel.intrinsics.system import publish_notification, clear_notification`. The system module owns the notification surface conceptually; the implementation lives at the kernel root so non-intrinsic callers (and external scripts) can import it without going through the intrinsic surface.
  - All handler functions re-exported from sub-modules for backward compatibility.
  - `handle()` (`__init__.py:82-109`) — main dispatcher with explicit dispatch table over the action enum (`refresh`, `sleep`, `lull`, `interrupt`, `suspend`, `cpr`, `clear`, `nirvana`, `presets`, `summarize`). No notification/dismiss fast path remains — voluntary reads and dismiss live on the standalone `notification` tool.

- `preset.py` — Preset management and refresh.
  - `_preset_ref_in()` (`preset.py:9-36`) — normalized membership test for preset path strings (~/foo vs absolute).
  - `_check_context_fits()` (`preset.py:39-76`) — verify agent's current context fits within target preset's context_limit.
  - `_refresh()` (`preset.py:79-199`) — stop, reload config + MCP servers, restart. Handles preset swap (named or revert) with authorization gate and context-limit guard. **Empty-string normalization:** `args.get('preset')` returning `''` or whitespace-only is treated as absent (`preset_name = None`) before any conflict/swap logic; this protects against tool-call providers that serialize optional string fields as `""` instead of omitting them — without normalization, an empty string would fall into the allowed-list gate and surface as `"preset '' is not in this agent's allowed list"`. The `preset='' + revert_preset=True` combination is consequently treated as a plain revert (no conflict). **MCP retry hook (issue #34):** before calling `agent._perform_refresh()`, invokes `agent._retry_failed_mcps()` if the Agent subclass defines it. Failures are logged and swallowed so a flaky MCP cannot block refresh itself. Lets the documented "fix config → refresh" recovery path work in-process.
  - `_presets()` (`preset.py:202-282`) — list available presets with LLM connectivity probing.

- `karma.py` — Karma-gated lifecycle actions.
  - `_KARMA_ACTIONS` / `_NIRVANA_ACTIONS` (`karma.py:13-14`) — gate mapping sets.
  - `_check_karma_gate()` (`karma.py:15-33`) — authorization gate: validates karma/nirvana admin flags, resolves target address, rejects self-targeting.
  - `_sleep()` (`karma.py:36-91`) — self-sleep (no karma needed). Guards the ACTIVE→ASLEEP transition against a divergent `.notification/` fingerprint: if mail/system payloads arrived during the same turn (so `notification_fingerprint(workdir) != agent._notification_fp`) and `force` is not set, refuses sleep and returns a status-ok refusal message rather than transitioning state. `force=True` bypasses the guard. Fixes kernel#112.
  - `_lull()` (`karma.py:94-107`) — put another agent to sleep.
  - `_suspend()` (`karma.py:109-121`) — suspend another agent.
  - `_cpr()` (`karma.py:124-137`) — resuscitate a suspended agent.
  - `_interrupt()` (`karma.py:140-151`) — interrupt a running agent's current turn.
  - `_clear()` (`karma.py:154-174`) — force a full molt on another agent.
  - `_nirvana()` (`karma.py:177-198`) — permanently destroy an agent's working directory.

- *(removed)* `notification.py` — the agent-facing generic dismiss submodule was **deleted**. Its `_dismiss` verb (and the voluntary notification-read fast path) moved to the standalone `notification` tool (sibling package `intrinsics/notification/`), whose atomic actions `dismiss_channel`/`dismiss_event`/`dismiss_ref` delegate to the kernel-root `notifications.dismiss_channel`. There are **no** `system` compatibility aliases. `system/__init__.py` no longer imports or re-exports `_dismiss`.
  - Producer-side notification submission still lives in `notifications.py` at the kernel root and is re-exported by this package's `__init__.py` as `publish_notification` / `clear_notification`. See root `ANATOMY.md` "Notifications" for the full architecture and dismissal taxonomy.

- `summarize.py` — Agent-authored context summarization.
  - `SUMMARIZE_MARKER` (`summarize.py:13`) — stable string marker (`"lingtai_agent_summarized_result"`) stamped on every replacement block so idempotency checks and future passes can detect them without heuristics.
  - `_is_already_summarized()` (`summarize.py:17`) — returns True iff a ToolResultBlock's content is already a summarize replacement.
  - `_find_tool_result_block()` (`summarize.py:22`) — walks live `ChatInterface._entries` and returns the `(entry, block_index, block)` triple for a given `tool_call_id`, or `(None, -1, None)` when not found.
  - `_summarize()` (`summarize.py:51`) — main handler. For each item, finds the matching `ToolResultBlock`, validates it (not already summarized, not a daemon result), mutates `block.content` in place with the compact replacement dict, logs `tool_result_summarized`, and calls `agent._save_chat_history(ledger_source="summarize")` after all successful mutations. After persisting, it calls `notifications.clear_large_result_reminders(agent, summarized_ids)` so each successfully summarized `tool_call_id` clears its matching `large_tool_result:{tool_call_id}` system reminder automatically — the preferred discharge path. The notification tool's dismiss verbs can acknowledge and remove these reminders when summarization is no longer possible. The result dict carries `cleared_reminders` (list of cleared ref_ids). Returns per-item success/failure with reasons. Passing `notification_threshold_chars` returns an error (`reason: runtime_threshold_change_not_supported`) — the threshold is config-only.

- `schema.py` — Tool registration.
  - `get_description()` (`schema.py:5-7`) — returns localized tool description.
  - `get_schema()` — returns JSON schema for the system tool. Action enum is exactly `["refresh", "sleep", "lull", "interrupt", "suspend", "cpr", "clear", "nirvana", "presets", "summarize"]` — no `notification`/`dismiss`. The dismiss-only properties (`channel`, `force`, `event_id`, `ref_id`) are gone; remaining properties are `items` (for `summarize`), `reason`, `address`, `preset`, `revert_preset`. `notification_threshold_chars` was removed from the schema — the threshold is config-only (init.json + refresh).

## Connections

- **Inbound:** `handle()` is called by the tool dispatcher (via `base_agent._dispatch_tool`).
- **Inbound (cross-module):** `publish_notification` is imported by `base_agent/messaging.py` (both `_rerender_unread_digest` and `_enqueue_system_notification`) and by `intrinsics/soul/flow.py:_run_consultation_fire`. `clear_notification` is imported by the same call sites for the empty-state path. The generic dismiss verb now lives on the standalone `notification` tool (delegating to kernel-root `notifications.dismiss_channel`); `system` no longer exposes it. `summarize.py:SUMMARIZE_MARKER` and `_is_already_summarized` are re-exported from `__init__.py` for external callers.
- **Tool-result metadata**: Tool result metadata is the umbrella for formal blocks nested under a single `_meta` envelope: permanent per-result `_meta.tool_meta`, sparse/update-driven `_meta.agent_meta`, sparse/update-driven `_meta.guidance` (a lightweight ref/hook to the resident `meta_guidance` system-prompt section, where the full guidance sections + `_meta` readme + adapter static rules live), and channel-owned sparse/update-driven `_meta.notifications` / `_meta.notification_guidance` (attached on first appearance and re-attached only on a material payload change or a deliberate `notification(action=check)` read, not re-stamped onto the newest result when unchanged). The old `_tool_result_metadata` compatibility block is intentionally removed; large-result follow-up is surfaced through `_meta.agent_meta.current_tool_result_chars` (a ranked summarize-candidate list, see below) rather than a provider-visible metadata field or a pushed notification.
- **Large-result ranking (replaces the removed `large_tool_result` notification):** Large tool results no longer raise a `large_tool_result` system notification. The two former producers — `base_agent/__init__.py:_maybe_notify_large_tool_result()` (per-result hook) and `base_agent/messaging.py:_rescan_large_tool_results()` (turn-boundary rescan) — are retained as inert no-ops (the hook seam and the turn-boundary housekeeping trio are unchanged; the total-length gate, the `_pending_large_result_*` helpers, and `LARGE_RESULT_TOTAL_LEN_GATE` were deleted). Instead, `meta_block.current_tool_result_chars()` ranks the largest formal results in live context and stamps them onto `_meta.agent_meta.current_tool_result_chars` (on the most recent emitted sparse `agent_meta` snapshot, which may be earlier than the newest tool result): `total_chars`, `threshold` (the `_summarize_notification_threshold` hint size, default 3000, config-only via `manifest.summarize_notification_threshold` in init.json + refresh), `over_threshold_count`, and `top_results` (id, tool_name, chars; top 5 over 1000 chars, no preview). `_meta`/notification/guidance scaffolding is excluded by `formal_tool_result_visible_len`. The agent reads `top_results`, digests what it needs, then calls `system(action="summarize")` for the chosen `tool_call_id`s. The threshold still drives the `tool_meta.comment.overflow` hint in `ToolExecutor` and is config-only (passing `notification_threshold_chars` to `system(action="summarize")` returns an error). **Legacy compatibility:** any `large_tool_result` event persisted before this change (or pre-molt) is still dismissible via the `notification` tool, and a successful `system(action="summarize")` of its `tool_call_id` still auto-clears it via `notifications.clear_large_result_reminders`.
- **Outbound:** Depends on `...notifications` (canonical `submit`/`clear`/`collect_notifications`), `...i18n` (translations), `...handshake` (`resolve_address`, `is_agent`, `is_alive`), `...state` (`AgentState`), `...presets` (preset loading), `...preset_connectivity` (connectivity probing). `summarize.py` depends on `...llm.interface:ToolResultBlock` for chat-history mutation.
- **Data flow:** Karma actions write signal files (`.sleep`, `.suspend`, `.interrupt`, `.clear`) into target agent working directories. Preset swap reads/writes `init.json` manifest. The `publish_notification` re-export writes `.notification/*.json` via `tmp + rename` (voluntary reads of that state now live on the standalone `notification` tool, not `system`). `summarize` mutates `ToolResultBlock.content` in the live `ChatInterface._entries` and calls `_save_chat_history(ledger_source="summarize")`; the original payload is never touched in `logs/events.jsonl`.

## Key invariants

- `handle()` uses an explicit dispatch table (`dict.get()`) rather than `globals().get()`, so it works correctly across sub-modules.
- `system` has **no** agent-callable `notification` or `dismiss` action; voluntary notification reads and dismiss live on the standalone `notification` tool. (The kernel still synthesizes a notification-delivery tool-call pair for IDLE/ASLEEP delivery — now shaped as `notification(action="check")` (no longer `system(action="notification")`), so the synthetic pair is byte-shape-identical to a voluntary `notification` check. That synthesis is `base_agent` plumbing, not a `system` intrinsic action. Such synthesized reads carry `_synthesized: True` in both the JSON body and the `ToolResultBlock.synthesized` flag, and the canonical live payload (`_meta.notifications` + `_meta.notification_guidance`) is stamped on by the turn loop's `attach_active_notifications` post-hook — guaranteeing one live notification payload in conversation history at any moment.)
- Karma gate checks resolve addresses through `_check_karma_gate()` which validates admin flags before any filesystem mutation.
- Generic notification dismiss is **not** a `system` verb — it lives on the standalone `notification` tool (delegating to kernel-root `notifications.dismiss_channel`). Its dismissal-taxonomy invariants (guarded producer channels refusing without `force`, protected source-of-truth channels refusing even with `force`, `post-molt` requiring a non-empty `reason`, `large_tool_result` acknowledge/remove handling) are documented at root `ANATOMY.md` "Notifications" and in `intrinsics/notification/ANATOMY.md`.
- Preset swap has two guards: authorization (allowed list) and context-fit (current tokens ≤ target context_limit).
- Producer notification writes (`publish_notification`) are atomic (`tmp + rename` inside `notifications.publish`) — readers never see a half-written file.
- `summarize` mutates only `ToolResultBlock.content` in the live `ChatInterface._entries` — entry order, role, id, name, synthesized flag, and `tool_call`/`tool_result` pairing are untouched. The durable `logs/events.jsonl` is never modified by `summarize`; the full original remains retrievable by `tool_call_id`.
- `_is_already_summarized()` detects the `SUMMARIZE_MARKER` sentinel to make re-summarization a per-item error rather than silently overwriting a prior summary.
- Large-result notification fires only for main-agent tool results; spill manifests (already capped) are excluded via `is_spill_manifest()`.
