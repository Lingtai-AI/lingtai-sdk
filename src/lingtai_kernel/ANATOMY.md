# lingtai_kernel

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

The minimal agent runtime: turn loop, lifecycle, signal consumption, tool dispatch, intrinsic wiring, mailbox glue, soul/molt orchestration. The kernel is standalone — the wrapper package `lingtai` (at `src/lingtai/`) depends on it strictly one-directionally.

> **What is an `ANATOMY.md`?** See the canonical convention at `src/lingtai/intrinsic_skills/lingtai-kernel-anatomy/SKILL.md`. This file follows the same 6-section template as every other anatomy in the tree.

## Components

The kernel root holds the coordinator (`base_agent/`) plus a flat collection of supporting modules. Most are self-contained leaves; subfolders are concept-boundary units with their own anatomy.

- `base_agent/` — `BaseAgent`, the kernel coordinator (package of 6 modules). `__init__.py` defines `BaseAgent` (~1027 lines: constructor, properties, state machine, hooks, cross-cutting stubs including the `.notification/` sync trio, pass-throughs to submodules). Submodules: `lifecycle.py` (start/stop/heartbeat/signals/refresh — heartbeat tick now also calls `_sync_notifications`), `turn.py` (main loop/message dispatch/AED/response processing), `tools.py` (tool schemas/dispatch/registry), `identity.py` (naming/manifest/status), `prompt.py` (system prompt building/flushing), `messaging.py` (mail/notification producers/outbound). Soul-flow domain logic lives in `intrinsics/soul/flow.py`. See `base_agent/ANATOMY.md`.
- `notifications.py` — **canonical `.notification/` filesystem helpers** (`fadbabf`/`dda7d8a`). `notification_fingerprint(workdir)` returns the `(name, mtime_ns, size)` triple-tuple used to detect change between heartbeat ticks. `collect_notifications(workdir)` reads every `.notification/*.json` into a dict keyed by stem. `publish(workdir, tool_name, payload)` writes one file atomically (tmp+rename). `clear(workdir, tool_name)` deletes one file (idempotent). `submit(workdir, tool_name, *, data, header, icon, priority, instructions=None)` is the canonical producer-facing helper — wraps `publish` with the standard envelope (header/icon/priority/published_at/data, plus optional top-level `instructions` when the producer supplies one) so producers only supply what is semantically theirs. The `system` intrinsic re-exports `submit` as `publish_notification` and `clear` as `clear_notification`.
- `session.py` — `SessionManager`. LLM session lifecycle, token bookkeeping, chat history persistence, AED (auto-error-recovery) retry path. Accepts a `notification_inject_fn` callback (wired to `BaseAgent._inject_notification_meta` at construction); called after `_health_check` in `send()` so ACTIVE-state notification meta is prepended to the latest string-content `ToolResultBlock` before the API call goes out.
- `tool_executor.py` — `ToolExecutor`. Synchronous tool dispatch, reasoning-parameter injection, timing, error capture.
- `tool_timing.py` — small helper for tool execution timing records.
- `tc_inbox.py` — `TCInbox` and `InvoluntaryToolCall`. **Legacy queue retained but dormant** under the `.notification/` redesign. Phase 3 will remove this module entirely; meanwhile the producer pipeline writes filesystem files instead of enqueuing. The molt path still calls `agent._tc_inbox.drain()` defensively to clear any pre-redesign items that survived a process restart.
- `prompt.py` — `SystemPromptManager` plus `build_system_prompt` / `build_system_prompt_batches`. Composes the system prompt from identity, capabilities, intrinsics, pad, rules. Default render order (`prompt.py:46-61`): principle → covenant → tools → **substrate** → procedures → comment, then rules → brief → library → codex → identity → pad. `substrate` sits **right after** `tools` so it functions as the long-form companion to the schemas above it (operational wisdom that spans tools — tool tiers, life states, channel discipline, etc.). The kernel ships `lingtai/prompts/substrate.md` as the packaged default (v1, issue #39); the `Agent` subclass auto-seeds `system/substrate.md` from it on first boot — no init.json opt-in required.
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

Out-of-band events — mail arrival, soul-flow firings, daemon emanations, MCP webhook events, kernel-internal alerts — surface in the agent's wire chat as **a single synthetic `(ToolCallBlock, ToolResultBlock)` pair** of shape `system(action="notification")` whose result is the JSON-serialized union of all currently-active producer files. The LLM has no native concept of "the world poked the agent," so the kernel masquerades the external state as a tool the agent appears to have called.

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

The shape comes from `BaseAgent._inject_notification_pair` (`base_agent/__init__.py:879`). The `_synthesized: True` envelope marker (also written as the `synthesized=True` flag on the `ToolResultBlock`) lets the agent distinguish kernel-injected reads from voluntary `system(action="notification")` calls when reading conversation history.

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

At most ONE `system(action="notification")` pair lives in the wire history at any time. When the kernel detects a fingerprint change, it strips the prior pair (recording its `call_id` in `agent._notification_block_id`) and either reinjects a fresh pair or — if all producer files vanished — leaves the wire empty. Agents observe the **current** notification state, not a history of arrivals. Past arrivals belong in the producer's own logs (e.g. `mailbox/inbox/`, `logs/soul_flow.jsonl`), not in the wire.

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

The optional `instructions` field is the producer-side directive — text describing what the agent must do to dismiss or act on the notification. It rides with the payload so each producer owns its own dismissal contract; generic frontend / kernel code does not need to know per-producer rules. Email uses it for "call read or dismiss to clear"; soul flow leaves it unset (voices are advisory, no dismissal needed); MCP servers can carry their own.

External producers (MCP servers over SSH, separate processes) bypass the helper and write the same envelope directly to `<workdir>/.notification/mcp.<server>.json` using `tmp + rename`. The contract is the filesystem layout, not the Python API.

### Sync mechanism — `BaseAgent._sync_notifications`

Three pieces of state on `BaseAgent` (`base_agent/__init__.py:366-372`):
- `_notification_fp: tuple` — last-observed `(name, mtime_ns, size)` triple-tuple from `notification_fingerprint`. Updated only on successful sync.
- `_notification_block_id: str | None` — `call_id` of the currently-injected wire pair, or `None` if no pair is in the wire.
- `_pending_notification_meta: str | None` — JSON body stashed during ACTIVE state for the next `SessionManager.send()` to prepend onto the latest tool result.

The sync loop runs from **two trigger points**:
1. **Heartbeat tick** (`base_agent/lifecycle.py:334`) — `agent._sync_notifications()` after `_check_rules_file`. Default cadence is the heartbeat interval (~1s); the producer's `_wake_nap` calls also nudge the heartbeat for sub-second latency.
2. **Voluntary calls** — `system(action="notification")` (`intrinsics/system/__init__.py:97`) returns `collect_notifications(workdir)` directly. Reading is always free; the agent can poll its own notification state any time without touching the wire.

`_sync_notifications` (`base_agent/__init__.py:761`):
1. Compute fingerprint. If unchanged, return (cheap path — the common case).
2. On change, strip the prior wire pair via `interface.remove_pair_by_call_id(prior_block_id)`.
3. If `notifications` is empty, the wire is now clean — commit the new (empty) fingerprint and return.
4. Otherwise, dispatch on agent state:
   - **IDLE** — `_inject_notification_pair` splices the synthetic `(ToolCallBlock, ToolResultBlock)` pair (impersonating a voluntary `system(action="notification")` call from the agent's perspective), then posts `MSG_TC_WAKE` and `_wake_nap("notification_sync")`. IDLE is "blocked on `inbox.get()`," so without a wake the loop sits forever and the pair never reaches the LLM. **Wake handler**: `_handle_tc_wake` (post-redesign) drives one inference round off the existing wire via `session.send(None)` — the adapter skips the input-append step and sends the canonical interface as-is. From the LLM's viewpoint the wake is indistinguishable from the agent voluntarily calling `system(action="notification")` and reacting to the result. No fake user message, no meta prefix. (The earlier wake-message draft posted `MSG_REQUEST(content=None)` to drive a meta-only turn through `_handle_request`; that was reverted because the meta line landed visibly in the agent's chat history every time a notification arrived. The "voluntary call" framing is cleaner.)
   - **ACTIVE** — stash the JSON body in `_pending_notification_meta`. The agent's run loop is between request boundaries; injecting a `(call, result)` pair now would race the in-flight tool round. Instead, the SessionManager's `notification_inject_fn` callback prepends `notifications:\n<json>\n\n` onto the latest string-content `ToolResultBlock` at request-send time, after stripping any stale prefix from older blocks. Dict-content `ToolResultBlock`s (MCP structured results) are skipped to preserve their schema.
   - **ASLEEP** — clear `_asleep` and `_cancel_event`, transition `IDLE` (reason `notification_arrival`), `_reset_uptime`, then proceed exactly like the IDLE branch (inject pair + post `MSG_TC_WAKE` → `_handle_tc_wake` drives the wire). This is the canonical notification-driven wake.
   - **STUCK / SUSPENDED** — observe but don't inject. The on-disk state is captured; injection is deferred until state recovers.
5. Commit the new fingerprint **only if injection succeeded** (or the state cannot inject — STUCK/SUSPENDED/empty). If `_inject_notification_pair` returned False because `interface.has_pending_tool_calls()` (mid-pair tail), `_notification_fp` stays at its prior value and the next heartbeat tick retries.

### Why this beats the legacy `tc_inbox` queue

The previous design (queue of `InvoluntaryToolCall` items + pre-request drain hook + per-id dismiss path) carried four pain points:
1. **Stateful queue** — producers had to track whether their event was still queued vs already spliced (the `_dismiss` path branched on this).
2. **Per-arrival pairs** — every event got its own pair. A burst of arrivals during ASLEEP woke the agent N times with N pairs to dismiss.
3. **Two consumers** — `_drain_tc_inbox` was called from `_handle_request`, the pre-request hook, and `_handle_tc_wake`, with subtle ordering and idempotency requirements.
4. **No external-process producers** — only in-process Python could enqueue; MCP servers running over SSH had no path.

The filesystem-as-protocol redesign collapses all four into "write a file, read a fingerprint." Producers are stateless; agents observe current state, not arrival history; the kernel has one consumer (`_sync_notifications`); and any process that can write to the workdir is a valid producer.

### Voluntary `system(action="notification")` — read-your-mailbox path

Beyond kernel-driven sync, agents can call `system(action="notification")` themselves. The handler (`intrinsics/system/__init__.py:97`) returns the bare `collect_notifications(workdir)` dict — no `_synthesized` envelope, since the call wasn't synthesized. Useful when the agent wants to recheck producers without waiting for the next sync tick.

### Migration window — `tc_inbox` is dormant, not deleted

Phase 2 (`d2da97e`) migrated all in-tree producers (mail, system events, soul flow) to `publish_notification`. Phase 2.5 migrated the LICC MCP inbox (`core/mcp/inbox.py`) to publish via `notifications.submit` to `.notification/mcp.<server>.json` instead of posting to the legacy inbox queue. The `tc_inbox.py` module survives as dead code — no producer enqueues, the drain hook is still installed but always finds the queue empty. Phase 3 (deferred to a separate point release after soak) will:
- Delete `tc_inbox.py`.
- Remove the pre-request drain hook from `BaseAgent._install_drain_hook` and the three drain call sites.
- Remove the `_dismiss` deprecation no-op (currently still answers `system(action="dismiss")` with `{"status":"ok","note":"...deprecated..."}` for in-flight chat histories).

### Adjacent: healing mid-pair tails

Distinct primitive (and unrelated to notifications) — `interface.close_pending_tool_calls(reason)` (`llm/interface.py:344`) synthesizes `tool_result` placeholders for orphan tool_calls when the wire chat itself ends mid-pair (process killed mid-turn, snapshot saved mid-turn). Marks them `synthesized=True`; if a real result arrives later for the same id, `add_tool_results` overwrites the placeholder so the wire stays honest. Used in `base_agent/turn.py:202, 446, 461` after exceptions, and at snapshot save time in `intrinsics/psyche/_snapshots.py`. The notification path repurposes the same `synthesized=True` flag, but the two systems don't share code.

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
- `.notification/<tool>.json` — notification dropbox (one file per producer channel — `email.json`, `soul.json`, `system.json`, `mcp.<server>.json`). Polled by `BaseAgent._sync_notifications` on every heartbeat tick. See "Notifications" section above.
- `.agent.json`, `.agent.heartbeat`, `.status.json` — manifest, liveness signal, runtime snapshot.
- Signal files (`.prompt`, `.inquiry`, `.sleep`, `.suspend`, `.clear`, `.rules`) — consumed by `base_agent/lifecycle.py` heartbeat ticks.

## Notes

- **The anatomy tree is being populated.** Every existing subfolder anatomy is listed in Composition; deeper anatomies will appear as agents do work in those folders. When you do work in a folder that lacks one, write it before leaving — see the convention skill for the writing checklist.
