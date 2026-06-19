# Patch: TC injection service + soul-flow ownership move

> **Status:** discussion / design proposal. Not yet implemented.
> **Author:** human (drafted with Claude Code).
> **Related:** `discussions/base-agent-package-refactor-patch.md` (commit `73e9c04`,
> implemented in `0509f7d`). This patch builds on the package shape that
> refactor produced.

## Motivation

Two distinct problems are tangled in the current `base_agent/soul_flow.py`:

1. **Soul-flow timer + fire orchestration** lives in the kernel coordinator
   even though soul-flow is an intrinsic. The kernel doesn't need to know
   what a consultation is, what a diary cue is, or what `soul_flow.jsonl`
   stores. Today it does — 325 LOC of soul-domain logic sits in a kernel
   submodule.

2. **Synthetic tool-call splicing** (`_drain_tc_inbox`) is also in
   `soul_flow.py`, but it isn't soul-specific. Today five call sites enqueue
   onto `tc_inbox`:

   | Producer | Purpose | Source key |
   |---|---|---|
   | `intrinsics/soul/` (via `base_agent/soul_flow.py:242`) | consultation appendix | `soul.flow` |
   | `base_agent/messaging.py:97` | mail-arrival notification | `system.mail` |
   | `base_agent/turn.py:262, 272` | system notifications (heartbeat, etc.) | `system.*` |
   | `base_agent/turn.py:322` | dismiss-race re-enqueue | (preserves prior source) |

   All four producers build their own `(call, result)` pairs, hand them to
   `tc_inbox.enqueue(...)`, and rely on a single drain implementation
   embedded in `soul_flow.py` to splice them into the wire. Putting drain
   logic in `soul_flow.py` is wrong — it implies splicing is a soul
   concern. It isn't. Splicing is an injection-service concern.

The previous `base_agent` package refactor extracted modules cleanly but
left this concept-tangle in place. This patch fixes it.

## Goal

After this patch:

- **`intrinsics/soul/` owns soul-flow end-to-end** — timer, fire,
  persistence, appendix tracking, rehydration. The kernel calls into soul
  at a few well-defined lifecycle moments (start, state change, shutdown);
  it does not contain soul logic.

- **`tc_inbox.py` owns injection** — enqueue (already there), drain
  (moved in), and the splice protocol (also moved in). Producers are
  decoupled: they share a queue with well-defined enqueue/drain
  semantics, **not** a class hierarchy.

- **`base_agent/soul_flow.py` is deleted.** Its responsibilities are
  redistributed to the two homes above. The kernel coordinator stops
  knowing what soul-flow is.

## Non-goals (out of scope)

- **No new abstract base class.** Producers (soul, mail-notification,
  system-notification, future daemon emanations) keep building their own
  `(call, result)` pairs. They share a *service*, not a parent class.
  Inheritance was considered and rejected: producer lifecycles diverge
  too much (soul has a wall-clock timer, email has a poll thread,
  daemon has emanations, system has signal files). An ABC would either
  be too thin to be useful (just `enqueue`) or too thick to fit all four.

- **No change to producer behavior.** Soul-flow's coalescing semantics
  (`coalesce=True`, `replace_in_history=True`), the appendix single-slot
  invariant, dismiss-by-notif-id — all preserved exactly. The wire
  format does not change.

- **No change to `events.jsonl`, `soul_flow.jsonl`, or `chat_history.jsonl`
  schemas.** This is a code-organization patch, not a behavioral one.

- **No change to the public `BaseAgent` API surface.** The 78 methods on
  the class today remain. Internal pass-throughs may change which module
  they delegate to, but external callers (tests, `lingtai/Agent`,
  capabilities) see no diff.

## Design

### Layer 1 — `tc_inbox.TCInbox` grows a drain method

Today `tc_inbox.py` is queue-only. The drain logic lives at
`base_agent/soul_flow.py:61` (`_drain_tc_inbox`). Move it.

**New method:** `TCInbox.drain_into(interface, appendix_tracker, on_drain_log)`.

```python
class TCInbox:
    def drain_into(
        self,
        interface: "ChatInterface",
        appendix_tracker: dict[str, str],
        on_drain_log: Callable[[int, list[str]], None] | None = None,
    ) -> bool:
        """Splice queued items into the wire chat at a safe boundary.

        Returns False if the interface still has pending tool-calls
        (caller should retry at next safe boundary). Returns True if a
        drain happened or the queue was empty.

        For each drained item:
          - If item.replace_in_history is True, look up appendix_tracker
            for a prior call_id under item.source and remove that pair
            from interface.entries first.
          - Append the (call, result) pair to interface.
          - If item.replace_in_history is True, record item.call.id in
            appendix_tracker under item.source.

        Caller is responsible for save_chat_history after this returns.
        """
```

