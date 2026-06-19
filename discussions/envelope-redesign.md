# Envelope Redesign — unifying tc_inbox into status-channel envelopes

> **Status:** Design doc, pre-patch. Discussion 2026-05-05 between Zesen and Claude (claude-opus-4-7[1m]).
> **Predecessors:** `system-injection-audit.md` (the seven injection paths catalogue), `tc-injection-service-implementation-proposal.md` (intermediate refactor toward `TCInbox.drain_into`), `email-unread-digest-notification-patch.md` (the prototype this design generalizes), `tc-inbox-mid-turn-drain-patch.md` (the mid-turn pre_request_hook this design reverts).
> **Goal:** Replace the `tc_inbox` queue + `pre_request_hook` mid-turn splice with a per-source **envelope registry** that delivers as a tool-pair when the agent is idle and as result-meta when active, with single-slot replace-on-supersede and tombstoned predecessors.

## 1. Why

The current tc_inbox machinery solves real problems but at the wrong abstraction level. Five concrete failure modes, all rooted in treating world events as a queue of discrete pairs to splice into the wire:

1. **Mid-turn splice lands inside coherent reasoning.** The `pre_request_hook` (`f46b346`) earns a *wire-legal* splice point — `has_pending_tool_calls()` is False after a tool-result lands, so an `(assistant[tool_call], user[tool_result])` pair can be appended legally. But "wire-legal" and "agent-coherent" are different. The model is mid-thought; splicing a soul-flow voice between bash result N and bash call N+1 is a non-sequitur the agent has to reconcile mid-task. The kernel sees a safe boundary; the agent experiences an interruption.

2. **Token cost inflates within a turn.** Anything spliced via `pre_request_hook` enters `interface.entries` and re-serializes on every subsequent API call in the turn. A single soul-flow voice queued at tool-call 2 of a 12-call chain ships in 11 of those API calls. `coalesce=True` dedupes across fires; it does nothing about per-turn re-serialization.

3. **`replace_in_history=True` mid-turn corrupts citations.** When a soul-flow voice is replaced mid-turn, `interface.remove_pair_by_call_id(prior_id)` deletes the prior pair wholesale. If the model was composing a sentence that quoted the old voice, the citation now references nothing in the current wire. Today this is documented as a "consideration, harmless because soul flow doesn't drive tool calls" (`base_agent/__init__.py:669-684`) — a brittle invariant that depends on every future producer staying disciplined.

4. **Two regimes diverge.** Canonical-interface adapters (Anthropic, OpenAI Chat Completions, Codex Responses, DeepSeek) deliver the spliced pair in the *current* request. Server-state adapters (`OpenAIResponsesSession`, `GeminiChatSession`, `InteractionsChatSession`) only deliver on the *next* turn after re-sync. Same producer code, different latency, depending on plumbing the producer shouldn't have to know about.

5. **Session lifecycle fragility.** `_install_drain_hook` has to be re-called on every drain so AED-rebuilt sessions pick up the hook; idempotent, but every session-replacing path (rebuild, recovery, molt) needs to either route through `_drain_tc_inbox` or remember to re-install. Persistent maintenance tax.

Beyond these mechanical issues, the deeper smell: **`tc_inbox` is shaped like an event queue but most of its callers want status semantics.** The `email.unread` producer (`base_agent/messaging.py:51`) already discovered this — it uses `coalesce=True, replace_in_history=True` with `_render_unread_digest()` to render the *current state of the inbox* as the result body, not per-message events. Soul flow does the same. System notifications are the only producer left using accumulating per-event delivery, and their accumulation is what makes a busy stretch of mail noisy in chat history.

The pattern that already works for `email.unread` is the design we want for *every* injection. This doc generalizes it.

## 2. Design — envelopes

### 2.1 Core abstraction

An **envelope** is a single-slot status surface keyed by `source`. Producers publish current state; the kernel decides delivery shape based on agent state at delivery time.

