# Notification Redesign — `.notification/` filesystem dropbox + JSON passthrough

> **Status:** Design doc, pre-patch. Discussion 2026-05-05 between Zesen and Claude (claude-opus-4-7[1m]).
> **Supersedes:** `envelope-redesign.md` (the intermediate envelope-registry design — kept as a historical record of the design path).
> **Predecessors:** `system-injection-audit.md` (the seven injection paths catalogue), `tc-injection-service-implementation-proposal.md` (intermediate refactor toward `TCInbox.drain_into`), `email-unread-digest-notification-patch.md` (the prototype that proved the single-slot replace pattern), `tc-inbox-mid-turn-drain-patch.md` (the `pre_request_hook` this design reverts).
> **Goal:** Replace the `tc_inbox` queue + `pre_request_hook` machinery with a `.notification/<source>.json` filesystem dropbox + JSON passthrough to the agent + frontends-as-additional-consumers.

## 1. Why

The current tc_inbox machinery solves real problems but at the wrong abstraction level. Across the prior design conversation we identified five concrete failure modes; this section names them and traces the root cause to a single architectural mistake.

### 1.1 The failure modes

1. **Mid-turn splice lands inside coherent reasoning.** `pre_request_hook` (`f46b346`) earns a *wire-legal* splice point — `has_pending_tool_calls()` is False after a tool-result lands, so an `(assistant[tool_call], user[tool_result])` pair can be appended legally. But "wire-legal" and "agent-coherent" are different things. The model is mid-thought; splicing a soul-flow voice between bash result N and bash call N+1 is a non-sequitur the agent has to reconcile mid-task.

2. **Token cost inflates within a turn.** Anything spliced via `pre_request_hook` enters `interface.entries` and re-serializes on every subsequent API call in the turn. A single soul-flow voice queued at tool-call 2 of a 12-call chain ships in 11 of those API calls.

3. **`replace_in_history=True` mid-turn corrupts citations.** Wholesale `remove_pair_by_call_id` on the prior pair → if the model was composing a sentence quoting it, the citation now references nothing. Today flagged as a "consideration, harmless because soul flow doesn't drive tool calls" (`base_agent/__init__.py:669-684`) — a brittle invariant that depends on every future producer staying disciplined.

4. **Two regimes diverge.** Canonical-interface adapters (Anthropic, OpenAI Chat, Codex Responses, DeepSeek) deliver the spliced pair in the *current* request. Server-state adapters (`OpenAIResponsesSession`, `GeminiChatSession`, `InteractionsChatSession`) only deliver on the *next* turn. Same producer code, different latency, depending on plumbing the producer shouldn't have to know about.

5. **Producer fragility.** Producers need Python access to `agent._tc_inbox.enqueue` or `agent._enqueue_system_notification`. External producers (MCP servers, shell hooks, future tooling) have no clean entry point. Session-replacing paths (rebuild, recovery, molt) all need to either route through `_drain_tc_inbox` or remember to re-install the hook.

### 1.2 The root cause

`tc_inbox` is shaped like an event queue but most of its callers want **status semantics**. The `email.unread` producer (`base_agent/messaging.py:51`) discovered this empirically — it uses `coalesce=True, replace_in_history=True` with `_render_unread_digest()` to render the *current state of the inbox* as the result body, not per-message events. Soul flow does the same. System notifications were the holdout, accumulating per-event slots.

Beyond status-vs-events, there's a deeper mismatch: `tc_inbox` is also shaped as an **internal Python API**, while the kernel's most successful producer pattern is the **filesystem signal-file pattern** (`.prompt`, `.inquiry`, `.sleep`, `.suspend`, `.clear`, `.rules` — see `lingtai_kernel/ANATOMY.md` §State). Signal files are external-process-friendly, locale-agnostic, multi-consumer-friendly, and require no import of any kernel module. Notifications should follow the same pattern.

### 1.3 What this design commits to

A complete pivot, three moves that compose:

1. **Producers write `.notification/<source>.json` files in the agent's working directory.** No imports, no hooks, no thread-safe registries. Anything that can write a file is a producer.

2. **The agent calls `system(action="notification")`** — voluntarily or synthesized by the kernel — and gets back the **current contents of `.notification/`** as a JSON object. Voluntary calls and kernel-synthesized calls are *literally indistinguishable* and that's fine because the tool description tells the agent the kernel can synthesize this call.

3. **The agent and frontends are co-equal consumers.** The kernel does not render. Producers publish structured data; the agent reads it as JSON in tool-result content; the TUI and portal each render it for their own surface. The kernel is a transparent passthrough.

The result: the kernel removes more code than it adds, the producer contract becomes "write a file," and presentation moves out of the kernel entirely.

## 2. Design

### 2.1 The producer contract

A producer publishes a notification by writing a JSON file:

```
<workdir>/.notification/<tool_name>.json          # for kernel intrinsics
<workdir>/.notification/mcp.<server_name>.json    # for MCP-loaded tools
```

The file's basename is the *tool* whose namespace owns this notification. Intrinsics (`email`, `soul`, `system`) write bare `<tool_name>.json`. MCP servers (telegram, feishu, wechat, imap, …) write `mcp.<server_name>.json` — the `mcp.` prefix mirrors how MCP-loaded tools are namespaced everywhere else in the kernel, so no new ontology is introduced.

