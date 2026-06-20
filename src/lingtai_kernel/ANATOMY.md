# lingtai_kernel

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues/mail/PR proposals; do not silently fix.

The minimal agent runtime: turn loop, lifecycle, signal consumption, tool dispatch, intrinsic wiring, mailbox glue, soul/molt orchestration. The kernel is standalone — the wrapper package `lingtai` (at `src/lingtai/`) depends on it strictly one-directionally.

> **What is an `ANATOMY.md`?** See the canonical convention at `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`. This file follows the same 6-section template as every other anatomy in the tree.

## Components

The kernel root holds the coordinator (`base_agent/`) plus a flat collection of supporting modules. Most are self-contained leaves; subfolders are concept-boundary units with their own anatomy.

- `base_agent/` — `BaseAgent`, the kernel coordinator (package of 6 modules). `__init__.py` defines `BaseAgent` (~1481 lines: constructor, properties, state machine, hooks, cross-cutting stubs including the `.notification/` sync trio, pass-throughs to submodules). Submodules: `lifecycle.py` (start/stop/heartbeat/signals/refresh — heartbeat tick now also calls `_sync_notifications`), `turn.py` (main loop/message dispatch/AED/response processing), `tools.py` (tool schemas/dispatch/registry), `identity.py` (naming/manifest/status), `prompt.py` (system prompt building/flushing), `messaging.py` (mail/notification producers/outbound). Soul-flow domain logic lives in `intrinsics/soul/flow.py`. See `base_agent/ANATOMY.md`.
- `nudge/` — **per-agent periodic checks** that publish notification reminders when something needs the agent's attention. `__init__.py:run_checks(agent)` is called once per heartbeat tick from `base_agent/lifecycle.py:_heartbeat_loop` (~line 320, wrapped in try/except). Each check is a self-contained module under `nudge/` exposing `check(agent) -> None`. `kernel_version.py` uses the shared `.notification/nudge.json` multi-entry payload to detect newer wheels and nudges the agent to refresh. `goal.py` is an IDLE-only check that reads protected `.notification/goal.json`; if the file exists, is active, and the idle delay has elapsed, it publishes one short `goal.reminder` event into `.notification/system.json` pointing back to `goal.json` and the goal manual. See `nudge/ANATOMY.md` for details and the "add a new nudge" recipe.
- `notifications.py` — **canonical `.notification/` filesystem helpers**. Channel names are syntax-checked and then allowlisted: built-ins (`email`, `soul`, `system`, `goal`, `nudge`, `molt`, `post-molt`, `bash`, `cron`, `btw`, `tool_loop_guard`) plus dynamic `mcp.*` bridge channels. Unknown files in `.notification/` are ignored by `notification_fingerprint()` and `collect_notifications()` and rejected by helper publish/clear/dismiss calls. `notification_fingerprint()` is byte-content-based (`name`, byte count, SHA-256) so byte-identical producer rewrites do not wake/reinject solely because mtime changed. `publish(workdir, tool_name, payload)` writes one file atomically (tmp+rename); `clear(workdir, tool_name)` deletes one file (idempotent); `submit(workdir, tool_name, *, data, header, icon, priority, instructions=None)` is the canonical producer-facing helper. `dismiss_channel()` is the strict agent-facing generic dismiss path: protected source-of-truth channels such as `goal` refuse generic dismiss, while `system` additionally supports atomic single-event removal by `event_id` or `ref_id` without deleting the rest of `system.json`. The `system` intrinsic re-exports `submit` as `publish_notification` and `clear` as `clear_notification`.
- `session.py` — `SessionManager`. LLM session lifecycle, token bookkeeping, chat history persistence, AED (auto-error-recovery) retry path. `send()` owns prompt/tool refresh, health checks, LLM dispatch, and usage accounting; notification delivery stays in `BaseAgent._sync_notifications` so session sends never mutate unrelated tool-result content.
- `tool_executor.py` — `ToolExecutor`. Synchronous/parallel tool dispatch, reasoning/commentary stripping, ToolCallGuard policy/preflight evaluation, LoopGuard duplicate advisory metadata, timing, enriched model-visible error payloads, result spilling, provider-visible result construction, ACTIVE-turn tool-call progress metadata, and event-sourced tool-call lifecycle tracing. Each provider/top-level call gets a `tool_trace_id` and logs proposal/normalization/validation/guard approval-or-denial/dispatch/result visibility boundaries. With the default empty guard chain, behavior remains pass-through (`policy="default_allow"`); when a guard denies, `ToolExecutor` synthesizes a structured `ToolCallGuardDenied` tool result instead of dispatching the handler, preserving provider tool-call/tool-result pairing for future policy rejections. Error results returned to the model include repair context (`error_type`, `error_phase`, `tool_trace_id`, normalized `tool_args`/`arg_keys`, bounded traceback tail for executor exceptions) plus a nested `tool_error` recovery block with summary/reason/retryability/guidance, while durable lifecycle logs remain the forensic record. It executes only provider/top-level tool calls; the old reserved nested `secondary` side channel has been removed. For compatibility with stale model output or old cached schemas, `_execute_single` and `_execute_parallel` drop a top-level `secondary` argument before dispatch and log `deprecated_secondary_ignored` instead of executing any nested communication call.
- `tool_call_guard.py` — `ToolProposal`, `GuardDecision`, and `ToolCallGuard`: the formalized tool-call guard chain used by `ToolExecutor` between normalized proposal and execution. The default guard has no checks and returns `default_allow`; checks are functional hooks that may return structured allow/warn/deny decisions. Denials are designed to serialize directly into synthesized rejection tool results (`guard_decision`, check/action/severity/reason/proposal), while warnings may attach `_advisory.type="tool_call_guard"` without blocking dispatch.
- `tool_timing.py` — small helper for tool execution timing records.
- `tc_inbox.py` — `TCInbox` and `InvoluntaryToolCall`. **Legacy queue retained but dormant** under the `.notification/` redesign. Phase 3 will remove this module entirely; meanwhile the producer pipeline writes filesystem files instead of enqueuing. The molt path still calls `agent._tc_inbox.drain()` defensively to clear any pre-redesign items that survived a process restart.
- `prompt.py` — `SystemPromptManager` plus `build_system_prompt` / `build_system_prompt_batches`. Composes the system prompt from identity, capabilities, intrinsics, pad, rules. Both builders prepend kernel-injected runtime principles: a **dynamic language principle** derived from `config.language` (en/zh/wen, English fallback for unknown codes carrying the raw code) that pins the agent's working language for ordinary prose/human-facing replies, and an **activeness principle** from `config.activeness` (`quiet` / `balanced` / `responsive`, with aliases; default `balanced`) that sets the agent's communication posture from terse action-first to explicit close-the-loop responsiveness. These render before `base_prompt` and all sections. Joining `build_system_prompt_batches()` with `\n\n` (empty batches filtered) is byte-identical to `build_system_prompt()`, matching the LLM session batch fallback contract. Default render order (`prompt.py:98-114`): principle → covenant → tools → **substrate** → procedures → comment, then rules → brief → skills → knowledge → identity → **character** → pad. `substrate` sits **right after** `tools` so it provides a compact resident operating model and routes expanded runtime/procedure guidance to the `system-manual` skill. `character` (the agent's self-authored identity from `system/lingtai.md`, written by `_lingtai_load`) renders right after the mechanical `identity` section and before `pad` — distinct from both `covenant` (operator contract, Batch 1) and `identity` (name/nickname/manifest). The kernel ships `lingtai/prompts/substrate.md` as the packaged compact default (issue #39) and `src/lingtai/intrinsic_skills/system-manual/SKILL.md` as the expanded manual; the `Agent` subclass auto-seeds `system/substrate.md` from it on first boot — no init.json opt-in required.
- `meta_block.py` — meta-block rendering (the structured prefix the kernel injects into LLM messages with state, time, stamina, etc.).
- `message.py` — `_make_message`, message-type sentinels (`MSG_REQUEST`, `MSG_TC_WAKE`). The wire format for the agent's inbox queue.
- `state.py` — `AgentState` enum (ACTIVE / IDLE / STUCK / ASLEEP / SUSPENDED).
- `config.py` — `AgentConfig` dataclass. Constructor-time options (stamina, soul cadences, max RPM, optional prompt activeness posture, etc.). Legacy `max_turns` remains for API compatibility but no longer owns the ACTIVE-turn tool-call emergency fuse.
- `safety_limits.py` — kernel-owned safety constants that are deliberately not user/preset/agent manifest configuration. Currently owns `ACTIVE_TURN_TOOL_CALL_EMERGENCY_LIMIT = 10_000` and `ACTIVE_TURN_TOOL_CALL_NOTICE_INTERVAL = 500` for the tool-call progress meter / emergency fuse.
- `workdir.py` — `WorkingDir`. Filesystem layout under the agent's working directory; manifest read/write; git operations. Also module-level `write_resolved_manifest` (`workdir.py:68`) — publishes the fully-resolved, secret-redacted manifest to `system/manifest.resolved.json` (atomic, best-effort) after every successful init read; called by `lingtai.Agent._read_init` (issue #259). Secret-key dropping lives in `_redact_secrets` (`workdir.py:50`).
- `handshake.py` — agent-discovery primitives (`is_agent`, `is_alive`) used by the TUI/portal to scan `.lingtai/` directories.
- `token_counter.py` — token counting helper (used for diary-cue cap, system prompt sizing).
- `token_ledger.py` — append-only per-call token usage log (`logs/token_ledger.jsonl`).
- `trace_redaction.py` — small deterministic redactor for durable trajectory writes. It redacts high-confidence token/key/password shapes in nested dict/list/string payloads before event/chat traces are persisted, without mutating the live in-memory conversation.
- `time_veil.py` — coarse-time rendering for state-aware prompts.
- `loop_guard.py` — ACTIVE-turn tool-call progress meter plus narrow loop detectors. The total-call ceiling is a large emergency fuse from `safety_limits.py`, not a normal workflow boundary or manifest-controlled setting; duplicate-call and invalid-tool checks remain focused loop detectors; duplicate keys strip `_reasoning` as metadata so semantically identical poll/list calls cannot bypass detection by changing rationale text.
- `logging.py` — logger configuration (separate from the `services/logging.py` event-log service).
- `llm_utils.py` — small shared helpers used by adapter implementations.
- `types.py` — shared type aliases.

## Connections

- **Kernel must never import from the wrapper.** `lingtai_kernel` is standalone; `lingtai` (the wrapper at `src/lingtai/`) depends on it strictly one-directionally.
- The kernel exposes its public surface through `__init__.py`. Anything not re-exported there is implementation detail.
- The wrapper layer registers LLM adapters into `llm.service` at import time, registers capabilities into `Agent` (which subclasses `BaseAgent`), and provides MCP, FileIO, Vision, Search, and the CLI.

## Notifications — the `.notification/` filesystem-as-protocol

Out-of-band events — mail arrival, soul-flow firings, daemon emanations, MCP webhook events, kernel-internal alerts — surface as one **live notification payload holder** at a time. When the agent is IDLE/ASLEEP, the holder is a synthetic `(ToolCallBlock, ToolResultBlock)` pair of shape `system(action="notification")` whose result carries the canonical JSON union of all currently-active producer files. When the agent is ACTIVE and has just produced an ordinary dict-shaped tool result, the holder is the same canonical `notifications` + `_notification_guidance` payload attached to that latest result (`meta_block.py:180-454`, `base_agent/turn.py:926-937`). Older holders remain in history only as skeletons/placeholders.

```
assistant: tool_call(id=notif_…, name="system", args={action:"notification"})
user:      tool_result(id=notif_…, synthesized=True, content="""{
             "_synthesized": true,
             "notifications": {
               "email":  { "header": "3 unread emails", "icon": "📧", ... },
               "soul":   { "header": "soul flow",       "icon": "🌊", ... },
               "system": { "header": "2 system notifications", "icon": "🔔",
                           "data": { "events": [...] } }
             }
           }""")
```

The canonical live payload is built by `meta_block.build_notification_payload` (`meta_block.py:180-222`) and used by both delivery surfaces. The synthesized-pair envelope comes from `BaseAgent._inject_notification_pair` (`base_agent/__init__.py:1192-1381`): it adds `_synthesized: True` (also written as the `synthesized=True` flag on the `ToolResultBlock`) around the canonical payload so the agent can distinguish kernel-injected reads from voluntary `system(action="notification")` calls when reading conversation history. The assistant side of that synthesized pair is now tool-only (`ToolCallBlock` without a visible summary `TextBlock`); model-visible notification details and guidance live in the matching tool result body so normal notification wakes do not surface as diary/text-input-like transcript text. The kernel adds a top-level `_notification_guidance` field plus a source-specific `_notification_guidance` field into each per-source block under `notifications` — this is kernel safety framing, separate from the producer's own optional `instructions` field (see Producer contract below). ACTIVE tool-result delivery calls `meta_block.attach_active_notifications` (`meta_block.py:336-454`, wired from `base_agent/turn.py:926-937`), which attaches that same canonical payload to the latest dict-shaped tool result; it does not build a compact/preview-only representation. Each actual-block injection also records a durable `notification_block_injected` event with `mode`, `call_id`, `sources`, canonical `payload` (`notifications` + `_notification_guidance`), and `meta`; this event is the historical/log surface for TUI `/notification` and is distinct from compact `notification_pair_injected` synthesized-pair metadata.

### Communication responsiveness

Notification previews deliberately trade completeness for latency: they let the agent notice that something arrived, not necessarily act safely from the preview alone. The operational rule lives in the substrate: answer directly only when the newest human message is complete and unambiguous; otherwise read the producer with the normal top-level channel tool before doing long work. If a human is waiting and the next step may take time, acknowledge or reply through the communication tool directly before starting the long-running tool. The kernel no longer provides a reserved nested communication side channel; tool execution now stays aligned with provider/top-level tool calls.

### Filesystem layout

Producers write a JSON file per channel into `<workdir>/.notification/`:

| File | Owner | Naming convention |
|---|---|---|
| `email.json` | `intrinsics/email` (unread digest, `_rerender_unread_digest`) | bare intrinsic name |
| `soul.json` | `intrinsics/soul/flow.py` (consultation fire) | bare intrinsic name |
| `system.json` | `base_agent/messaging.py:_enqueue_system_notification` (events list, max 20 newest; supports per-event dismiss by `event_id`/`ref_id`) | bare intrinsic name |
| `goal.json` | active-goal source of truth (protected from generic dismiss) | bare intrinsic name |
| `mcp.<server>.json` | external MCP server adapter (e.g. `mcp.imap.json`, `mcp.telegram.json`) | dotted prefix |

Each file is the producer's complete state for that channel — there is no "queue of unread events." When the producer's state empties (e.g. unread count drops to 0), it deletes the file. The basename (without `.json`) becomes the dict key the agent sees in `notifications`.

The reader path is allowlist-based. Built-in channels and the dynamic `mcp.*` prefix are visible; unknown `.notification/*.json` files are ignored by fingerprinting/collection and cannot be mutated through kernel publish/clear/dismiss helpers. This prevents accidental prompt injection from arbitrary files in the notification directory while preserving MCP server fan-out.

### Single-slot wire invariant

At most ONE live notification payload exists in the wire history at any time. The live holder is recorded as `agent._notification_live_holder` (`base_agent/__init__.py:430-439`) and may be either a synthesized notification result dict or a normal tool-result content dict. When payload moves, `meta_block.skeletonize_notification_holder` (`meta_block.py:290-325`) strips `notifications`/guidance (and the retired `_notifications` upgrade-cleanup key) from the old normal result or replaces an old synthesized result with a skeleton placeholder. Historical synthesized pairs are therefore preserved for chronology, but only the newest holder contains actionable notification data. Agents observe the **current** notification state, not a history of arrivals. Past arrivals belong in the producer's own logs (e.g. `mailbox/inbox/`, `logs/soul_flow.jsonl`), not in live payload history.

### Producer contract — `submit(workdir, tool_name, *, data, header, icon, priority, instructions=None)`

In-process producers call **`publish_notification`** (re-exported by the `system` intrinsic from `notifications.submit`) — the canonical helper that wraps `notifications.publish` with the standard envelope:

```python
from lingtai_kernel.intrinsics.system import publish_notification, clear_notification

publish_notification(
    agent._working_dir, "email",
    header=f"{n} unread email{'s' if n != 1 else ''}",
    icon="📧",
    instructions=(
        "After handling, call email(action=\"read\", email_id=[...]) "
        "or email(action=\"dismiss\", email_id=[...]) to clear "
        "handled mails from this notification."
    ),
    data={"count": n, "newest_received_at": ts, "digest": body},
)

# When state empties:
clear_notification(agent._working_dir, "email")
```

Side effects of `publish_notification`:
- Writes `.notification/<tool_name>.json` atomically (tmp + rename) with `{header, icon, priority, published_at, data}` plus an optional top-level `instructions` field when the producer supplies one.
- Returns immediately — no enqueue, no wake post. The kernel sync mechanism (next section) handles wire injection.

The optional `instructions` field is the producer-side directive — text describing what the agent must do to dismiss or act on the notification. It rides with the payload so each producer owns its own dismissal contract; generic frontend / kernel code does not need to know per-producer rules. Email uses it for "call read or dismiss to clear"; soul flow sets it to advise that voices are inner monologue (no dismissal needed); MCP servers can carry their own. Separately from `instructions`, the kernel injects its own `_notification_guidance` text both at the envelope top level and into each per-source block when it synthesizes the wire pair — that is kernel-side safety framing about source provenance and verification, not a producer dismissal contract.

**Dismissal contract.** Producers fall into four categories. **Category A — mirror over real producer state** (e.g. `email` over `read.json`) MUST register with `register_generic_dismiss_guard("<channel>", "<suggested verb>")`; generic clearing would leave producer state unchanged and the mirror inaccurate. **Category B — notification IS the output** (e.g. `soul`) may use generic `system(action="dismiss", channel=...)` or convenience aliases. **Category C — coalesced event summary** (e.g. `mcp.<server>`) may use generic dismiss after the agent has handled the summarized event. **Category D — protected source of truth** (currently `goal`) is allowlisted but refuses generic dismiss; to cancel/complete the goal, mutate or delete `.notification/goal.json` itself. The `system` channel is Category B with an extra atomic path: `system(action="dismiss", channel="system", event_id=...)` or `ref_id=...` removes only matching entries from `data.events`, preserving the file when other events remain. New producers declare the category and follow that contract.

External producers (MCP servers over SSH, separate processes) bypass the helper and write the same envelope directly to `<workdir>/.notification/mcp.<server>.json` using `tmp + rename`. The contract is the filesystem layout, not the Python API.

### Sync mechanism — `BaseAgent._sync_notifications`

Four pieces of state on `BaseAgent` (`base_agent/__init__.py:415-439`):
- `_notification_fp: tuple` — last-observed `(name, size, sha256)` triple-tuple from `notification_fingerprint`. Updated only on successful sync.
- `_notification_block_id: str | None` — informational `call_id` of the latest injected synthesized pair; retained for molt/reset telemetry, no longer used to delete pairs.
- `_notification_inject_seq: int` — monotonic injection counter so repeated notification payloads still produce unique synthetic pairs.
- `_notification_live_holder: dict | None` — the single current dict that carries live notification payload, skeletonized/stripped whenever payload moves.

The sync loop runs from **two trigger points**:
1. **Heartbeat tick** (`base_agent/lifecycle.py:328`) — `agent._sync_notifications()` after `_check_rules_file`. Default cadence is the heartbeat interval (~1s); the producer's `_wake_nap` calls also nudge the heartbeat for sub-second latency.
2. **Voluntary calls** — `system(action="notification")` (`intrinsics/system/__init__.py:92-115`) returns a placeholder dict (`_notification_placeholder: True` + explanatory message); the canonical `notifications` + `_notification_guidance` keys are stamped onto that same result by the ACTIVE-path `attach_active_notifications` post-hook, just like any other dict-shaped tool result. There is no separate "voluntary returns bare channel keys" code path — notifications surface only via the meta-block path so at most one live notification payload exists in history at any moment.

`_sync_notifications` (`base_agent/__init__.py:817`):
1. Compute fingerprint. If unchanged, return (cheap path — the common case).
2. On change, collect current notification files. If `notifications` is empty, skeletonize/strip the current live holder, commit the empty fingerprint, and return.
3. Otherwise, keep the old live holder intact until a new holder is successfully registered; this preserves the only live payload if injection is blocked by pending tool calls.
4. Otherwise, dispatch on agent state:
   - **IDLE** — `_inject_notification_pair` splices the synthetic `(ToolCallBlock, ToolResultBlock)` pair (impersonating a voluntary `system(action="notification")` call from the agent's perspective), then posts `MSG_TC_WAKE` and `_wake_nap("notification_sync")`. IDLE is "blocked on `inbox.get()`," so without a wake the loop sits forever and the pair never reaches the LLM. **Wake handler**: `_handle_tc_wake` (post-redesign) drives one inference round off the existing wire via `session.send(None)` — the adapter skips the input-append step and sends the canonical interface as-is. From the LLM's viewpoint the wake is indistinguishable from the agent voluntarily calling `system(action="notification")` and reacting to the result. No fake user message, no meta prefix. (The earlier wake-message draft posted `MSG_REQUEST(content=None)` to drive a meta-only turn through `_handle_request`; that was reverted because the meta line landed visibly in the agent's chat history every time a notification arrived. The "voluntary call" framing is cleaner.)
   - **ACTIVE** — after each tool-result batch, `attach_active_notifications` moves the canonical `notifications` + `_notification_guidance` payload to the latest dict-shaped tool result and strips/skeletonizes the previous holder (`base_agent/turn.py:1203-1226`, `meta_block.py:312-389`). If the batch has no dict result, the old holder stays live and the fingerprint remains uncommitted so IDLE delivery can retry later. **Post-molt special case:** `psyche(context, molt, ...)` publishes `.notification/post-molt.json` before its own tool result returns, so that same result batch deliberately skips ACTIVE notification stamping/fingerprint commit (`base_agent/turn.py:719-741`, `base_agent/turn.py:1203-1215`). This leaves the just-written post-molt state for the next boundary: if the agent becomes IDLE/ASLEEP, `_sync_notifications` injects the synthesized pair + `MSG_TC_WAKE`; if the agent continues ACTIVE, the next non-molt tool-result batch may consume/stamp the post-molt notification normally. The bug was the old behavior where the molt result itself stamped + fingerprinted post-molt, making the next IDLE pass see no change and never wake.
   - **ASLEEP** — clear `_asleep` and `_cancel_event`, transition `IDLE` (reason `notification_arrival`), `_reset_uptime`, then proceed exactly like the IDLE branch (inject pair + post `MSG_TC_WAKE` → `_handle_tc_wake` drives the wire). This is the canonical notification-driven wake. **Degraded fallback:** if `_inject_notification_pair` still returns False after `_heal_pending_tool_calls` (a wire shape heal cannot rescue), stay IDLE, enqueue one `MSG_REQUEST` from `system` naming the affected sources and pointing the agent at `system(action="notification")` or direct `.notification/` reads, log `notification_wake_degraded`, and commit the fingerprint so the failure does not replay until on-disk state changes. Reverting to ASLEEP without committing the fingerprint produced a livelock (heartbeat → wake → inject fail → revert → repeat); the degraded fallback unblocks the run loop instead.
   - **STUCK / SUSPENDED** — observe but don't inject. The on-disk state is captured; injection is deferred until state recovers.
5. Commit the new fingerprint **only if** a live holder was successfully updated (synthesized pair injected or canonical payload stamped onto a tool result), the state is empty / cannot inject (STUCK / SUSPENDED), or the ASLEEP degraded fallback fired (see ASLEEP bullet above — committing here is what breaks the heartbeat replay loop). If `_inject_notification_pair` returned False because `interface.has_pending_tool_calls()` (mid-pair tail), if ACTIVE stamping found no dict-shaped tool result, or if the active batch was the `psyche(context, molt, ...)` batch that just published `post-molt.json`, `_notification_fp` stays at its prior value and the next tick/boundary retries. Ordinary ACTIVE stamping commits the full current notification fingerprint for the payload it just attached.

### Why this beats the legacy `tc_inbox` queue

The previous design (queue of `InvoluntaryToolCall` items + pre-request drain hook + per-id dismiss path) carried four pain points:
1. **Stateful queue** — producers had to track whether their event was still queued vs already spliced (the `_dismiss` path branched on this).
2. **Per-arrival pairs** — every event got its own pair. A burst of arrivals during ASLEEP woke the agent N times with N pairs to dismiss.
3. **Two consumers** — `_drain_tc_inbox` was called from `_handle_request`, the pre-request hook, and `_handle_tc_wake`, with subtle ordering and idempotency requirements.
4. **No external-process producers** — only in-process Python could enqueue; MCP servers running over SSH had no path.

The filesystem-as-protocol redesign collapses all four into "write a file, read a fingerprint." Producers are stateless; agents observe current state, not arrival history; the kernel has one consumer (`_sync_notifications`); and any process that can write to the workdir is a valid producer.

### Voluntary `system(action="notification")` — read-your-mailbox path

Beyond kernel-driven sync, agents can call `system(action="notification")` themselves. The handler (`intrinsics/system/__init__.py:92-115`) returns a placeholder dict — `{"_notification_placeholder": True, "message": "…"}` — and never builds the bare channel collection itself. The turn-loop post-hook `attach_active_notifications` then stamps the canonical `notifications` + `_notification_guidance` payload onto that same result, exactly as it would for any other dict-shaped tool result. From the agent's perspective this is indistinguishable in shape from a kernel-synthesized pair (which additionally carries `_synthesized: True`). Useful when the agent wants to recheck producers without waiting for the next sync tick — and because the read path is unified with the meta-block stamp, there is never a duplicate representation of the same channels in one result.

### Migration window — `tc_inbox` is dormant, not deleted

Phase 2 (`d2da97e`) migrated all in-tree producers (mail, system events, soul flow) to `publish_notification`. Phase 2.5 migrated the LICC MCP inbox (`core/mcp/inbox.py`) to publish via `notifications.submit` to `.notification/mcp.<server>.json` instead of posting to the legacy inbox queue. The `tc_inbox.py` module survives as dead code — no producer enqueues, the drain hook is still installed but always finds the queue empty. Phase 3 (deferred to a separate point release after soak) will:
- Delete `tc_inbox.py`.
- Remove the pre-request drain hook from `BaseAgent._install_drain_hook` and the three drain call sites.
- Remove the legacy `ids=` soak path from `system(action="dismiss")`; `_dismiss` itself remains as the channel-level generic clear (`channel` + optional `force`).

### Adjacent: healing mid-pair tails

Distinct primitive (and unrelated to notifications) — `interface.close_pending_tool_calls(reason)` (`llm/interface.py:405`) synthesizes `tool_result` placeholders for orphan tool_calls when the wire chat itself ends mid-pair (process killed mid-turn, snapshot saved mid-turn). Marks them `synthesized=True`; if a real result arrives later for the same id, `add_tool_results` overwrites the placeholder so the wire stays honest. Used in `base_agent/turn.py:317-321, 419-423, 451-455, 889-893` after sleep/retry/continuation exceptions, and at snapshot save time in `intrinsics/psyche/_snapshots.py`. The notification path repurposes the same `synthesized=True` flag, but the two systems don't share code.

### Historical note: retired ACTIVE meta-prefix injection

Issue #82/#83 retired the ACTIVE-state `notifications:\n<json>\n\n` prefix path. Earlier code stashed notification JSON during ACTIVE and had `SessionManager.send()` prepend it onto the latest `ToolResultBlock`, which polluted unrelated tool output (e.g. daemon dispatch results) and invalidated strict-prefix caches. ACTIVE sync now leaves the fingerprint uncommitted and delivers only after the agent is observably IDLE via the same synthetic pair + `MSG_TC_WAKE` path used for ordinary notification wakeups.

## Composition

This file is the top of the kernel anatomy tree. Each subfolder below has its own `ANATOMY.md` — descend into the one that holds your question.

- [`base_agent/`](base_agent/ANATOMY.md) — `BaseAgent` class (the kernel coordinator). 7 submodules: identity, lifecycle, turn, soul_flow, tools, prompt, messaging.
- [`intrinsics/`](intrinsics/ANATOMY.md) — kernel-built-in tools. Four intrinsics: `system`, `psyche`, `soul`, `email`. Always present, never removable.
- [`llm/`](llm/ANATOMY.md) — LLM service ABC, adapter registry, chat interface, streaming protocol. Provider adapters live in the wrapper package, not here.
- [`services/`](services/ANATOMY.md) — kernel-side service implementations: filesystem mailbox (`mail.py`), JSONL event log plus additive SQLite query index (`logging.py`).
- [`migrate/`](migrate/ANATOMY.md) — versioned, append-only migrations for kernel-managed on-disk state. Preset-domain migrations are `m<NNN>_<name>.py`; agent-workdir/init migrations are `agent_m<NNN>_<name>.py`.
- [`i18n/`](i18n/ANATOMY.md) — three-locale message catalog (en / zh / wen). Loaded by `t(language, key)` in the intrinsics.

## State

The kernel only writes inside the agent's working directory (`<workdir>/`). Per-folder anatomy files name the specific files each subsystem writes; this root only catalogs the top-level layout:

- `history/chat_history.jsonl` — wire history (one line per role+content entry).
- `history/snapshots/` — periodic git-tracked snapshots.
- `system/` — kernel-managed durable state (pad, soul records, summaries, rules).
- `system/manifest.resolved.json` — derived runtime artifact: the fully-resolved (preset-materialized, validated, path-resolved, secret-redacted) manifest, regenerated by `workdir.py:write_resolved_manifest` on every boot/refresh/molt-reload. Safe to delete; consumers (TUI/portal) read it instead of re-resolving raw init.json (issue #259).
- `logs/events.jsonl` — structured event log (the JSONL service).
- `logs/token_ledger.jsonl` — per-call token usage.
- `mailbox/{inbox,outbox,sent}/` — filesystem mailbox.
- `tmp/tool-results/<timestamp>-<tool>-<id>-<uuid>.{json,txt}` — sidecar artifacts for oversized tool results. Two producers (`tool_result_artifacts.py:spill_oversized_result`): the preventive 10K cap in `ToolExecutor._build_result_message` (issue #144) writes a sidecar on every fresh result over `PREVENTIVE_MAX_CHARS=10_000`, and the retroactive 5K cap in `base_agent/turn.py:_compact_history_before_retry` (issue #144) rewrites already-committed history before AED retry over `RETROACTIVE_MAX_CHARS=5_000`. In both cases the wire keeps only a compact manifest stamped with the namespaced marker `artifact="lingtai_tool_result_spill"` (plus `status="spilled"`, `spill_path`, `spill_path_abs`, `source` ∈ {`preventive`, `retroactive`}, `cap_chars`, `original_char_count`, `preview`, …); the artifact holds the full original. `is_spill_manifest` requires the marker (or the legacy structural quadruple for forward-compat with manifests produced before the marker existed). Files are local to one agent run and may accumulate — not auto-cleaned today.
- `.notification/<tool>.json` — notification dropbox (one file per producer channel — `email.json`, `soul.json`, `system.json`, `mcp.<server>.json`). Polled by `BaseAgent._sync_notifications` on every heartbeat tick. See "Notifications" section above.
- `.agent.json`, `.agent.heartbeat`, `.status.json` — manifest, liveness signal, runtime snapshot.
- Signal files (`.prompt`, `.inquiry`, `.sleep`, `.suspend`, `.clear`, `.rules`) — consumed by `base_agent/lifecycle.py` heartbeat ticks.

## Notes

- **The anatomy tree is being populated.** Every existing subfolder anatomy is listed in Composition; deeper anatomies will appear as agents do work in those folders. When you do work in a folder that lacks one, write it before leaving — see the convention skill for the writing checklist.