```
envelope.publish(
    source: str,                # e.g. "email.unread", "soul.flow", "system.notification"
    content: str,               # rendered current state — what the agent reads
    header: str = None,         # one-line summary for tombstone on supersede
    tombstone_on_supersede: bool = True,
)
```

Replace-only by construction: `publish` to an existing source overwrites whatever envelope currently exists at that source — wherever it lives (queued, spliced as pair, appended as meta).

### 2.2 Two delivery modes

The kernel chooses delivery shape from agent state at delivery time. The envelope content is the same in both cases; only the shape differs.

**Mode A — pair delivery (agent idle):** Synthesize a tool-call pair `(call=envelope_call(source=...), result=content)`, splice into wire, post `MSG_TC_WAKE` to drive the agent through it. This is the existing `_handle_tc_wake` path with the envelope content as the result body. The agent wakes and reads the envelope as if it had just queried this channel.

**Mode B — meta delivery (agent active):** Append the envelope content as a structured preamble onto the **closest preceding tool result** in the wire. No new pair. No structural splice. The agent reads it on its next round-trip as part of an existing result block:

```
[envelope · soul.flow · 23s ago]
voice from past-self: "reconsidering — approach B is safer because..."

<original tool result content for whatever the agent was actually doing>
```

The agent's perception of "I asked for X and got back X plus an envelope notice attached" is structurally identical to how human inboxes work — the next thing you check carries any pending status with it.

### 2.3 Single-slot per source, with tombstones on supersede

When a `publish` arrives for a source that already has a live envelope:

1. **If the prior envelope is still queued** (idle path, not yet spliced): overwrite in the registry. No wire effect.
2. **If the prior envelope is spliced as a pair**: replace the *content* of the prior result with a tombstone, then deliver the new envelope (in whichever mode is current).
3. **If the prior envelope is appended as meta on a result**: replace the meta block with a tombstone marker, then deliver the new envelope.

A tombstone is a one-line marker with the producer's `header`:

```
[envelope superseded · 23s ago] prior soul.flow voice favored approach A
```

This is the **header-only tombstone** design — neither full-body retention (would defeat the single-slot invariant) nor empty marker (would dangle citations). Producers supply `header` as the semantic summary of what was being said; if not supplied, fall back to an empty tombstone (`[envelope superseded]`).

