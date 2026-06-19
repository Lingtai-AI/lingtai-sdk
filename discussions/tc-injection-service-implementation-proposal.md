# Implementation proposal: TC injection service + soul-flow ownership move

> **Re:** `discussions/tc-injection-service-patch.md` (commit `22410df`)
> **Status:** propose-only, not implementation

## Review summary

The patch is well-designed. I agree with the boundary moves, the open question resolutions, and the one-commit shape. My changes are narrow: two implementation details in the `drain_into` method, one import-path correction, and one observation about the LLM-hang context that prompted both tasks.

## Responses to the 4 open questions

### 1. Should `_run_inquiry` move too? → `intrinsics/soul/inquiry.py`

**Agreed.** The patch's suggested resolution is correct: `_run_inquiry` belongs in `inquiry.py` alongside `soul_inquiry`. The reasoning is clean — inquiry is agent-invocable, flow is mechanical. Placing them in different files mirrors the conceptual split.

One nuance: `_run_inquiry` currently imports from `..intrinsics.soul` (lazily, to get `soul_inquiry`). After the move, it's *inside* `intrinsics/soul/inquiry.py`, so the import becomes `from .inquiry import soul_inquiry` — a sibling import. No circular risk since `soul_inquiry` is already defined in that file. The body becomes:

```python
def _run_inquiry(agent, question: str, source: str = "agent") -> None:
    try:
        result = soul_inquiry(agent, question)  # direct call, same module
        if result:
            agent._log("insight", ...)
            _persist_soul_entry(agent, result, mode="inquiry", source=source)
        ...
```

Wait — `_persist_soul_entry` is moving to `flow.py`. So `inquiry.py` would import from `.flow`. That creates an `inquiry → flow` dependency. Is that a problem? No — `flow.py` has no reason to import from `inquiry.py`, so the dependency is one-directional. But it means `inquiry.py` is not a leaf anymore. Acceptable.

### 2. Should `_persist_soul_entry` move? → `intrinsics/soul/flow.py`

**Agreed.** Both `soul_inquiry.jsonl` and `soul_flow.jsonl` are soul-domain logs. It's a soul-package utility. `_run_inquiry` (now in `inquiry.py`) will import it from `.flow`.

### 3. Should `_append_soul_flow_record` and `_flatten_v3_for_pair` be private? → yes

**Agreed.** No callers outside soul-flow. Keep them inside `flow.py`, do not re-export from `__init__.py`.

### 4. Should `_rehydrate_appendix_tracking` be called from soul or kernel? → soul-domain, kernel calls as hook

**Agreed.** The function reads `agent._chat.interface.entries` (kernel state) and writes `agent._appendix_ids_by_source` (kernel state), but the *logic* of what constitutes a soul-flow appendix pair is soul-domain. Same pattern as `_start_soul_timer` — kernel calls soul at lifecycle moments.

Verified: `psyche.py:686-687` and `psyche.py:865-866` clear `agent._appendix_ids_by_source` during molt. The dict stays on `BaseAgent.__init__`, so these clears continue to work. No change needed.

## mock.patch surface

**Zero patch targets need rewriting.** Grep confirms:

```bash
$ grep -rn "mock.patch.*soul_flow\|patch.*base_agent\.soul_flow" tests/
(no results)
```

All soul-related tests either:
- Import from `lingtai_kernel.intrinsics.soul` (the package re-export) — those continue to work via `__init__.py` re-exports.
- Use `patch.object(agent, "_run_consultation_fire")` (the pass-through) — those continue to work because the pass-through stub stays on `BaseAgent`.

The `test_soul_whisper_delegates_to_consultation_fire` and `test_soul_whisper_swallows_consultation_fire_error` failures are pre-existing (they were already broken before `0509f7d`). The patch does not change their status.

## Implementation detail: `drain_into` method signature

The patch proposes:

```python
def drain_into(self, interface, appendix_tracker, on_drain_log=None) -> bool:
```

Two concerns:

**1. The `on_drain_log` callback is over-engineered.** The existing `_drain_tc_inbox` does two things after draining: logs and calls `_save_chat_history`. The logging is trivial (one `agent._log` call). Wrapping it in a callback adds indirection for a single call site. Better: return the drained items (or a summary) and let the caller log directly. This keeps `TCInbox` purely a queue/splicer with no callback injection.