**Why pass `appendix_tracker` as an argument** (not stored on `TCInbox`):
the tracker semantically belongs to the wire, not the queue. It's
state about *what was spliced*, not *what is queued*. The queue is
stateless across drains; the tracker survives drains. Decoupling them
makes the queue trivially testable.

**Why pass `on_drain_log` as a callback** (not done internally):
`TCInbox` doesn't know about the agent's logger. Keeping logging at
the call site preserves the existing event format
(`tc_inbox_drain {count, sources}`).

**Already-existing `drain()` stays.** `drain_into()` is a higher-level
convenience that calls `drain()` internally. Tests that just want to
inspect the queue contents continue to use `drain()`.

### Layer 2 — soul flow moves to `intrinsics/soul/flow.py`

Create new file `src/lingtai_kernel/intrinsics/soul/flow.py` containing
everything currently in `base_agent/soul_flow.py` *except* the splice
helper (`_drain_tc_inbox`, which moves to `TCInbox.drain_into` in
Layer 1).

**Functions moving:**

| Current location | New location |
|---|---|
| `base_agent/soul_flow.py:_start_soul_timer` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_cancel_soul_timer` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_soul_whisper` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_persist_soul_entry` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_append_soul_flow_record` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_run_inquiry` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_flatten_v3_for_pair` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_run_consultation_fire` | `intrinsics/soul/flow.py` |
| `base_agent/soul_flow.py:_rehydrate_appendix_tracking` | `intrinsics/soul/flow.py` |

`intrinsics/soul/__init__.py` re-exports them at the package level so
existing imports (`from intrinsics.soul import _run_consultation_fire`)
continue to work — same pattern the package already uses for
`config.py`, `consultation.py`, `inquiry.py`.

**Why `flow.py` and not just folding into `consultation.py`:**
consultation is the *content* of a fire (the LLM call sequence,
diary cue, voice rendering). Flow is the *cadence* of fires (timer,
state-gating, post-fire wake, appendix tracking). They have different
test surfaces and different lifecycles. Keeping them as siblings
mirrors the timer/fire split that already exists conceptually.

### Layer 3 — kernel hooks shrink

`base_agent/__init__.py` keeps three thin hooks for soul-flow lifecycle:

```python
# Kernel calls these at lifecycle moments. They are pass-throughs to
# intrinsics/soul/flow.py. Soul-domain logic does not live in the
# kernel; only the call sites do.

def _start_soul_timer(self) -> None:
    from ..intrinsics.soul.flow import _start_soul_timer
    _start_soul_timer(self)

def _cancel_soul_timer(self) -> None:
    from ..intrinsics.soul.flow import _cancel_soul_timer
    _cancel_soul_timer(self)

def _drain_tc_inbox(self) -> None:
    """Splice queued involuntary tool-call pairs at a safe boundary."""
    if self._chat is None:
        try:
            self._session.ensure_session()
        except Exception:
            return
    iface = self._chat.interface
    drained = self._tc_inbox.drain_into(
        iface,
        self._appendix_ids_by_source,
        on_drain_log=lambda count, sources: self._log(
            "tc_inbox_drain", count=count, sources=sources,
        ),
    )
    if drained:
        self._save_chat_history()