`tombstone_on_supersede=False` skips the tombstone entirely — used by status sources where every update is continuous (e.g. mail unread count changing from 3 to 5 shouldn't tombstone the prior envelope; just replace it).

### 2.4 Why tombstones, not deletion

Today's `replace_in_history=True` does wholesale deletion. The argument that "soul flow content isn't cited so it's safe" is brittle — any producer that wants replace semantics on cite-able content trips an invariant break. Tombstoning splits the difference correctly: the *content* is ephemeral (gets replaced), the *fact that the agent perceived something* is durable (the tombstone persists).

Concretely:
- **Citation safety**: a sentence the model is composing that references the old envelope's content lands on a tombstone that says "this was here and was replaced," not on nothing. Reasoning recovers naturally with "as the prior voice (now superseded) suggested..."
- **Self-model coherence**: the agent's history reads as a narrative of *changes of mind* rather than a series of mysteriously-mutated states.
- **Debug affordance**: replaying the wire shows when transitions happened, not just the latest state. Pairs cleanly with `events.jsonl` for post-hoc analysis.

### 2.5 What gets removed

This design eliminates several concepts:

- **`coalesce` flag**: replaced by registry-level slot uniqueness. There is no "queue with optional dedup" — the registry is one-per-source.
- **`replace_in_history` flag**: replaced by the universal supersede-with-tombstone behavior.
- **`pre_request_hook` mid-turn drain**: gone entirely. Meta-on-tool-result is a content edit, not a structural splice; no alternation invariant to defend.
- **`_install_drain_hook` and `_drain_tc_inbox_for_hook`**: gone (the hook is gone).
- **`remove_by_notif_id` queue path**: notifications are envelopes; "dismiss" means re-publish with empty content (which becomes a tombstone) or `envelope.clear(source)`.
- **The two-regime distinction** (canonical vs server-state adapters): meta-on-result is text inside an existing block, identical in both regimes.

Net code change: probably negative. The registry + the two delivery modes + supersede-with-tombstone is less code than the queue + two flags + three drain points + the install dance.

## 3. Concrete shape in the kernel

### 3.1 New module: `envelope.py`

Replaces `tc_inbox.py`. Roughly:

```python
@dataclass
class Envelope:
    source: str
    content: str
    header: str | None
    published_at: float
    tombstone_on_supersede: bool = True
    # Tracking — populated by delivery
    delivered_call_id: str | None = None  # set when delivered as pair
    delivered_meta_at_call_id: str | None = None  # set when delivered as meta

class EnvelopeRegistry:
    """Single-slot-per-source envelope registry, thread-safe."""
    def __init__(self):
        self._envelopes: dict[str, Envelope] = {}
        self._lock = threading.Lock()

    def publish(self, source, content, header=None, tombstone_on_supersede=True):
        """Replace any existing envelope with the same source. Returns the
        prior envelope (if any) so the caller / drainer can tombstone it."""

    def clear(self, source) -> Envelope | None:
        """Remove the envelope for a source. Returns prior for tombstoning."""

    def snapshot(self) -> list[Envelope]:
        """Snapshot the current registry state (for delivery rendering)."""

    def mark_delivered_as_pair(self, source, call_id) -> None: ...
    def mark_delivered_as_meta(self, source, call_id) -> None: ...
```

### 3.2 Delivery render — turn-start path

`_handle_request` (`base_agent/turn.py:_handle_request`) currently calls `agent._drain_tc_inbox()` first. New behavior:

```python
def _handle_request(agent, msg):
    # Render any pending envelopes into the wire before the LLM call
    _render_envelopes_for_turn(agent)
    # ... rest unchanged ...
```

`_render_envelopes_for_turn` walks the registry. For each envelope:

- If the wire tail is `user[tool_result]` (we're in the middle of a multi-turn task that just got a real tool result): **append meta** — modify the existing `ToolResultBlock` to prepend `[envelope · {source} · {age}]\n{content}\n\n` to its content. Mark delivered-as-meta with the call_id.
- Otherwise (turn-start with `user[text]`): **deliver as pair** — synthesize `(envelope_call, envelope_result)` and append. Mark delivered-as-pair with the call_id.

Either way, registry state is updated to track *where* the envelope currently lives in the wire.

### 3.3 Delivery render — idle wake path

`_handle_tc_wake` (`base_agent/turn.py:_handle_tc_wake`) currently drains the `tc_inbox` queue and drives each item through the LLM. New behavior: takes envelopes from the registry, delivers each as a pair (no meta path here — by definition the agent is idle, no preceding tool result to attach to), drives through the LLM. Identical to today's tc_wake structure, just sourced from the registry.

When idle wake delivers an envelope as a pair, mark it delivered-as-pair with the new call_id. The registry knows where every live envelope lives.

### 3.4 Supersede semantics

When `publish` finds an existing envelope:

1. Look up `prior.delivered_call_id` (pair) or `prior.delivered_meta_at_call_id` (meta) — what's currently in the wire.
2. If the prior is delivered as a pair: replace the result's content with the tombstone marker (keep the call_id, preserve wire structure). Then deliver the new envelope per §3.2/§3.3.
3. If the prior is delivered as meta on another tool's result: replace the prepended envelope block with the tombstone marker. Then deliver the new envelope.
4. If the prior is undelivered (still in the registry but never spliced): just overwrite, no wire effect.

If `tombstone_on_supersede=False`: same as above but the prior wire content is *removed* rather than replaced with a tombstone. This is the right semantics for sources where every update is continuous status (mail unread count changing).

### 3.5 Producer migration

All current tc_inbox producers become envelope publishers:

| Producer | Today | After |
|---|---|---|
| `email.unread` (mail arrival) | `coalesce=True, replace_in_history=True`, `source="email.unread"` | `envelope.publish(source="email.unread", content=digest, header=f"{count} unread", tombstone_on_supersede=False)` |
| `soul.flow` (consultation voice) | `coalesce=True, replace_in_history=True`, `source="soul.flow"` | `envelope.publish(source="soul.flow", content=voice_body, header=voice_summary, tombstone_on_supersede=True)` |
| `system.notification:<notif_id>` (mail bounce, etc.) | `coalesce=False, replace_in_history=False`, per-event slot | `envelope.publish(source=f"system.notification:{notif_id}", content=body, header=summary, tombstone_on_supersede=True)` — each notification is its own source, so single-slot is per-notification |

The system notification case is interesting: each notification has a unique `notif_id`, so `source=f"system.notification:{notif_id}"` makes each one its own slot. This preserves today's "every notification gets its own pair" semantics while still using the unified envelope mechanism. **Dismiss** becomes `envelope.clear(f"system.notification:{notif_id}")`. No more `remove_by_notif_id` parallel path.

### 3.6 Tool surface for the agent

The agent sees synthesized calls under a single tool name `envelope` (or keep `system`/`email`/`soul` per source for backward compatibility — see §6 on migration). Args carry `source` and producer-specific metadata (received_at, count, etc.). The agent's identity prompt teaches the convention:

> *Tool pairs whose `source` is in `{email.unread, soul.flow, system.notification:*, ...}` are kernel-injected envelopes — the world's status surfaces, not tools you called. Read the content; act on the content; do not narrate the perception as if you initiated it.*

### 3.7 Persistence

Envelope state is volatile by default — the registry is in-memory, rebuilt from the live producers on agent restart. The wire chat (where envelopes have been delivered) is durable via `chat_history.jsonl` as today. This is correct: envelopes are status snapshots, not persistent events, and the durable record is the rendered wire.

Producers that need persistence across restart (mail counts, soul flow records) already persist independently (`mailbox/`, `system/soul/`); they re-publish the current envelope on agent boot if relevant.

## 4. Walkthrough — the three failure modes resolved

### 4.1 Mid-turn coherence

**Before:** Soul-flow voice arrives during bash chain → `pre_request_hook` splices pair between tool-result N and tool-call N+1 → next API call serializes the spliced pair as part of the request → model sees a non-sequitur mid-thought.

**After:** Soul-flow voice arrives during bash chain → `envelope.publish(source="soul.flow", ...)` updates the registry → next time `_render_envelopes_for_turn` runs (start of next outer turn), it walks the registry. If the next turn opens with `user[text]` (a real human message arrived), envelopes deliver as pairs alongside the user's input. If the next turn is a continuation of the bash chain, the wire tail is `user[tool_result]` and envelopes append as meta on that result. In both cases, the agent reads the envelope at a coherent grain — the start of a perception cycle, not the middle of one.

### 4.2 Token cost

**Before:** Spliced pair re-serializes in every subsequent API call of the turn (N-1 redundant copies for an N-call chain).

**After:** Meta is appended once to a single tool result. That result re-serializes as part of normal wire history (the same as any tool result), but the envelope content is part of *one* result block, not duplicated. Once the next turn opens, the envelope's slot is occupied, so re-publishes overwrite in place. Worst case is one envelope per source per outer turn.

### 4.3 Citation corruption

**Before:** `replace_in_history=True` deletes prior pair wholesale → mid-composition citations dangle.

**After:** Supersede leaves a tombstone with the prior envelope's `header`. Citations land on `[envelope superseded · X ago] prior soul.flow voice favored approach A` — coherent text the model can reconcile naturally. The wire structure is preserved (the prior `call_id` still resolves), and the agent reads a narrative of "I had this perception, it was updated" instead of a silent rewrite.

## 5. What gets harder

Honest list of trade-offs:

1. **Renderer complexity goes up slightly.** `_render_envelopes_for_turn` has to walk the registry, decide pair-vs-meta per envelope, track delivery sites for future supersede. ~80-150 lines vs. today's `_drain_tc_inbox` (~30 lines).

2. **Meta delivery requires `interface` to support content edits.** Today `ChatInterface` has `add_assistant_message`, `add_tool_results`, `remove_pair_by_call_id` — appending text to an existing `ToolResultBlock`'s content needs a new method (`prepend_to_result_content(call_id, text)`). One method, ~10 lines.

3. **Producers lose explicit accumulation.** Today `system.notification` accumulates one pair per event; after migration, accumulation is expressed via per-notif_id source keys. Existing behavior is preserved but the semantic is now "many envelopes, each single-slot" rather than "one accumulating queue." Producers that genuinely want accumulation must mint distinct sources. This is good (forces explicit choice) but is a migration thinking task.

4. **MCP addons (lingtai-imap, -telegram, -feishu, -wechat) need to be told.** Each MCP server's LICC inbox listener publishes via `_enqueue_system_notification` today; that helper continues to work (it becomes a thin wrapper over `envelope.publish` with per-notif_id sourcing). Existing MCP servers don't need code changes. But future MCP servers should be encouraged to use envelope semantics directly when appropriate (e.g. a Telegram inbox status digest rather than per-message notifications).

5. **The "agent was asleep" temporal context disappears from the wire.** Today, an agent woken by tc_wake reads a synthesized pair with no marker that it was asleep. After this redesign, same situation. If we want to mark sleep→wake transitions, that's a separate, smaller change — `[envelope · ... · woke from ASLEEP after 47m]` could be added to the meta line on idle-wake delivery. Out of scope for this doc but worth flagging.

## 6. Migration plan

Rough sequence, intended for a single coordinated change rather than incremental:

### 6.1 Kernel core (Phase 1)

1. Add `envelope.py` with `Envelope` and `EnvelopeRegistry`.
2. Add `interface.prepend_to_result_content(call_id, text)` and `interface.replace_result_content(call_id, text)` (the latter for tombstones).
3. Add `_render_envelopes_for_turn(agent)` in `base_agent/turn.py` (or a new `base_agent/envelopes.py` submodule following the existing package pattern).
4. Replace `_drain_tc_inbox()` body with envelope rendering. Keep the method name as a transitional shim or rename.
5. Replace `_handle_tc_wake` body to source from registry instead of queue.
6. Update `BaseAgent.__init__` to instantiate `EnvelopeRegistry` instead of `TCInbox`.

### 6.2 Adapter cleanup (Phase 2)

7. Remove `pre_request_hook` from all four adapters (anthropic, openai, gemini, plus deepseek if present). Pure deletions — the hook callsites become unconditional.
8. Remove `_install_drain_hook` and `_drain_tc_inbox_for_hook` from `BaseAgent`.
9. Remove `pre_request_hook` from `ChatSession` base in `llm/base.py`.

### 6.3 Producer migration (Phase 3)

10. Migrate `_enqueue_email_unread_digest` (`base_agent/messaging.py:51`) to call `envelope.publish` directly.
11. Migrate `_enqueue_system_notification` to a thin wrapper over `envelope.publish` with `source=f"system.notification:{notif_id}"`. Keep the helper signature stable for back-compat.
12. Migrate `intrinsics/soul/flow.py:228` from `tc_inbox.enqueue` to `envelope.publish`.
13. Migrate `intrinsics/system/notification.py` dismiss path from `remove_by_notif_id` + `remove_pair_by_call_id` to `envelope.clear(f"system.notification:{notif_id}")`. The clear call handles both queued-and-spliced cases via the registry's delivery tracking.
14. Migrate `intrinsics/psyche/_molt.py:193, 369` from `_tc_inbox.drain()` to `envelope_registry.clear_all()`.

### 6.4 Tests (Phase 4)

15. Port existing `test_tc_inbox*.py` to envelope tests. Add specific tests for:
    - Supersede with tombstone (header-only marker present, prior call_id structure preserved).
    - Pair-vs-meta delivery selection based on wire tail.
    - Replace-on-meta (envelope on a tool result is replaced when superseded, not duplicated).
    - `tombstone_on_supersede=False` behavior (pure replace, no marker).
    - Idle wake delivers from registry; mid-turn delivery does not double-fire.

### 6.5 Documentation (Phase 5)

16. Update `lingtai_kernel/ANATOMY.md` "Involuntary tool-call pairs" section to describe envelopes. Move historical context to a "History" subsection.
17. Update `base_agent/ANATOMY.md` and `intrinsics/system/ANATOMY.md` for the dismiss path change.
18. Update producer-facing docs (`_enqueue_system_notification` docstring) to reflect the back-compat wrapper.
19. Add identity-prompt sentence teaching the envelope convention.

### 6.6 Release

20. Single bundled commit (or 3-4 commits along Phase boundaries). Bump kernel minor version. Release notes call out the `pre_request_hook` removal and the dismiss-path migration as the two API-visible changes.

Estimated effort: 2-3 days focused work for Phases 1-4, half a day for documentation. The MCP addon repos (sibling repos) need no changes because they call `_enqueue_system_notification` which stays compatible.

## 7. Open questions

Things that genuinely don't have obvious answers and need decision before implementation:

### 7.1 Meta rendering format

Inline text preamble (`[envelope · soul.flow · 23s ago]\n{content}\n\n{original_result}`) is the simplest. Structured field on `ToolResultBlock` (a new `meta` slot the adapters serialize separately) is cleaner but every adapter has to know how to render it. **Recommendation:** start with inline text. Document the convention in `lingtai_kernel/ANATOMY.md`. Graduate to structured field only if a real need emerges.

### 7.2 How verbose should the meta marker be?

Three candidates:

- Minimal: `[envelope · soul.flow]\n{content}`
- Time-included: `[envelope · soul.flow · 23s ago]\n{content}`
- Full: `[envelope · soul.flow · published 23s ago · trigger: timer]\n{content}`

The temporal context matters for the agent's reasoning (a 30s-old voice vs. a 5min-old one hit differently). Trigger context is debug-useful but probably noise for the agent. **Recommendation:** time-included, single line.

### 7.3 What does "closest preceding tool result" actually mean?

If the wire tail is currently `assistant[text]` (the agent is composing a message, not in a tool chain), there is no preceding tool result. Options:
  - Defer to next turn (envelope stays in registry, delivers on next opportunity).
  - Deliver as a pair anyway (same as idle path, but the "wake" is a no-op since the agent is already active).

**Recommendation:** defer to next turn. Don't synthesize new pairs while the agent is composing — that's exactly the mid-turn-coherence problem this redesign exists to fix. The next turn will have either a tool result to attach to or a fresh `user[text]` to pair-deliver alongside.

### 7.4 What happens if a producer publishes during meta delivery?

Race: `_render_envelopes_for_turn` is iterating the registry, decides envelope X delivers as meta on result R. Mid-iteration, producer publishes envelope Y for the same source X.

**Resolution:** publish acquires the registry lock, iteration acquires snapshot under lock. Snapshot semantics: iteration sees a frozen registry state; concurrent publish updates the registry but doesn't affect the in-flight delivery. The new publish will be picked up on the next `_render` call.

### 7.5 Should `tombstone_on_supersede` be opt-in or opt-out?

For status sources (mail count, soul flow voice), one of opt-in/opt-out is wrong:
- Opt-in (default False): mail-as-status correctly skips tombstones; soul flow has to remember to opt in.
- Opt-out (default True): soul flow correctly tombstones; mail-as-status has to remember to opt out.

**Recommendation:** opt-out (default True). Tombstoning is the conservative semantic — it preserves more information by default. Producers that explicitly know their channel is continuous status (mail unread count) opt out.

### 7.6 Backwards compatibility window?

This is a kernel internal change — no public API surface affected. `_enqueue_system_notification` keeps working (becomes a wrapper). `agent._tc_inbox` attribute could be retained as an alias to `agent._envelope_registry` if any external code touches it (none should, but the wrapper package should be grepped).

**Recommendation:** ship in one release, no compat shim. The rename is internal; the tool-pair shape on the wire is unchanged from the agent's POV.

## 8. Risks and rollback

### 8.1 Risk: loss of explicit accumulation

If a future producer wants real per-event accumulation (a notification stream, not status snapshots), they have to mint per-event source keys. Forgetting to do this means events overwrite each other silently.

**Mitigation:** add a lint / runtime warning when a producer republishes the same source within 100ms — likely indicates the producer wants distinct sources but is using a static one. Probably overkill; document the pattern and rely on review.

### 8.2 Risk: meta delivery hides envelopes from agents that aren't reading the convention

If the identity prompt doesn't teach the agent that `[envelope · ...]` preambles are kernel injections, the agent might read a meta-delivered envelope as part of the original tool result and respond as if the tool said it. E.g. agent runs `bash ls`, envelope appends a soul-flow voice as meta, agent thinks bash output included philosophy.

**Mitigation:** identity prompt sentence is mandatory. Add a unit test that the prompt assembly always includes the envelope-convention sentence when any envelope-capable intrinsic is enabled.

### 8.3 Rollback

Phase 2 (adapter cleanup) is the only irreversible step within a release — once `pre_request_hook` is removed from the adapters, restoring it is a real rebuild. Phases 1, 3, 4 are revertable by reverting the commit.

If post-release we find a critical issue, rollback is one revert. The wire format on disk (`chat_history.jsonl`) is unchanged — both designs use the same `(call, result)` shape — so rolling back doesn't require migrating chat histories.

## 9. Appendix — relationship to prior designs

This redesign supersedes / partially supersedes:

- `tc-inbox-mid-turn-drain-patch.md` — the `pre_request_hook` introduction (`f46b346`). This doc reverts that mechanism in favor of meta-on-result, which solves the same problem (mid-turn delivery latency for mail/soul) without the structural splice.

- `tc-injection-service-implementation-proposal.md` — the intermediate refactor that consolidated drain logic into `TCInbox.drain_into()`. That refactor is good and stays; the `drain_into` method becomes part of the envelope renderer's pair-delivery path.

- `email-unread-digest-notification-patch.md` — the prototype this design generalizes. The `email.unread` semantic (single-slot, replace, render-current-state) is what every envelope source becomes.

- `system-injection-audit.md` §1 — catalogues the seven injection paths. This redesign collapses path 1 (synthesized tool-call pairs) into a single envelope mechanism. Paths 2-7 (system prompt rebuilds, MSG_REQUEST text, content prefixes, tool-result dict fields, close_pending_tool_calls, etc.) are out of scope and continue to use their own channels — those are correct as-is for their respective concerns.

- `soul-flow-single-trigger-patch.md` — fixed paying full LLM cost per fire even when coalesced. Continues to apply: `_run_consultation_fire` still runs at most once per fire window. The envelope just changes how the resulting voice is delivered.

The historical evolution makes sense in retrospect: tc_inbox was a queue, then got coalesce semantics, then got `replace_in_history`, then got mid-turn drain. Each step solved a real pain. The cumulative shape — a single-slot status surface with mid-turn re-delivery — *is* the envelope. This doc just names the destination explicitly and removes the scaffolding.