Proposed alternative:

```python
@dataclass
class DrainResult:
    drained: bool  # True if drain happened or queue was empty; False if blocked
    count: int = 0
    sources: list[str] = field(default_factory=list)
```

Then the kernel hook becomes:

```python
def _drain_tc_inbox(self) -> None:
    if self._chat is None:
        try:
            self._session.ensure_session()
        except Exception:
            return
    result = self._tc_inbox.drain_into(self._chat.interface, self._appendix_ids_by_source)
    if result.count > 0:
        self._log("tc_inbox_drain", count=result.count, sources=result.sources)
        self._save_chat_history()
```

Simpler, no callbacks, testable (assert on `DrainResult` fields).

**2. The return type should distinguish "blocked" from "empty."** The patch says `False` means "still has pending tool-calls, caller should retry." But `True` covers both "drained items" and "queue was empty." Callers (the kernel hook) need to know if there were items to decide whether to save chat history. The `DrainResult` dataclass handles this cleanly.

## Implementation detail: import path correction

The patch says `intrinsics/soul/flow.py` imports `from ..state import AgentState`. This is correct — `..` goes up to `lingtai_kernel/`, then into `state.py`. Same pattern used successfully in the `base_agent` refactor's sub-modules.

## Circular import verification

No new cycles introduced. The dependency graph after the patch:

```
base_agent/__init__.py
  ├── imports from base_agent/lifecycle.py (lazy)
  ├── imports from base_agent/turn.py (lazy)
  ├── calls self._tc_inbox.drain_into() (inline, no import)
  └── calls intrinsics.soul.flow._start_soul_timer (lazy)

base_agent/lifecycle.py
  └── calls agent._start_soul_timer (pass-through → intrinsics/soul/flow)
  └── calls agent._run_inquiry (pass-through → intrinsics/soul/inquiry)

base_agent/turn.py
  └── calls agent._drain_tc_inbox (pass-through → inline drain)
  └── calls agent._cancel_soul_timer (pass-through → intrinsics/soul/flow)

intrinsics/soul/flow.py
  ├── from ..state import AgentState
  ├── from ..tc_inbox import InvoluntaryToolCall
  ├── from ..message import _make_message, MSG_TC_WAKE
  └── from .consultation import ... (existing, no change)

intrinsics/soul/inquiry.py
  ├── from .inquiry import soul_inquiry (sibling, same file)
  └── from .flow import _persist_soul_entry (one-directional)

tc_inbox.py
  └── from ..llm.interface import (TYPE_CHECKING only, already exists)
```

No cycles. Smoke import: `python -c "import lingtai_kernel.intrinsics.soul.flow"`.

## Test additions

The patch's proposed `tests/test_tc_inbox_drain.py` is well-scoped. I'd add one more case:

- **Multiple items with different sources** — verify all drain in order, all tracked independently in `appendix_tracker`.

## ANATOMY.md updates needed

1. `base_agent/ANATOMY.md`: Remove `soul_flow.py` row from Components. Add note: "Soul-flow logic now lives in `intrinsics/soul/flow.py`."
2. `intrinsics/soul/ANATOMY.md`: Add `flow.py` row. Add `inquiry.py` update (now contains `_run_inquiry`).
3. `src/lingtai_kernel/ANATOMY.md`: Update `base_agent/` description (it currently lists "soul_flow" as one of the 7 sub-modules).

## Recommendation

**Proceed.** The patch is sound, the boundaries are correct, and the implementation is mechanical. My two refinements (DrainResult instead of callback, return type distinguishes blocked/empty) are small and optional — the callback approach works, it's just slightly less clean.

Suggested commit message:
```
refactor(soul,tc_inbox): move soul-flow logic to intrinsics/soul/flow.py; add TCInbox.drain_into

Soul-flow domain logic (timer, fire, persistence, appendix tracking)
moves from base_agent/soul_flow.py to intrinsics/soul/flow.py.
_run_inquiry moves to intrinsics/soul/inquiry.py. TCInbox grows a
drain_into() method that owns the splice protocol. base_agent/soul_flow.py
is deleted. Kernel hooks become 3-line pass-throughs to intrinsics/soul/flow.
```

## Stamina

~35,800s at time of writing. Comfortable.