```

The kernel still owns:
- `_appendix_ids_by_source: dict[str, str]` (instance attribute on
  `BaseAgent`).
- The decision of *when* to call `_drain_tc_inbox()` (at safe wire
  boundaries — `_handle_request` top, before next `send()`).
- The decision of *when* to start/cancel the soul timer (state
  transitions in `_set_state`, lifecycle events in
  `lifecycle.py`).

The kernel no longer owns:
- What a soul timer is (delay, callback, fire logic).
- What a consultation fire is (diary cue, voice rendering, persistence).
- How a synthetic pair is spliced (drain order, replace-in-history,
  call_id tracking inside the queue).

### Layer 4 — `base_agent/soul_flow.py` deleted

After Layers 1–3, `base_agent/soul_flow.py` is empty. Delete the file.
Update `base_agent/ANATOMY.md`'s Components section to remove the
`soul_flow.py` row. Add a row to `intrinsics/soul/ANATOMY.md` for
`flow.py`.

## Module shape after the patch

```
src/lingtai_kernel/
├── base_agent/
│   ├── __init__.py        # 916 → ~880 LOC (drain stub stays, soul_flow stubs become 3-line passthroughs)
│   ├── identity.py        # 150 (unchanged)
│   ├── lifecycle.py       # 442 (unchanged — calls _start_soul_timer / _cancel_soul_timer hooks)
│   ├── messaging.py       # 161 (unchanged — still enqueues notifications)
│   ├── prompt.py          # 55 (unchanged)
│   ├── soul_flow.py       # DELETED (was 325)
│   ├── tools.py           # 161 (unchanged)
│   └── turn.py            # 425 (unchanged — still calls _drain_tc_inbox)
├── intrinsics/
│   └── soul/
│       ├── __init__.py    # 153 → ~165 (new re-exports for flow.py)
│       ├── ANATOMY.md     # +1 row for flow.py
│       ├── config.py      # 370 (unchanged)
│       ├── consultation.py # 519 (unchanged)
│       ├── flow.py        # NEW ~310 LOC (moved from base_agent/soul_flow.py minus _drain_tc_inbox)
│       └── inquiry.py     # 61 (unchanged)
└── tc_inbox.py            # 106 → ~155 (drain_into method added)
```

**Net LOC delta:** roughly zero (-325 in base_agent, +310 in intrinsics/soul,
+50 in tc_inbox, -35 in base_agent/__init__.py). The patch is about
ownership, not size.

## Test impact

### Tests that must keep passing as-is

Run before and after:
```
tests/test_base_agent.py
tests/test_agent.py
tests/test_soul_flow.py
tests/test_consultation.py        # if exists
tests/test_tc_inbox.py            # if exists
```

The known pre-existing failures (`test_status`, `test_soul_whisper_delegates_to_consultation_fire`,
`test_system_refresh`, `test_lull_rejects_asleep_target`, `test_cpr_agent_hook_returns_agent`,
plus one more) stay failing for the same reasons. Do not pretend to
fix them in this patch.

### mock.patch surface

Search before extracting:

```bash
grep -rn "mock.patch.*soul_flow\|patch(.lingtai_kernel.base_agent.soul_flow" tests/
```

Any test that patches `lingtai_kernel.base_agent.soul_flow.X` needs
its target rewritten to `lingtai_kernel.intrinsics.soul.flow.X`. The
*public* re-export (`intrinsics.soul.X`) also continues to work but
patches on it land in the wrong namespace — the same trap the prior
soul refactor hit. Patches must target the file that *defines* the
function, not where it's re-exported.

The previous refactor (commit `0509f7d`) had zero patch-target
rewrites because all soul logic was already imported lazily and
tests went through the class API. Verify this still holds before
declaring the patch test-clean.

### New tests

One new test file: `tests/test_tc_inbox_drain.py`. Cover:

- Empty queue → `drain_into` returns True, no interface mutation.
- One non-replacing item → call appended, result appended,
  appendix_tracker untouched.
- One `replace_in_history=True` item with no prior tracker entry →
  pair appended, tracker updated.
- One `replace_in_history=True` item with prior tracker entry →
  prior pair removed from interface, new pair appended, tracker
  updated.
- Pending tool-calls in interface → `drain_into` returns False, queue
  unchanged.
- `on_drain_log` callback fires with correct count and sources.

This test file replaces the implicit drain coverage in
`test_soul_flow.py` (which tested drain behavior through the agent's
`_drain_tc_inbox` pass-through). Keep that test for end-to-end
coverage; the new tests verify the unit in isolation.

## Risks

1. **Mock.patch namespace drift.** As above — any test patching
   `base_agent.soul_flow.X` breaks silently if missed. Mitigation:
   grep before, grep after, run targeted suite.

2. **Circular import potential.** `intrinsics/soul/flow.py` imports
   from `..tc_inbox` (for `InvoluntaryToolCall`) and from
   `..llm.interface` (for block types). It also imports
   `.consultation` (already in the soul package). The current
   `base_agent/soul_flow.py` does the same imports. No new cycle is
   introduced. Verify by import-checking the package after move:
   `python -c "import lingtai_kernel.intrinsics.soul.flow"`.

3. **Hot-path lazy imports.** `_drain_tc_inbox` is called on every
   `_handle_request`. Today it's a free function in `base_agent/`.
   After the patch, the kernel hook does the splice inline (no
   import needed) and the queue's `drain_into` is already imported
   at module load. Net: same call cost.

4. **`_appendix_ids_by_source` ownership.** The dict stays on
   `BaseAgent` instance (kernel state, set in `__init__`). The
   queue's `drain_into` *reads and writes* this dict via
   parameter-passing. This is intentional: the dict tracks wire
   state, which is the kernel's domain; the queue is stateless across
   drains. Alternative considered: store the dict on `TCInbox`. Rejected
   because it conflates queue identity with wire identity.

5. **`psyche.py` clears `_appendix_ids_by_source`** in two places
   (`psyche.py:686, 866`) during molt. Verify those clears still
   happen on the same dict on the same agent instance after the
   patch. They will — the dict location doesn't change.

## Implementation outline

Suggested commit shape: **one commit** for atomicity (the patch is a
boundary move; partial moves leave the codebase in a half-state).

Order within the commit:

1. Create `intrinsics/soul/flow.py` with the moved functions. Keep
   imports relative (`from ..state import AgentState`,
   `from ..tc_inbox import InvoluntaryToolCall`, etc.).
2. Add re-exports to `intrinsics/soul/__init__.py`.
3. Add `TCInbox.drain_into(...)` to `tc_inbox.py`.
4. Rewrite `BaseAgent._drain_tc_inbox` in `base_agent/__init__.py`
   to call `self._tc_inbox.drain_into(...)` directly. Remove the
   pass-through that imported from `base_agent/soul_flow.py`.
5. Rewrite `BaseAgent._start_soul_timer` and `_cancel_soul_timer`
   pass-throughs to import from `intrinsics/soul/flow.py`.
6. Delete `src/lingtai_kernel/base_agent/soul_flow.py`.
7. Update `base_agent/ANATOMY.md` (remove soul_flow.py row, note
   that soul-flow logic now lives in intrinsics/soul/flow.py).
8. Update `intrinsics/soul/ANATOMY.md` (add flow.py row, document
   the kernel-hook contract).
9. Update root `ANATOMY.md` if its description of `base_agent/`
   names `soul_flow` (it does — line 13 in the current version).
10. Add `tests/test_tc_inbox_drain.py`.
11. Run targeted suite, verify zero new failures.

## Open questions

1. **Should `_run_inquiry` move too?** It's called from the heartbeat
   loop on `.inquiry` signal files — a cross-cluster call. It's
   already in `base_agent/soul_flow.py:123`. Moving it to
   `intrinsics/soul/flow.py` is consistent (it's soul-domain). But
   inquiry is *agent-invocable* via the `soul` tool, while flow is
   not. They might belong in different files inside the soul package.
   **Suggested resolution:** move `_run_inquiry` to `intrinsics/soul/inquiry.py`
   alongside `soul_inquiry`, not to `flow.py`. That keeps each file
   coherent (flow = mechanical cadence, inquiry = on-demand,
   consultation = LLM call mechanics).

2. **Should `_persist_soul_entry` move?** It writes
   `logs/soul_inquiry.jsonl` and `logs/soul_flow.jsonl`. Both are
   soul-domain. Move it where it's most-called from. Today it's
   called from `_run_inquiry` (heartbeat path) and `intrinsics/soul/__init__.py`
   (via `agent._persist_soul_entry` — a pass-through that today
   resolves to `base_agent/soul_flow.py`). **Suggested resolution:**
   move to `intrinsics/soul/flow.py`, since it logs both inquiry
   and flow modes — it's a soul-package utility.

3. **Should `_append_soul_flow_record` and `_flatten_v3_for_pair` be
   private to `flow.py`?** They have no callers outside soul-flow
   today. **Suggested resolution:** yes — keep them inside `flow.py`,
   do not re-export from `intrinsics/soul/__init__.py`.

4. **Should `_rehydrate_appendix_tracking` be called from soul or
   from kernel?** Today it's called from
   `base_agent/__init__.py` during chat-history rehydration (after
   loading from `chat_history.jsonl` on startup). The function reads
   `agent._chat.interface.entries` and writes
   `agent._appendix_ids_by_source` — a kernel-state mutation. The
   logic of *what counts as a soul-flow appendix pair*, however, is
   soul-domain. **Suggested resolution:** the function stays
   soul-domain (lives in `intrinsics/soul/flow.py`); the kernel
   calls it as a hook during rehydration, the same pattern as
   `_start_soul_timer`.

## Recommendation

Proceed. The patch is mechanical (function moves + one new method),
the boundaries are clear, and the result strictly improves the
ownership model: kernel coordinator does coordinator things, soul
intrinsic owns soul logic, injection queue owns injection.

Risk surface is small (mock.patch namespace, circular imports — both
covered by grep + smoke import).

The estimated 0.5-day implementation budget assumes no test surprises;
the prior `0509f7d` refactor finished in ~2300s of stamina, this is
substantially smaller.