This is the kernel's **universal async-info-injection surface**. Anything a tool wants to surface to the agent asynchronously — mail digests, soul-flow voices, scheduled wakes, MCP server events, future tooling — flows through this single mechanism. Producers don't import any kernel module; they write a file. The mailbox, signal files, and `.agent.json` already establish this filesystem-as-protocol pattern; notifications slot into it.

The file content is whatever JSON the producer wants the agent to read. The producer deletes the file when there's nothing to notify.

That's the entire contract. No required schema, no required fields, no header/body distinction, no `notif_id` lifecycle. The file *is* the current state of the channel.

#### 2.1.1 Atomicity

Producers should write to `<source>.json.tmp` and rename, not directly to `<source>.json`. The kernel poller can otherwise read mid-write and see malformed JSON. The poller handles `JSONDecodeError` gracefully (skip and try next tick), but tmp+rename is the right hygiene.

#### 2.1.2 Replace-only

Writing to `<source>.json` overwrites whatever was there. There is no append, no queue, no per-event slot. If a producer represents multiple distinct events (e.g. mail with several unread messages), the producer renders cumulative state into one file:

```json
{
  "count": 3,
  "previews": [
    {"from": "Zesen", "subject": "...", "received_at": "2026-05-05T14:21:00Z", "snippet": "..."},
    {"from": "Marco", "subject": "...", "received_at": "2026-05-05T14:03:00Z", "snippet": "..."},
    {"from": "soul-bot", "subject": "...", "received_at": "2026-05-05T13:18:00Z", "snippet": "..."}
  ],
  "newest_received_at": "2026-05-05T14:21:00Z"
}
```

The producer is responsible for re-rendering its file whenever its state changes. This is the same pattern `email.unread` uses today (`_render_unread_digest`), generalized.

#### 2.1.3 Suggested-but-not-required envelope shape

Frontends benefit from optional metadata fields the kernel does not enforce. Producers that include them get richer rendering in TUI/portal for free; producers that omit them get a generic fallback render.

```json
{
  "header": "3 unread emails",       // optional: one-line summary for compact UI
  "icon": "📧",                      // optional: glyph for status indicators
  "priority": "normal",              // optional: "low" | "normal" | "high"
  "published_at": "2026-05-05T...",  // optional but recommended (frontends use for age)
  "data": { ...producer payload... } // the structured content
}
```

The agent ignores `header`/`icon`/`priority` and reads `data` directly. The TUI uses `header` for status line, `icon` for indicators. The portal uses all four for card layout. Document this shape in `lingtai_kernel/ANATOMY.md` as the convention; producers adopt it because their notifications look better.

#### 2.1.4 Producer-side size discipline

The kernel does not enforce a size cap. Producers are responsible for keeping their payloads reasonable — soul flow caps the voice text, email caps the digest preview list, MCP servers truncate event bodies if needed. If a producer misbehaves (writes a multi-megabyte payload), the agent's context inflates and the producer's author sees the impact directly. This is the same discipline as today (`_render_unread_digest` already self-caps), promoted to a contract.

If runaway producers become a real problem, we add a kernel-side cap then. Until then, no preemptive guard.

### 2.2 The kernel's job — one sync mechanism

The kernel runs **one mechanism** for all notification delivery: a fingerprint-based sync that keeps the wire's notification block in agreement with `.notification/`. The placement of that block varies by agent state, but the mechanism does not.

#### 2.2.1 Fingerprint poll

On every heartbeat tick (already polling for `.prompt`, `.inquiry`, etc. — see `base_agent/lifecycle.py`), the kernel computes a fingerprint of `.notification/`:

```python
def notification_fingerprint(workdir: Path) -> tuple:
    notif_dir = workdir / ".notification"
    if not notif_dir.is_dir():
        return ()
    return tuple(sorted(
        (f.name, f.stat().st_mtime_ns, f.stat().st_size)
        for f in notif_dir.iterdir()
        if f.is_file() and f.suffix == ".json"
    ))
```

`(name, mtime_ns, size)` per file, sorted. Cheap, catches every real change (writes bump mtime; tmp+rename bumps mtime; deletion removes the entry; appearance adds one). `mtime_ns` rather than `mtime` so rapid producer writes don't collide on second-granularity filesystems.

The kernel keeps the last-seen fingerprint as instance state. **If the fingerprint matches, the wire already reflects the filesystem and nothing needs to change.** If it differs, a sync runs.

Cost: one `listdir` + a stat per file per tick. For <10 files this is microseconds. Heartbeat cadence (~1s) gives sub-second delivery latency, which is invisible for the use cases.

Producers that want sub-tick latency can call a kernel helper `agent._notification_changed()` that nudges the run loop — same pattern as today's `_wake_nap`. Optional fast path; polling is canonical.

#### 2.2.2 Collection

When sync runs, the kernel reads the directory:

```python
def collect_notifications(workdir: Path) -> dict:
    """Read .notification/*.json, return as dict keyed by tool basename.
    Returns {} if directory is absent or empty (or all files unparseable)."""
    notif_dir = workdir / ".notification"
    if not notif_dir.is_dir():
        return {}
    out = {}
    for f in sorted(notif_dir.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_bytes())
        except (json.JSONDecodeError, OSError):
            continue
    return out
```

