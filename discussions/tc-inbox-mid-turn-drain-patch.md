# Mid-turn tc_inbox drain: proposal for draining inside the tool-call loop

> **Status:** design proposal. Not yet implemented.
> **Author:** codex-gpt5.5 (drafted per human brief `45a69498`).
> **Related:**
> - `discussions/tc-injection-service-patch.md` — the drain-into-TCInbox refactor (already landed).
> - `discussions/voluntary-compression-patch.md` — no conflict (compression is about
>   tool-result content, not drain timing).

## 1  Problem

`_handle_request` in `base_agent/turn.py:333` calls `agent._drain_tc_inbox()`
once, at request entry — *before* the first `session.send()`.  The tool-call
loop inside `_process_response` (turn.py:514–573) never drains again.  During
a long task (codex routinely runs 20–50+ tool rounds per request), any
`(call, result)` pair enqueued on `tc_inbox` mid-task — mail notifications,
soul.flow voices, future daemon/MCP events — sits queued until the outer turn
ends.

The `MSG_TC_WAKE` posted by producers does not help: the run-loop is inside
`_handle_request` and won't return to `agent.inbox.get()` until the entire
request finishes.

**Live evidence** (codex-gpt5.5 dev network, 2026-05-04):

| Event | Enqueued at | Effect |
|---|---|---|
| Mail `aba0ea68` | 02:42:52 mid-task | No `tc_wake_dispatch` for ~5 min until bash chain ended |
| Mail `f46271c8` | 02:23:20 | Hit `tc_wake_noop reason=pending_tool_calls`; never spliced — lost until self-polling |

Both producers (mail notifications and soul.flow) share the same bottleneck.
Soul masks the latency via `coalesce=True` + 5-min timer retries; mail just
sits because `coalesce=False` (every mail is a distinct slot).

## 2  Candidate sites

### 2a  After `session.send(tool_results)` — text-only gate

**Where:** Inside `_process_response`, after line 571 (`response =
agent._session.send(tool_results)`), only when `response.tool_calls` is empty.

**Mechanism:** One drain call:

```python
response = agent._session.send(tool_results)
agent._save_chat_history()
if not response.tool_calls:
    agent._drain_tc_inbox()      # ← new
```

**Wire safety:** After `session.send()` returns, the adapter has appended the
new assistant response to the interface.  If the response has no tool_calls,
`has_pending_tool_calls()` returns False — safe boundary.

**Verdict:** Trivial to implement (one line), but **rarely triggers**.  In a
typical 20-round tool loop, almost every LLM response emits tool_calls.  The
only text-only response is the *final* one, which is the same boundary where
the drain already fires (end of `_handle_request`).  This gives near-zero
latency improvement for the common case.

---

### 2b  Before `session.send(tool_results)` — commit-then-drain

**Where:** Inside `_process_response`, before line 571.

