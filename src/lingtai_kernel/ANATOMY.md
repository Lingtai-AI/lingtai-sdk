# lingtai_kernel

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

The minimal agent runtime: turn loop, lifecycle, signal consumption, tool dispatch, intrinsic wiring, mailbox glue, soul/molt orchestration. The kernel is standalone — the wrapper package `lingtai` (at `src/lingtai/`) depends on it strictly one-directionally.

> **What is an `ANATOMY.md`?** See the canonical convention at `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`. This file follows the same 6-section template as every other anatomy in the tree.

## Components

The kernel root holds the coordinator (`base_agent/`) plus a flat collection of supporting modules. Most are self-contained leaves; subfolders are concept-boundary units with their own anatomy.

- `base_agent/` — `BaseAgent`, the kernel coordinator (package of 6 modules). `__init__.py` defines `BaseAgent` (~1481 lines: constructor, properties, state machine, hooks, cross-cutting stubs including the `.notification/` sync trio, pass-throughs to submodules). Submodules: `lifecycle.py` (start/stop/heartbeat/signals/refresh — heartbeat tick now also calls `_sync_notifications`), `turn.py` (main loop/message dispatch/AED/response processing), `tools.py` (tool schemas/dispatch/registry), `identity.py` (naming/manifest/status), `prompt.py` (system prompt building/flushing), `messaging.py` (mail/notification producers/outbound). Soul-flow domain logic lives in `intrinsics/soul/flow.py`. See `base_agent/ANATOMY.md`.
- `nudge/` — **per-agent periodic checks** that publish to the shared `.notification/nudge.json` channel when something needs the agent's attention. `__init__.py:run_checks(agent)` is called once per heartbeat tick from `base_agent/lifecycle.py:_heartbeat_loop` (~line 320, wrapped in try/except). Each check is a self-contained module under `nudge/` exposing `check(agent) -> None`; throttling and dedupe state live on the agent as `agent._nudge_<name>_state`. Checks call `nudge.upsert(agent, kind, body)` / `nudge.remove(agent, kind)` to mutate the multi-entry `nudge.json` payload under a lazy per-agent RMW lock. Today's only check is `kernel_version.py`: compares frozen `lingtai.__version__` against `importlib.metadata.version("lingtai")` to detect a newer wheel on disk and nudges the agent to refresh. Agent dismisses everything at once with `system(action='dismiss', channel='nudge')`. See `nudge/ANATOMY.md` for the "add a new nudge" recipe.
- `notifications.py` — **canonical `.notification/` filesystem helpers** (`fadbabf`/`dda7d8a`). `validate_channel_name()` / guard registry (`notifications.py:37-68`) validate generic-dismiss channel names and let Category-A producers guard unsafe clears. `notification_fingerprint(workdir)` (`notifications.py:71-89`) returns the `(name, mtime_ns, size)` triple-tuple used to detect change between heartbeat ticks. `collect_notifications(workdir)` (`notifications.py:92-113`) reads every `.notification/*.json` into a dict keyed by stem. `publish(workdir, tool_name, payload)` (`notifications.py:116-131`) writes one file atomically (tmp+rename). `clear(workdir, tool_name)` (`notifications.py:134-145`) deletes one file (idempotent). `clear_with_result()` / `dismiss_channel()` (`notifications.py:148-265`) are the strict agent-facing generic dismiss path. `submit(workdir, tool_name, *, data, header, icon, priority, instructions=None)` is the canonical producer-facing helper — wraps `publish` with the standard envelope. The `system` intrinsic re-exports `submit` as `publish_notification` and `clear` as `clear_notification`.
- `session.py` — `SessionManager`. LLM session lifecycle, token bookkeeping, chat history persistence, AED (auto-error-recovery) retry path. `send()` owns prompt/tool refresh, health checks, LLM dispatch, and usage accounting; notification delivery stays in `BaseAgent._sync_notifications` so session sends never mutate unrelated tool-result content.
- `tool_executor.py` — `ToolExecutor`. Synchronous/parallel tool dispatch, reasoning stripping, timing, error capture, and the privileged reserved `secondary` communication side channel: `_validate_secondary` enforces the communication whitelist (send/reply/read), action policy, and recursive-key ban; `_execute_secondary` rejects excluded primary tools at runtime, then dispatches valid communication calls mechanically before the primary, bypassing LoopGuard for the secondary itself while never blocking the primary; `_execute_single` pops `secondary` before the primary LoopGuard key and reports the outcome under the top-level `_secondary` field — for `action="read"` it also forwards a bounded slice of the read payload under `_secondary.result` (capped by `SECONDARY_READ_RESULT_MAX_BYTES`); send/reply expose only a confirmation summary so handler internals (e.g. message_ids) never leak. `_execute_parallel` runs all allowed secondaries in a sequential pre-pass before worker threads to avoid concurrent human-message fan-out.
- `secondary_tools.py` — policy and schema helpers for reserved nested `secondary` calls: communication-tool whitelist (`email`/`telegram`/`wechat`/`feishu`), action whitelist (`send`/`reply`/`read` per tool), primary-tool eligibility exclusions, read-result cap constant, and the injected JSON-schema property (`secondary_tools.py:15-100`).
- `tool_timing.py` — small helper for tool execution timing records.
- `tc_inbox.py` — `TCInbox` and `InvoluntaryToolCall`. **Legacy queue retained but dormant** under the `.notification/` redesign. Phase 3 will remove this module entirely; meanwhile the producer pipeline writes filesystem files instead of enqueuing. The molt path still calls `agent._tc_inbox.drain()` defensively to clear any pre-redesign items that survived a process restart.
- `prompt.py` — `SystemPromptManager` plus `build_system_prompt` / `build_system_prompt_batches`. Composes the system prompt from identity, capabilities, intrinsics, pad, rules. Default render order (`prompt.py:52-68`): principle → covenant → tools → **substrate** → procedures → comment, then rules → brief → skills → knowledge → identity → **character** → pad. `substrate` sits **right after** `tools` so it provides a compact resident operating model and routes expanded runtime/procedure guidance to the `system-manual` skill. `character` (the agent's self-authored identity from `system/lingtai.md`, written by `_lingtai_load`) renders right after the mechanical `identity` section and before `pad` — distinct from both `covenant` (operator contract, Batch 1) and `identity` (name/nickname/manifest). The kernel ships `lingtai/prompts/substrate.md` as the packaged compact default (issue #39) and `src/lingtai/intrinsic_skills/system-manual/SKILL.md` as the expanded manual; the `Agent` subclass auto-seeds `system/substrate.md` from it on first boot — no init.json opt-in required.
- `meta_block.py` — meta-block rendering (the structured prefix the kernel injects into LLM messages with state, time, stamina, etc.).
- `message.py` — `_make_message`, message-type sentinels (`MSG_REQUEST`, `MSG_TC_WAKE`). The wire format for the agent's inbox queue.
- `state.py` — `AgentState` enum (ACTIVE / IDLE / STUCK / ASLEEP / SUSPENDED).
- `config.py` — `AgentConfig` dataclass. Constructor-time options (stamina, soul cadences, max RPM, etc.).
- `workdir.py` — `WorkingDir`. Filesystem layout under the agent's working directory; manifest read/write; git operations.
- `handshake.py` — agent-discovery primitives (`is_agent`, `is_alive`) used by the TUI/portal to scan `.lingtai/` directories.
- `token_counter.py` — token counting helper (used for diary-cue cap, system prompt sizing).
- `token_ledger.py` — append-only per-call token usage log (`logs/token_ledger.jsonl`).
- `time_veil.py` — coarse-time rendering for state-aware prompts.
- `loop_guard.py` — guard against infinite tool loops.
- `logging.py` — logger configuration (separate from the `services/logging.py` event-log service).
- `llm_utils.py` — small shared helpers used by adapter implementations.
- `types.py` — shared type aliases.

## Connections

- **Kernel must never import from the wrapper.** `lingtai_kernel` is standalone; `lingtai` (the wrapper at `src/lingtai/`) depends on it strictly one-directionally.
- The kernel exposes its public surface through `__init__.py`. Anything not re-exported there is implementation detail.
- The wrapper layer registers LLM adapters into `llm.service` at import time, registers capabilities into `Agent` (which subclasses `BaseAgent`), and provides MCP, FileIO, Vision, Search, and the CLI.

## Notifications — the `.notification/` filesystem-as-protocol

Out-of-band events — mail arrival, soul-flow firings, daemon emanations, MCP webhook events, kernel-internal alerts — surface as one **live notification payload holder** at a time. When the agent is IDLE/ASLEEP, the holder is a synthetic `(ToolCallBlock, ToolResultBlock)` pair of shape `system(action="notification")` whose result carries the canonical JSON union of all currently-active producer files. When the agent is ACTIVE and has just produced an ordinary dict-shaped tool result, the holder is the same canonical `notifications` + `_notification_guidance` payload attached to that latest result (`meta_block.py:164-390`, `base_agent/turn.py:926-937`). Older holders remain in history only as skeletons/placeholders.

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

The canonical live payload is built by `meta_block.build_notification_payload` (`meta_block.py:164-205`) and used by both delivery surfaces. The synthesized-pair envelope comes from `BaseAgent._inject_notification_pair` (`base_agent/__init__.py:1019-1184`): it adds `_synthesized: True` (also written as the `synthesized=True` flag on the `ToolResultBlock`) around the canonical payload so the agent can distinguish kernel-injected reads from voluntary `system(action="notification")` calls when reading conversation history. The kernel adds a top-level `_notification_guidance` field plus a source-specific `_notification_guidance` field into each per-source block under `notifications` — this is kernel safety framing, separate from the producer's own optional `instructions` field (see Producer contract below). ACTIVE tool-result delivery calls `meta_block.attach_active_notifications` (`meta_block.py:308-390`, wired from `base_agent/turn.py:926-937`), which attaches that same canonical payload to the latest dict-shaped tool result; it does not build a compact/preview-only representation.

### Communication responsiveness and secondary tool calls

Notification previews deliberately trade completeness for latency: they let the agent notice that something arrived, not necessarily act safely from the preview alone. The operational rule lives in the substrate: answer directly only when the newest human message is complete and unambiguous; otherwise read the producer. The reserved `secondary` channel is the runtime bridge between those rules and long primary work. Eligible primary tool schemas receive a narrow nested communication-call property from `secondary_tools.py` (`secondary_tools.py:18-30`, `secondary_tools.py:66-100`): only `email`/`telegram`/`wechat`/`feishu`, only `send`/`reply`/`read`, no nested `secondary`. `ToolExecutor._validate_secondary` enforces the whitelist and recursive-key ban (`tool_executor.py:134-198`), `_execute_secondary` runs the communication call before the primary while catching all failures (`tool_executor.py:200-317`), and `_execute_single` / `_execute_parallel` attach the outcome to the primary result under `_secondary` (`tool_executor.py:400-420`, `tool_executor.py:568-609`). For `action="read"`, the executor forwards only a bounded slice under `_secondary.result`; `send`/`reply` expose a lean status summary so handler internals such as message IDs do not leak (`tool_executor.py:272-284`). This is intentionally not a general nested-workflow engine: it exists to reply promptly or fetch full message content without abandoning the primary task.

### Filesystem layout

Producers write a JSON file per channel into `<workdir>/.notification/`:

| File | Owner | Naming convention |
|---|---|---|
| `email.json` | `intrinsics/email` (unread digest, `_rerender_unread_digest`) | bare intrinsic name |
| `soul.json` | `intrinsics/soul/flow.py` (consultation fire) | bare intrinsic name |
| `system.json` | `base_agent/messaging.py:_enqueue_system_notification` (events list, max 20 newest) | bare intrinsic name |
| `mcp.<server>.json` | external MCP server adapter (e.g. `mcp.imap.json`, `mcp.telegram.json`) | dotted prefix |

Each file is the producer's complete state for that channel — there is no "queue of unread events." When the producer's state empties (e.g. unread count drops to 0), it deletes the file. The basename (without `.json`) becomes the dict key the agent sees in `notifications`.

### Single-slot wire invariant

At most ONE live notification payload exists in the wire history at any time. The live holder is recorded as `agent._notification_live_holder` (`base_agent/__init__.py:430-439`) and may be either a synthesized notification result dict or a normal tool-result content dict. When payload moves, `meta_block.skeletonize_notification_holder` (`meta_block.py:262-295`) strips `notifications`/guidance (and the retired `_notifications` upgrade-cleanup key) from the old normal result or replaces an old synthesized result with a skeleton placeholder. Historical synthesized pairs are therefore preserved for chronology, but only the newest holder contains actionable notification data. Agents observe the **current** notification state, not a history of arrivals. Past arrivals belong in the producer's own logs (e.g. `mailbox/inbox/`, `logs/soul_flow.jsonl`), not in live payload history.

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

**Dismissal contract.** Producers fall into three categories. **Category A — mirror over real producer state** (e.g. `email` over `read.json`) MUST register with `register_generic_dismiss_guard("<channel>", "<suggested verb>")`; generic clearing would leave producer state unchanged and the mirror inaccurate. **Category B — notification IS the output** (e.g. `soul`, `system`) may use generic `system(action="dismiss", channel=...)` or convenience aliases. **Category C — coalesced event summary** (e.g. `mcp.<server>`) may use generic dismiss after the agent has handled the summarized event. New producers declare the category and follow that contract.

External producers (MCP servers over SSH, separate processes) bypass the helper and write the same envelope directly to `<workdir>/.notification/mcp.<server>.json` using `tmp + rename`. The contract is the filesystem layout, not the Python API.

### Sync mechanism — `BaseAgent._sync_notifications`

Four pieces of state on `BaseAgent` (`base_agent/__init__.py:415-439`):
- `_notification_fp: tuple` — last-observed `(name, mtime_ns, size)` triple-tuple from `notification_fingerprint`. Updated only on successful sync.
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
   - **ACTIVE** — after each tool-result batch, `attach_active_notifications` moves the canonical `notifications` + `_notification_guidance` payload to the latest dict-shaped tool result and strips/skeletonizes the previous holder (`base_agent/turn.py:926-937`, `meta_block.py:308-390`). If the batch has no dict result, the old holder stays live and the fingerprint remains uncommitted so IDLE delivery can retry later.
   - **ASLEEP** — clear `_asleep` and `_cancel_event`, transition `IDLE` (reason `notification_arrival`), `_reset_uptime`, then proceed exactly like the IDLE branch (inject pair + post `MSG_TC_WAKE` → `_handle_tc_wake` drives the wire). This is the canonical notification-driven wake. **Degraded fallback:** if `_inject_notification_pair` still returns False after `_heal_pending_tool_calls` (a wire shape heal cannot rescue), stay IDLE, enqueue one `MSG_REQUEST` from `system` naming the affected sources and pointing the agent at `system(action="notification")` or direct `.notification/` reads, log `notification_wake_degraded`, and commit the fingerprint so the failure does not replay until on-disk state changes. Reverting to ASLEEP without committing the fingerprint produced a livelock (heartbeat → wake → inject fail → revert → repeat); the degraded fallback unblocks the run loop instead.
   - **STUCK / SUSPENDED** — observe but don't inject. The on-disk state is captured; injection is deferred until state recovers.
5. Commit the new fingerprint **only if** a live holder was successfully updated (synthesized pair injected or canonical payload stamped onto a tool result), the state is empty / cannot inject (STUCK / SUSPENDED), or the ASLEEP degraded fallback fired (see ASLEEP bullet above — committing here is what breaks the heartbeat replay loop). If `_inject_notification_pair` returned False because `interface.has_pending_tool_calls()` (mid-pair tail), or if ACTIVE stamping found no dict-shaped tool result, `_notification_fp` stays at its prior value and the next tick/boundary retries.

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
- [`services/`](services/ANATOMY.md) — kernel-side service implementations: filesystem mailbox (`mail.py`), JSONL event log (`logging.py`).
- [`migrate/`](migrate/ANATOMY.md) — versioned, append-only migrations for kernel-managed on-disk state. Each migration is `m<NNN>_<name>.py`.
- [`i18n/`](i18n/ANATOMY.md) — three-locale message catalog (en / zh / wen). Loaded by `t(language, key)` in the intrinsics.

## State

The kernel only writes inside the agent's working directory (`<workdir>/`). Per-folder anatomy files name the specific files each subsystem writes; this root only catalogs the top-level layout:

- `history/chat_history.jsonl` — wire history (one line per role+content entry).
- `history/snapshots/` — periodic git-tracked snapshots.
- `system/` — kernel-managed durable state (pad, soul records, summaries, rules).
- `logs/events.jsonl` — structured event log (the JSONL service).
- `logs/token_ledger.jsonl` — per-call token usage.
- `mailbox/{inbox,outbox,sent}/` — filesystem mailbox.
- `tmp/tool-results/<timestamp>-<tool>-<id>-<uuid>.{json,txt}` — sidecar artifacts for oversized tool results. Two producers (`tool_result_artifacts.py:spill_oversized_result`): the preventive 10K cap in `ToolExecutor._build_result_message` (issue #144) writes a sidecar on every fresh result over `PREVENTIVE_MAX_CHARS=10_000`, and the retroactive 5K cap in `base_agent/turn.py:_compact_history_before_retry` (issue #144) rewrites already-committed history before AED retry over `RETROACTIVE_MAX_CHARS=5_000`. In both cases the wire keeps only a compact manifest stamped with the namespaced marker `artifact="lingtai_tool_result_spill"` (plus `status="spilled"`, `spill_path`, `spill_path_abs`, `source` ∈ {`preventive`, `retroactive`}, `cap_chars`, `original_char_count`, `preview`, …); the artifact holds the full original. `is_spill_manifest` requires the marker (or the legacy structural quadruple for forward-compat with manifests produced before the marker existed). Files are local to one agent run and may accumulate — not auto-cleaned today.
- `.notification/<tool>.json` — notification dropbox (one file per producer channel — `email.json`, `soul.json`, `system.json`, `mcp.<server>.json`). Polled by `BaseAgent._sync_notifications` on every heartbeat tick. See "Notifications" section above.
- `.agent.json`, `.agent.heartbeat`, `.status.json` — manifest, liveness signal, runtime snapshot.
- Signal files (`.prompt`, `.inquiry`, `.sleep`, `.suspend`, `.clear`, `.rules`) — consumed by `base_agent/lifecycle.py` heartbeat ticks.

## Notes

- **The anatomy tree is being populated.** Every existing subfolder anatomy is listed in Composition; deeper anatomies will appear as agents do work in those folders. When you do work in a folder that lacks one, write it before leaving — see the convention skill for the writing checklist.