Keys are filenames-without-extension (`email`, `soul`, `mcp.telegram`, `mcp.imap`, …) — the tool whose namespace owns each entry. Sorted iteration is deterministic so the agent sees a stable ordering across reads.

#### 2.2.3 Sync — strip + reinject

When the fingerprint changes, the kernel:

1. **Strips** any prior notification block from the wire (by tracking the entry id of the currently-injected block in `BaseAgent` state — one id, not a per-source map).
2. **If the new collection is empty** (all producers cleared their files): done. Wire now has zero notification blocks.
3. **If the new collection is non-empty**: injects a new block at the placement appropriate for the agent's current state (see §2.2.4). The newly-injected block's entry id is recorded for the next strip.

Result: at any moment the wire has *exactly zero or one* notification block, and that block's content (if present) matches the current `.notification/` snapshot.

#### 2.2.4 Three placement variants — same content, different shapes

Where the block lands depends on the agent's state when the sync runs:

**IDLE → splice as a pair.** The kernel synthesizes:

```
assistant: system(action="notification")   [synthetic call]
user:      <collected JSON dict>           [synthetic result]
```

Appends the pair to the wire and posts `MSG_TC_WAKE` to nudge the agent. This is the canonical "wake the agent because something arrived" path — same wake mechanism today's `_handle_tc_wake` provides, but content sourced from the filesystem.

**ACTIVE → meta on the most recent tool result.** When the agent is mid-tool-chain, the next outgoing API call's most recent `ToolResultBlock` is mutated to carry the notification dict as a prepended `notifications:\n<json>\n\n` block. Older result blocks (from earlier in the turn or earlier in the conversation) are stripped of any notification prefix they carried — only the most recent result holds the current snapshot.

This happens at **request-send time**, not per-result-block construction. The kernel intercepts the wire as it's being serialized, finds the most recent `ToolResultBlock`, and injects/strips meta there. This is content modification of an existing block, not a structural splice. Every adapter ships the result as-is; no `pre_request_hook` needed, no alternation invariant to defend, no canonical-vs-server-state regime split.

If the agent is ACTIVE but its most recent assistant message is text-only (no tool calls in this turn), there is no `ToolResultBlock` to attach to. The notification waits for the next tool result the agent produces. For a normally-active agent this is within seconds; for an agent composing a long text reply, the notification surfaces when the next tool fires. This "wait for the next valid attachment" is correct — it's exactly the mid-turn coherence guarantee we wanted.

**ASLEEP → wake then sync as IDLE.** During ASLEEP, fingerprint polling continues. If the fingerprint changes, the kernel transitions the state to IDLE (the canonical wake transition) and then runs sync as IDLE — splicing the pair, posting `MSG_TC_WAKE`. This is the load-bearing wake path for external producers (mail arrival from `mcp.imap`, telegram messages, etc.). The kernel does *not* require an external `.sleep`-clearing signal for notification-driven wake; the notification arrival itself is the signal.

If the fingerprint hasn't changed during ASLEEP (no producer wrote anything), the agent stays asleep — no spurious wakes from heartbeat ticks alone.

#### 2.2.5 The single-slot invariant

This is the design's load-bearing guarantee, worth naming explicitly:

> **At any moment, the agent's wire context contains exactly zero or one notification block. The block's content, when present, always reflects the current state of `.notification/`.**

Consequences:

- **No accumulation.** Multiple producers updating their files in quick succession produce one resync, not multiple stacked blocks.
- **No staleness.** A block in the wire is, by construction, current as of the last sync — and sync runs every tick.
- **No dismiss.** The agent reading the block doesn't change anything; only producers mutating files changes anything. The kernel keeps the wire in sync; the agent observes whatever's in sync.
- **No coalesce flag, no replace_in_history flag.** Strip-and-reinject is universal; the file *is* the slot.
- **No notification-id lifecycle.** The kernel tracks one id (the wire's currently-injected block, if any) for stripping purposes. Producers don't see ids at all.

The agent's mental model: "there is one place in my context where notifications live; whenever it appears, what I see is current; when there's nothing to notify, it's absent." Coherent across IDLE pair-splices, ACTIVE result-meta, and ASLEEP wakes.

#### 2.2.6 The "no notifications" case

If `.notification/` is empty or absent, the fingerprint is `()`. If the prior fingerprint was also `()`, sync no-ops. If the prior fingerprint was non-empty (producer just cleared its file), sync strips the wire's block and injects nothing. Voluntary `system(action="notification")` calls return `{}` — useful for the agent to query "is there anything I might have missed?" even when there's nothing pending.

### 2.3 The agent's mental model

The tool description for `system` (specifically `action="notification"`) is the contract. Suggested wording (English; en/zh/wen all need updates):

> `system(action="notification")` returns the current state of all notification channels as a JSON object keyed by source. Sources include `email`, `soul.flow`, scheduled wakes, and any other producer that has published to the notification surface. Each source's value is structured data the producer wrote.
>
> The kernel may synthesize this call on your behalf in two situations: (1) when you are idle and notifications arrive, the kernel wakes you with this call already made, so your next thought has the notifications in context; (2) when you are mid-tool-chain, the next tool result you receive may carry a `notifications:` JSON block prepended to its content — this is the same data, surfaced alongside the result you were already going to read.
>
> The data is replace-only: each source has one current state, not a history of events. If a producer has nothing to notify, its source is absent from the result. There is no dismiss action — you read what's currently published, and the producer updates the published state when its situation changes (e.g. mail intrinsic re-renders the unread digest when you read mail).

The agent reads this once at boot (system prompt assembly). Every appearance of `system(action="notification")` in the wire — voluntary, synthesized as pair, prepended as meta — is legible because the tool description explained all three modes.

### 2.4 What the agent never has to do

Several things the prior design required the agent to handle, this design eliminates:

- **No dismiss.** The agent never says "I've handled this notification." Producers update their files when state changes; the agent's act of reading is not a state change.
- **No "is this synthesized" distinction.** Tool description tells the agent the kernel can synthesize this call; the agent treats voluntary and kernel-synthesized identically.
- **No notif_id tracking.** Each `<source>.json` is the source of truth; there's no per-event handle.
- **No mid-turn coherence reconciliation.** Notifications arrive as either (a) a voluntary tool call the agent made, (b) a result-prepend on a tool the agent called, or (c) a wake event when idle. All three are coherent grain points.

## 3. Frontend integration

The notification surface is now a **public protocol**, not an internal kernel detail. Frontends are first-class consumers.

### 3.1 The surface is the directory

The TUI and portal each independently:
1. Watch `<workdir>/.notification/` for changes (filesystem watcher, not kernel API).
2. On change, read the directory using the same logic as `collect_notifications`.
3. Render per-source for their own UI surface.

Neither frontend needs a kernel API. Neither blocks on the other. New frontends (web extension, mobile app, MCP server consuming notifications from another agent) all participate the same way.

### 3.2 Source-specific renderers

Each frontend implements per-source renderers for sources it cares about most, with a generic fallback for the rest:

**TUI (Go + Bubble Tea):**
- Status bar: glanceable indicators per source — `📧 3 unread · 🌊 soul · 💬 telegram(2)`.
- `/notifications` slash command: panel showing full content per source.
- Source-specific renderers for `email` (sender list with snippets), `soul` (italicized voice text), `system` (multiplexed event types with relative time), `mcp.telegram` (chat preview). Other sources fall back to a generic `header` line + pretty-printed JSON.

**Portal (Go + embedded web frontend):**
- Notification icon in top bar with non-empty indicator.
- Drawer with cards, one per source.
- Per-source card layouts for `email` (sender avatars, click-through to thread), `soul` (quote-styled rendering), `mcp.telegram`/`mcp.feishu`/`mcp.wechat` (chat-style cards), etc. Generic fallback: collapsible JSON viewer.

### 3.3 Why this works

The frontends and the agent each get the projection that's right for them, sourced from the same JSON. The producer doesn't need to know any frontend exists. The kernel doesn't need to know any frontend exists. The frontends evolve independently — TUI ships a basic render today, portal ships a richer one next week, agent always reads the structured payload directly without going through a render layer.

This is the correct division of concerns: **producers are publishers, the kernel is a transparent transport, consumers are renderers.**

## 4. What gets removed

This design eliminates a substantial amount of kernel machinery:

### 4.1 Code removed

- **`tc_inbox.py`** — the queue, `InvoluntaryToolCall`, `DrainResult`, `coalesce` flag, `replace_in_history` flag, `remove_by_notif_id`. ~160 lines.
- **`pre_request_hook` from `ChatSession`** (`llm/base.py`) and from all four adapters (anthropic, openai, gemini, deepseek). ~6 callsites × ~3 lines each = ~20 lines.
- **`_install_drain_hook` and `_drain_tc_inbox_for_hook`** from `BaseAgent`. ~50 lines plus their docstrings.
- **`_drain_tc_inbox`** as a queue-draining method (kept as a name for the polling delivery, or renamed).
- **`_appendix_ids_by_source`** dict on `BaseAgent` — was tracking call_ids for `replace_in_history`. Gone.
- **The dismiss intrinsic** (`intrinsics/system/notification.py:_dismiss`) and `interface.remove_pair_by_notif_id`. Producers update their own files; the agent never dismisses.
- **`_pending_mail_notifications`** tracking on the mail intrinsic. Mail re-renders `.notification/email.json` from current inbox state; no per-message tracking needed.

### 4.2 Concepts removed

- **Coalesce semantics.** Replace-only is universal; the file *is* the slot, and the wire's single-slot invariant mirrors it.
- **`replace_in_history` semantics.** Strip-and-reinject is the only update path. The kernel tracks one entry id (current wire block); strip-by-id is unambiguous.
- **Per-source notification ids.** The kernel tracks one id total — the currently-injected wire block. Producers don't see ids at all; they own files.
- **Tombstones.** No per-source pairs to supersede. Citations to old snapshots are stable in chat history (the snapshot in the wire stays as it was when delivered); the *next* sync produces a new block, not a replacement of the prior wire entry's content.
- **Mid-turn drain hook regime distinction.** No mid-turn structural splice → no canonical-vs-server-state difference.
- **Notification dismiss path.** Replaced by producer self-management — producer overwrites or deletes its file when state changes; sync mechanism updates the wire automatically.
- **Token-cost inflation worry.** Because only the *most recent* `ToolResultBlock` carries the meta (and older ones are stripped), the wire never accumulates duplicate notification copies across turns. Per-turn token cost is bounded by one notification snapshot.

### 4.3 Net code change

Estimated: **−250 to −350 lines of kernel code, +80 to +120 lines of new code.** Net negative. This is a real architectural simplification, not a sideways move.

## 5. The three drain points → one sync mechanism

Today's three drain points (per `lingtai_kernel/ANATOMY.md` §Drain points) collapse into one sync mechanism with three placement variants:

| Today | After |
|---|---|
| `_handle_request` entry — drains `tc_inbox` at turn start | Sync fires on heartbeat fingerprint change. At request-send time, the kernel checks fingerprint vs wire block and updates if needed. Voluntary `system(action="notification")` calls work as a normal tool dispatch. |
| `pre_request_hook` mid-turn drain | **Removed.** ACTIVE-state sync at request-send time attaches the current notification snapshot as meta on the most recent `ToolResultBlock`, stripping it from older results. No structural splice; no `pre_request_hook` needed. |
| `_handle_tc_wake` for idle agents | IDLE-state sync splices a `(call, result)` pair; `_handle_tc_wake` becomes a thin wrapper. ASLEEP fingerprint change wakes the agent (state→IDLE) before the same pair-splice runs. |

The mechanism is one function: compute fingerprint, compare to last-seen, on change call `collect_notifications`, strip prior wire block (if any), inject new block at the placement appropriate for current state. The placement variants are decisions the sync makes; the mechanism doesn't fork.

For an agent composing a text-only reply with no `ToolResultBlock` in flight, ACTIVE-state sync has nothing to attach to — the notification waits until the next tool result the agent produces. On a normally-active agent this is within seconds. The wait is correct: synthesizing a fake result mid-text would re-introduce the structural splice problem we just escaped.

## 6. Producer migration

This section is the design's leverage point: the `.notification/` filesystem becomes the **universal async-info-injection surface** for all tool→agent flow. Every producer in the kernel that currently routes async info through `tc_inbox`, `_enqueue_*` helpers, or ad-hoc `[system]` injection, migrates to "write a JSON file." There is no per-producer machinery anymore — there is one mechanism, and producers are file writers.

The naming is flat: `<intrinsic_name>.json` for kernel intrinsics, `mcp.<server_name>.json` for MCP-loaded servers. Within each file, the producer multiplexes its own events however it wants — no kernel-imposed sub-schema.

### 6.1 Mail (`email`)

**Today:** `_enqueue_email_unread_digest` (`base_agent/messaging.py:51`) calls `_render_unread_digest`, builds a `ToolCallBlock`/`ToolResultBlock` pair, enqueues on `tc_inbox` with `coalesce=True, replace_in_history=True`.

**After:** the email intrinsic writes `.notification/email.json` whenever its render of inbox state changes:

```python
notif_path = workdir / ".notification" / "email.json"
notif_path.parent.mkdir(exist_ok=True)
data = {
    "count": count,
    "previews": [...],
    "newest_received_at": newest_ts,
}
tmp = notif_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, ensure_ascii=False))
tmp.rename(notif_path)
```

When count drops to 0 (agent read all unread), the intrinsic deletes `.notification/email.json`. The kernel sync notices the disappearance, strips the wire block.

### 6.2 Soul (`soul`)

**Today:** `intrinsics/soul/flow.py:228` enqueues `InvoluntaryToolCall` with `coalesce=True, replace_in_history=True`.

**After:** writes `.notification/soul.json` with the voice content. Consultation fires overwrite the file (filesystem replace = wire replace, by the sync mechanism). When soul flow decides "nothing to say" (fire produces no voices, or TTL expires), the producer deletes the file.

If soul ever has multiple notification kinds (flow + pending-inquiry, say), they multiplex inside the JSON payload — `{"flow": {...}, "inquiry_pending": {...}}` — not across multiple files. The file basename is the *tool*; the contents are the tool's choice.

### 6.3 System (`system`)

**Today:** `_enqueue_system_notification` builds a per-event pair with `notif_id`, enqueues without coalesce. Multiple call sites: mail bounce, scheduled wake, future kernel events.

**After:** the system intrinsic owns `.notification/system.json` and multiplexes its event types inside the file. If multiple bounces accumulate alongside a scheduled wake reminder:

```json
{
  "bounces": {"count": 2, "recent": [...]},
  "scheduled_wake": {"fires_at": "...", "message": "..."}
}
```

The `notif_id` concept goes away — the file *is* the notification, and the system intrinsic decides when individual events have been dispatched/expired. Existing call sites that today call `_enqueue_system_notification(...)` migrate to a thin helper `system_publish(event_type, payload)` that updates the JSON in place.

### 6.4 MCP server channels (`mcp.<name>`)

Each MCP server (lingtai-imap, lingtai-telegram, lingtai-feishu, lingtai-wechat) writes its own `.notification/mcp.<server_name>.json`:

- `mcp.imap.json` — IMAP/Gmail mail events (raw arrivals)
- `mcp.telegram.json` — Telegram messages
- `mcp.feishu.json`, `mcp.wechat.json` — same pattern

This decouples MCP servers from the kernel's Python API entirely. They have filesystem write access to the agent's working directory already; they just write JSON. Status-shaped channels (chat backlog) render digests; event-shaped channels accumulate inside the file (e.g. a list of unread messages with caps).

**Note on imap vs email split.** The `mcp.imap` server feeds raw mail events; the `email` intrinsic aggregates across mail sources (local mailbox + imap + future). Both files live independently in `.notification/`. The agent sees both keys (`email`, `mcp.imap`). If users want unified mail status, the email intrinsic reads `mcp.imap.json` plus its own state and writes an aggregated `email.json`. Two distinct producers, two distinct files, no kernel arbitration.

### 6.5 Inquiry / `.prompt` signal injection — out of scope (for now)

Today, `.prompt` and `.inquiry` reply paths inject `[system]` messages directly into the wire (not through `tc_inbox`). These are **human→agent signals**, not producer state — the human types `.prompt`, the kernel pushes it once, and there's no "current state" to reflect. Their semantics differ from notifications.

Open question: should they migrate too? Pros: one mechanism for everything. Cons: `[system]` messages are one-shot deliveries (the human's message exists in the wire forever as a chat record), while notifications are state mirrors (the wire's notification block is replaced when state changes). Forcing `.prompt` into the notification mold would either give it staleness ("your last prompt was…" lingering forever) or require an artificial dismiss — neither matches the human-signal pattern.

**Recommendation:** keep `.prompt` and `.inquiry` separate, document the distinction. Notifications are for tools publishing async state; signal-file injections are for one-shot human→agent messages.

### 6.6 Molt clearing

`intrinsics/psyche/_molt.py:193, 369` currently calls `_tc_inbox.drain()` and clears `_appendix_ids_by_source`. After: clears the `.notification/` directory wholesale at molt time, or selectively (producer-driven) if specific notifications should survive a molt. The reset fingerprint forces the next sync to inject whatever's still on disk into the fresh wire.

## 7. Migration plan

Reordered so the risky/destructive step (adapter cleanup) lands last, after the new mechanism is proven by living producers. During the migration window, `tc_inbox` keeps working as a degenerate empty queue — old code paths exist but no producer enqueues, so they're harmless.

### 7.1 Phase 1: kernel sync mechanism

The new mechanism lives alongside the old. Nothing migrates yet; producers still use `tc_inbox`. This phase is purely additive.

1. Create `<workdir>/.notification/` directory lazily on first publish (no need to bootstrap empty).
2. Add `lingtai_kernel/notifications.py` with:
   - `notification_fingerprint(workdir) -> tuple` — the (name, mtime_ns, size) tuple from §2.2.1.
   - `collect_notifications(workdir) -> dict` — read all `.json` files, return dict keyed by basename (§2.2.2).
   - `publish(workdir, tool_name, payload)` — tmp+rename helper for in-process producers.
   - `clear(workdir, tool_name)` — delete a producer's file.
3. Add the sync logic to `BaseAgent`:
   - Track `_notification_fp` (last-seen fingerprint) and `_notification_block_id` (entry id of the wire's currently-injected block, or `None`).
   - On heartbeat tick: compute current fingerprint; if differs, run sync.
   - Sync logic: strip prior block (by id), inject new block per current state (IDLE pair / ACTIVE meta / ASLEEP wake-then-IDLE-pair).
4. Add the `system(action="notification")` intrinsic action handler that returns `collect_notifications(workdir)`.
5. Add request-send-time meta attachment for ACTIVE state: when serializing the wire for an outgoing API call, find the most recent `ToolResultBlock` and prepend/strip `notifications:\n<json>\n\n` per current `.notification/` state.
6. Update the `system` tool description with the contract from §2.3 (en/zh/wen).
7. Tests: filesystem→wire sync covered for all three placement variants, ASLEEP-wake path, fingerprint-unchanged no-op.

After Phase 1 lands, the new mechanism is live and `.notification/` files would surface in the agent's wire — but no producer is writing them yet, so the directory stays empty. Existing `tc_inbox` continues to work unchanged.

### 7.2 Phase 2: producer migration

Migrate producers one at a time. Each migration is a small self-contained patch — no kernel changes needed since the surface already exists.

8. Migrate `_enqueue_email_unread_digest` (`base_agent/messaging.py`) to write `.notification/email.json`; delete on count=0.
9. Migrate `intrinsics/soul/flow.py:228` to write `.notification/soul.json`; delete when fire produces no voices.
10. Migrate `_enqueue_system_notification` to update `.notification/system.json` (multiplexed); preserve the helper signature so call sites don't change.
11. Migrate `intrinsics/psyche/_molt.py` molt-time clearing to clear `.notification/` directory.
12. Sibling MCP repos (lingtai-imap, lingtai-telegram, lingtai-feishu, lingtai-wechat): update their event handlers to write `.notification/mcp.<server>.json`. These ship as separate repo releases on their own cadence.
13. Delete `_pending_mail_notifications` tracking from the mail intrinsic — superseded by filesystem state.

After Phase 2, all producers route through `.notification/`. `tc_inbox` is never enqueued; `_handle_tc_wake` and `pre_request_hook` are still wired but receive nothing.

### 7.3 Phase 3: adapter + base_agent cleanup (deferred)

This phase is intentionally separated from the rest — it touches every adapter and is the only irreversible step. Schedule for a release after Phase 2 has soaked.

14. Remove `pre_request_hook` from all four adapters (anthropic, openai, gemini, plus deepseek if present).
15. Remove `pre_request_hook` from `ChatSession` base in `llm/base.py`.
16. Remove `_install_drain_hook` and `_drain_tc_inbox_for_hook` from `BaseAgent`.
17. Remove `_appendix_ids_by_source` from `BaseAgent`.
18. Remove the `_tc_inbox` attribute from `BaseAgent`.
19. Delete `lingtai_kernel/tc_inbox.py`.
20. Delete `intrinsics/system/notification.py:_dismiss` and `interface.remove_pair_by_notif_id`.
21. Remove `replace_in_history` and `coalesce` plumbing from `tc_inbox` and any remaining callers.

### 7.4 Phase 4: docs + frontends

22. Update `lingtai_kernel/ANATOMY.md` "Involuntary tool-call pairs" section: describe the new `.notification/` protocol. Move historical context to a "History" subsection.
23. Update `base_agent/ANATOMY.md` and `intrinsics/system/ANATOMY.md` for the dismiss-path removal and the new `system(action="notification")` action.
24. Add new ANATOMY entry for `lingtai_kernel/notifications.py`.
25. **Frontends:** TUI implements `.notification/` watcher + status bar + `/notifications` panel. Portal implements `.notification/` watcher + drawer with cards. Both ship independently — they can read `.notification/` files even before the kernel produces them, or after, whichever lands first.

### 7.5 Release

Phase 1 + Phase 2 ship together as the headline release. Phase 3 (adapter cleanup) ships as a separate point release after soak. Phase 4 docs/frontends ship on their own cadence (no kernel coupling).

Release notes for the headline release call out:
- The `.notification/` filesystem protocol as the new producer contract.
- The dismiss-path removal (agent-visible: `system(action="dismiss")` no longer exists; producers manage their own state).
- `tc_inbox` retained internally, scheduled for removal in a follow-up release.

## 8. Open questions

Things that genuinely don't have obvious answers and deserve decision before implementation:

### 8.1 Suggested envelope shape — enforce or document?

§2.1.3 sketches `{header, icon, priority, published_at, data}`. The kernel doesn't enforce this — producers can write any JSON. But frontends rely on it for compact rendering.

**Option A:** Document as convention; producers adopt voluntarily; frontends fall back gracefully when fields are absent.
**Option B:** Schema-validate at producer-write time (the kernel's `publish` helper validates before writing).

**Recommendation:** A. Conventions enforced by tooling (the `publish` helper writes the right shape; raw filesystem writes don't validate) hit the right balance — easy to do correctly, hard to do badly accidentally, but no enforcement that would block external producers from writing whatever JSON they want.

### 8.2 Polling cadence

Heartbeat (~1s) is the canonical mechanism. Is sub-second delivery ever needed?

**Recommendation:** No. The use cases (mail arrival, soul flow, scheduled wakes) all tolerate 1s latency. The fast-path `_notification_changed()` helper exists for in-process producers but is optional. External producers (MCP servers writing files) get heartbeat-cadence delivery, which is fine.

### 8.3 Where does the directory live?

Within `<workdir>/.notification/` is the obvious answer (parallels `<workdir>/.lingtai/`, `.agent.json`, signal files at workdir root). But signal files are at *workdir root* (`.prompt`, `.inquiry`), not in a subdirectory.

**Option A:** `<workdir>/.notification/<source>.json` — subdirectory.
**Option B:** `<workdir>/.notification.<source>.json` — flat at workdir root.

**Recommendation:** A. The set is unbounded (one file per source key, source keys can include `:` for sub-namespacing), and a subdirectory keeps the workdir root readable. Signal files are a finite well-known set that benefits from being top-level; notifications aren't.

### 8.4 What happens during agent restart?

`.notification/` files persist across restart by virtue of being filesystem state. On boot, the agent immediately sees whatever is published. This is correct: producers that persist their own state independently (mail's inbox, soul's records) will re-publish on their own cadence; transient producers (in-memory schedulers) need to clean up on shutdown if they don't want stale notifications hanging around.

**Recommendation:** Document this. No special kernel logic on boot — just trust the filesystem state.

### 8.5 ASLEEP — does the sync run, and does it wake the agent?

ASLEEP is the canonical "agent paused, only signal files wake it" state. Does notification arrival count as a wake signal?

**Decision:** Yes. The fingerprint poll runs during ASLEEP just like every other state. If the fingerprint changes, the kernel transitions ASLEEP → IDLE (the canonical wake path) and runs sync as IDLE — splicing the pair, posting `MSG_TC_WAKE`. This is the load-bearing wake mechanism for external producers (e.g. `mcp.imap` mail arrival pulls a sleeping agent back into action).

If the fingerprint doesn't change during ASLEEP (no producer wrote anything), the agent stays asleep — no spurious wakes from heartbeat ticks alone.

Note that producers that *should not* wake an asleep agent are responsible for not writing during ASLEEP. Soul flow already meets this — it doesn't fire during ASLEEP. External producers (mail, telegram) by design *should* wake the agent on arrival; that's the entire point.

This subsumes today's `MSG_TC_WAKE` mechanism — `_handle_tc_wake` becomes a thin wrapper that triggers the same sync logic with the wake transition pre-applied.

### 8.6 What about `system(action="notification")` calls when nothing is published?

Voluntary call returns `{}`. Should the agent see `{}` or get a friendlier "nothing pending" message?

**Recommendation:** `{}`. The agent reads JSON; an empty dict is an unambiguous "nothing here." Wrapping in prose ("you have no notifications") is the kind of presentation concern this design moved out of the kernel.

### 8.7 Pretty-printing JSON in tool result content

The ACTIVE-state meta block prepended to a `ToolResultBlock` carries pretty-printed JSON. How indented?

**Recommendation:** `json.dumps(data, indent=2, ensure_ascii=False)`. Two-space indent is readable without being wasteful; `ensure_ascii=False` preserves UTF-8 (Chinese, emoji, etc.) without escape sequences cluttering the output. Same formatting for the IDLE pair-splice synthetic result.

### 8.8 Backwards compatibility window

This is a kernel internal change — no public API surface affected from the agent's perspective except (a) the `system(action="dismiss")` action goes away, and (b) `system(action="notification")` returns a different shape (was per-event pairs, now a current-state dict).

The agent's tool description changes; old chat histories from before the migration still work (they reference old tool calls that don't repeat). New tool calls use the new shape.

**Recommendation:** ship in one release, no compat shim for `dismiss`. Update i18n in the same commit. Old chat histories are read-only by definition and don't break.

### 8.9 Should producers also be able to write `.notification/<source>.md`?

Frontends might want pre-rendered markdown for some sources. The kernel passes JSON to the agent; the frontend could read either JSON or markdown depending on what the producer published.

**Recommendation:** No, not now. Producers publish structured data; consumers render. If a producer wants to publish markdown, it goes in the JSON as a `body_markdown` field. Filesystem-level format diversity is complexity we don't need yet.

## 9. Risks and rollback

### 9.1 Risk: producers writing malformed JSON

Mitigation: the kernel's `collect_notifications` wraps `json.loads` in a try/except and skips files that fail to parse, logging a warning. Producer bug doesn't break the agent; producer authors see warnings in `events.jsonl` and fix.

Oversized payloads are a producer-side concern (§2.1.4). The kernel doesn't impose a cap; if a producer ships a multi-megabyte payload, the agent's context inflates and the producer's author sees it directly. Add a kernel cap only if runaway producers become a real problem.

### 9.2 Risk: stale `.notification/` files from crashed producers

If a producer writes a file and then crashes without clearing, the file persists indefinitely. The agent keeps seeing the stale notification.

Mitigation: producers should include `published_at` and clean up at sensible TTLs themselves. The kernel does *not* impose staleness logic — that's a producer concern. Frontends can warn on age (e.g. red color for >1h old), but the kernel stays silent.

### 9.3 Risk: filesystem watcher latency on slow disks (NFS, network mounts)

Frontends watching the directory get latency that depends on the filesystem layer. For NFS-mounted workdirs, watches can be unreliable.

Mitigation: frontends fall back to polling if watch is unavailable. Kernel polling at heartbeat cadence is unaffected (it's an explicit `listdir`+`stat`, not a watch).

### 9.4 Risk: meta-delivery hides notifications from agents that don't read the convention

If the identity prompt or tool description doesn't teach the agent what `notifications:\n{...}` is, the agent might read it as part of the bash output and respond as if bash said it.

Mitigation: tool description for `system(action="notification")` is updated in the same commit as site C ships. Add a unit test that verifies the system prompt assembly always includes the notification-convention text when `system` intrinsic is enabled.

### 9.5 Rollback

Phase 2 (adapter cleanup) is the only irreversible step within a release. Phases 1, 3, 4, 5 are revertable.

If a critical post-release issue surfaces, rollback is one revert. Wire format on disk (`chat_history.jsonl`) is unchanged — both designs produce `(call, result)` shapes — so rolling back doesn't require migrating chat histories. `.notification/` files become inert (nothing reads them); next kernel-up they resume working.

## 10. Why this is the right design

The design conversation arc that led here was a sequence of removals, not additions. Each removal made the design *more* capable, not less, because the removed thing was load-bearing complexity that had to be coordinated:

- Started with `tc_inbox` queue + two flags (`coalesce`, `replace_in_history`) + three drain points + `pre_request_hook` + canonical-vs-server-state regime distinction + `_install_drain_hook` lifecycle dance + `notif_id` lifecycle + dismiss intrinsic + `_appendix_ids_by_source` tracking.
- Through several intermediate designs (envelope registry, header-only tombstones, single-slot replace).
- To: producers write JSON files, kernel reads directory, agent gets a dict.

The endpoint is the kind of design that doesn't decay because there's no internal complexity to drift. The kernel does one thing — pass JSON from filesystem to agent — and the producer contract is the simplest possible (write a file).

The filesystem-as-protocol pattern is doing real work here. It's the same pattern the kernel already uses for the mailbox, signal files, and the agent manifest. Notifications slot into that pattern identically. External producers (MCP servers, shell hooks, future tooling) participate on equal footing with built-in intrinsics. Frontends consume the same surface without going through any kernel API.

The agent's mental model is the simplest it has been: there's a tool called `system(action="notification")`. Sometimes you call it; sometimes the kernel calls it on your behalf when you're idle, or surfaces its content alongside another tool result when you're active. Either way, you get a JSON object describing what's currently published. There is no event log, no dismiss, no per-event lifecycle. There is just the current state of the channels, which you read.

That's not just clean engineering; it's a coherent self-model the agent can actually inhabit without contradiction.