**Mechanism:** Commit tool results to the interface directly (bypassing the
adapter's commit phase), drain, then tell the adapter to skip re-committing:

```python
# Phase 1: commit results to wire
agent._session.interface.add_tool_results(tool_results)

# Phase 2: drain at safe boundary
agent._drain_tc_inbox()

# Phase 3: API call only (adapter must not re-commit)
response = agent._session.send_committed()   # ← new adapter method
```

**Wire safety:** After `add_tool_results()`, the interface tail is
`user[tool_results]` — `has_pending_tool_calls()` returns False.  Guaranteed
safe.

**Verdict:** Architecturally clean safe boundary, but **cross-cuts every LLM
adapter**.  Requires a new `send_committed()` method on the `ChatSession` ABC
and implementations in all 8+ adapter session classes.  Also changes the
contract of `send()` — adapters must now handle pre-committed results, which
is a larger refactor than a one-line hook.

---

### 2c  Inside the adapter's `send()` — pre-request hook

**Where:** Inside every adapter's `send()`, between the commit phase
(`add_tool_results` / `add_user_message`) and the API call.

**Mechanism:** Add an optional `pre_request_hook` callback to the
`ChatSession` ABC.  The kernel installs `_drain_tc_inbox` as that hook.
Each adapter calls the hook after committing the message but before the API
request.

**Wire safety:** Same as (b) — after `add_tool_results()`, the tail is
`user[tool_results]`, `has_pending_tool_calls()` returns False.

**Verdict:** Requires one-line changes in each adapter (8 send methods across
7 files), plus the hook definition in `base.py` and hook installation in the
kernel.  Clean separation of concerns: the adapter doesn't know *what* the
hook does, only that it fires at a safe boundary.

---

## 3  Wire-state invariant proof (option c)

The invariant we must maintain: **`drain_into` only fires when
`has_pending_tool_calls()` is False.**

### 3.1  When `send(message)` receives a `list` (tool results)

Every adapter follows this sequence:

```
① interface.add_tool_results(message)     ← commits results, tail becomes user[results]
② interface.enforce_tool_pairing()         ← cleans orphans
③ [hook fires here]                        ← drain runs; wire is safe
④ convert interface → provider format
⑤ API call → LLM
⑥ parse response
⑦ interface.add_assistant_message(...)     ← tail becomes assistant[...]
⑧ return LLMResponse
```

At point ③:
- The tail entry is `user[tool_results]` (set by ①).
- `has_pending_tool_calls()` inspects the tail for `ToolCallBlock` — there are
  none in a `user[tool_results]` entry.  Returns False.
- `drain_into` checks `has_pending_tool_calls()` → False → proceeds.

**Invariant holds.**

### 3.2  When `send(message)` receives a `str` (user text)

At point ①, `add_user_message(text)` is called.  If the tail has pending
tool_calls, it raises `PendingToolCallsError` — the send never reaches the
hook.  If the tail has no pending calls, the tail becomes `user[text]` — same
safe state as 3.1.

For the initial request in `_handle_request`, the entry drain (line 333) has
already fired.  The hook at point ③ would be redundant but harmless — the
queue is likely empty.

**Invariant holds.**

### 3.3  Edge case: adapter appends assistant before hook

No adapter appends the assistant response before the hook.  The assistant
response is always appended *after* the API call (point ⑦), which is after the
hook (point ③).  This is enforced by the adapter's control flow: the API call
must happen before the response can be parsed and appended.

**Invariant holds.**

## 4  Race and reentrancy

### 4.1  `replace_in_history` and `coalesce` (soul.flow)

**Concern:** If the drain fires mid-task and splices a soul.flow pair with
`replace_in_history=True`, it removes the prior pair from `interface.entries`.
The LLM's next `send()` call will serialize a wire *without* the prior pair.
The LLM conditioned on a history that contained that pair — could this confuse
it?

**Analysis:** This is identical to what happens today at turn boundaries.  The
entry drain at `_handle_request:333` already replaces soul.flow pairs between
turns.  Each `send()` call re-serializes the full interface from scratch (no
cached state) — the adapter does not maintain a provider-side cache of the
wire.  The LLM always sees the *current* state of the interface.

The soul.flow pair is a synthetic tool call that the agent did not initiate.
The agent's diary already records the voice content.  Replacing it mid-turn is
semantically equivalent to replacing it between turns.  The LLM may notice the
history changed, but it has no dependency on the prior pair's presence.

**`coalesce=True` without `replace_in_history`:** handled entirely on the
queue side.  If soul.flow fires twice during a busy stretch, the second
enqueues replaces the first (same source key).  Only the latest version is
drained.  No wire mutation needed.

**Verdict:** No new risk.  The mid-turn drain is strictly better than letting
the pair sit stale in the queue for minutes.

### 4.2  Thread safety

`TCInbox.enqueue()` is lock-protected (`self._lock`).  `TCInbox.drain()` (called
inside `drain_into`) acquires the same lock.  The drain copies the list under
the lock, then splices outside it — so `ChatInterface` mutations don't hold
the lock.

The hook fires on the main agent thread (inside `session.send()`).  Producers
fire on background threads (soul timer thread, mail listener thread).  The
lock serializes enqueue and drain.  No race.

**`tc_inbox.drain_into` is idempotent with respect to empty queues** — if
the queue is empty, it returns immediately.  Calling it multiple times per
turn (entry drain + mid-turn hook) is safe.

### 4.3  Recursive drain (tool triggers mail to self)

If a tool's execution sends mail to self (e.g., `_inject_notification`), the
notification producer enqueues a new item on `tc_inbox` during the tool
execution phase.  The item sits in the queue.  When the next `session.send()`
fires (next tool round), the hook drains it.

**No recursive drain:** the drain happens in the hook, which is inside
`session.send()`.  Tools don't call `session.send()`.  The drain doesn't
execute tools.  No recursion path exists.

### 4.4  Double drain (entry drain + mid-turn hook)

The entry drain at `_handle_request:333` may drain some items.  The mid-turn
hook drains new items that arrived during the tool loop.  Because `drain()`
atomically empties the queue (list swap under lock), the second drain only
sees items enqueued *after* the first drain.  No double-splice.

### 4.5  `save_chat_history` during drain

The drain calls `_save_chat_history()` when items are spliced (line 616 in
`base_agent/__init__.py`).  In the tool loop, `_save_chat_history()` is also
called after each `session.send()` (line 573).  If the drain saves mid-send,
the saved history includes the drain items but not the new assistant response.
If the process crashes between the drain save and the send completion, the
wire has a clean state: the drain items are persisted, and the pending
tool_calls from the previous assistant turn are recoverable by AED.

**No change to save behavior needed.**  The existing drain saves; the existing
tool-loop saves.  They compose correctly.

## 5  Test surface

### 5.1  New tests required

| Test | What it verifies |
|---|---|
| `test_mid_turn_drain_splices_between_rounds` | Multi-tool-round LLM response; enqueue an item between rounds via a background thread; assert the item appears in the interface before the outer turn ends. |
| `test_coalesce_mid_turn_only_latest_survives` | Soul.flow fires twice mid-turn with `coalesce=True`; only the latest version is spliced; the prior queue entry was replaced. |
| `test_replace_in_history_mid_turn` | Soul.flow pair with `replace_in_history=True` fires mid-turn; prior pair of the same source is removed from interface; new pair replaces it. |
| `test_no_double_splice_entry_and_hook` | Entry drain drains item A; mid-turn hook drains item B (enqueued after entry drain).  Item A appears exactly once; item B appears exactly once. |
| `test_hook_does_not_fire_on_send_error` | Adapter's API call raises; hook should have already fired (between commit and API).  Drain items are in the interface; AED recovery handles the error. |
| `test_mail_notification_mid_turn_latency` | Enqueue a mail notification during tool execution; verify it's spliced within the next tool round, not at turn end. |

### 5.2  Existing tests to re-verify

```
tests/test_base_agent.py
tests/test_tc_inbox.py
tests/test_soul_flow.py
tests/test_consultation.py
tests/test_agent.py
```

No existing test should break — the change is additive (new drain point, no
removal of existing drain points).  The entry drain still fires; the tool-loop
drain is new.

### 5.3  mock.patch surface

The hook is installed via `session.pre_request_hook = agent._drain_tc_inbox`.
Tests that mock `_drain_tc_inbox` will continue to work — the hook calls the
same method.  No new patch targets needed.

## 6  Recommendation

**Ship option (c) — the pre-request hook.**

Rationale:

| Criterion | (a) text-only gate | (b) commit-then-drain | (c) pre-request hook |
|---|---|---|---|
| Latency improvement | Near zero | Full | Full |
| Adapter changes | 0 files | 8 methods + new ABC | 8 one-line additions |
| Kernel changes | 1 line | 3 lines + new session method | 3 lines + hook install |
| Wire safety proof | Trivial | Strong | Strong |
| Risk | None | Medium (contract change) | Low (additive) |

Option (a) is trivial but doesn't solve the problem.  Option (b) is clean but
requires a new adapter method (`send_committed`) that changes the contract of
every session class.  Option (c) achieves the same wire safety with minimal
adapter changes — each adapter adds one line (the hook call) and keeps its
existing `send()` contract unchanged.

### 6.1  Code sketch

**`src/lingtai_kernel/llm/base.py` — ChatSession ABC:**

```python
class ChatSession(ABC):
    # ... existing attributes ...

    # Optional hook fired after the message is committed to the interface
    # but before the API request is made.  Receives the ChatInterface.
    # Installed by the kernel to enable mid-turn tc_inbox draining.
    pre_request_hook: "Callable[[ChatInterface], None] | None" = None
```

No default implementation needed — it's a simple attribute check.

**Each adapter's `send()` — one line added:**

```python
# anthropic/adapter.py (and every other adapter)
def send(self, message) -> LLMResponse:
    # --- Phase 1: commit message to interface ---
    if isinstance(message, str):
        self._interface.add_user_message(message)
    elif isinstance(message, list):
        self._interface.add_tool_results(message)

    # --- Hook: safe boundary for mid-turn drains ---
    if self.pre_request_hook:                         # ← NEW LINE
        self.pre_request_hook(self._interface)        # ← NEW LINE

    # --- Phase 2: API call ---
    self._interface.enforce_tool_pairing()
    candidate_msgs = to_anthropic(self._interface)
    # ... rest unchanged ...
```

**`src/lingtai_kernel/base_agent/__init__.py` — hook installation:**

```python
def _install_drain_hook(self) -> None:
    """Install the mid-turn tc_inbox drain hook on the chat session."""
    if self._chat is not None and hasattr(self._chat, 'pre_request_hook'):
        self._chat.pre_request_hook = lambda iface: self._drain_tc_inbox()
```

Called from `_ensure_chat()` or wherever the session is first created.

**`src/lingtai_kernel/base_agent/turn.py` — no changes needed.**

The existing entry drain at line 333 stays.  The mid-turn drain happens via
the hook.  Both drain the same queue; no coordination needed.

### 6.2  List of adapters requiring the hook line

| File | send() line(s) | Count |
|---|---|---|
| `lingtai/llm/anthropic/adapter.py` | 337 | 1 |
| `lingtai/llm/openai/adapter.py` | 500, 868, 1255 | 3 |
| `lingtai/llm/gemini/adapter.py` | 152, 364 | 2 |
| `lingtai/llm/deepseek/adapter.py` | 76 | 1 |
| `lingtai/llm/minimax/adapter.py` | (check) | 1 |
| `lingtai/llm/custom/adapter.py` | (check) | 0–1 |
| `lingtai/llm/openrouter/adapter.py` | (check) | 0–1 |
| **Total** | | **8–10** |

Each change is two lines (guard + call).  Adapters that delegate to another
adapter's session (e.g., openrouter → openai) may inherit the hook via
composition — verify during implementation.

### 6.3  Interaction with existing systems

**AED recovery:** If the API call fails after the hook has drained items, AED
rebuilds the session.  The drained items are in the interface (persisted by the
drain's `_save_chat_history`).  AED's `close_pending_tool_calls` handles the
failed assistant turn.  No data loss.

**Molt:** The drain flushes the queue before molt.  No change needed — the
existing drain at `_handle_request` entry already handles this; the mid-turn
drain is additive.

**Soul flow `replace_in_history`:** The hook fires at every tool round.  If
soul.flow enqueues a replacement pair mid-turn, the next tool round's drain
replaces the prior pair.  The LLM sees the updated pair on its next `send()`.
This is identical to turn-boundary behavior.  See §4.1.

**TC wake path (`_handle_tc_wake`):** Unaffected.  The wake path has its own
drain at line 410 and its own tool loop.  If the wake path's tool loop also
needs mid-turn drain, the hook covers it — the hook is on the session, not
on `_handle_request`.

## 7  Implementation order

1. Add `pre_request_hook` attribute to `ChatSession` in `llm/base.py`.
2. Add the two-line hook call to each adapter's `send()`.
3. Add `_install_drain_hook()` to `BaseAgent` and call it from session
   creation path.
4. Write the 6 new tests from §5.1.
5. Run existing test suite.
6. Commit.  No push — leave for human review.

Estimated scope: ~30 lines of production code, ~150 lines of test code.
Single commit.

## 8  Open questions

1. **Should the entry drain at `_handle_request:333` be removed?**  No.  It
   handles items enqueued between the inbox message and the first `send()`.
   Removing it would create a gap.  The two drains are complementary:
   entry drain covers "before the first round," hook drain covers "between
   rounds."

2. **Should the hook also fire for `str` (user text) sends?**  Yes —
   consistency.  The entry drain likely emptied the queue, so the hook drain
   is a no-op.  But if a producer fires between the entry drain and the first
   send (a narrow window), the hook catches it.

3. **Should we gate the hook on `isinstance(message, list)` to only fire for
   tool-result sends?**  No — the hook should fire unconditionally.  The drain
   itself handles the "queue empty" case cheaply.  Gating adds complexity for
   no measurable benefit.

4. **Should the `_GatedSession` wrapper call the hook?**  No — the hook must
   fire *inside* the adapter's `send()`, after the adapter commits the message
   to the interface.  The gate wraps the entire `send()` call; it cannot
   inject code between the commit and the API phases.
