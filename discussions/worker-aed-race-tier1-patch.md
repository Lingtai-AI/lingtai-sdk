# Proposal: Tier 1 safety fix for LLM-worker / AED race

> **Status:** discussion / design proposal. Not yet implemented.
> **Motivated by:** live incident 2026-05-03 23:36–23:42 where `codex-gpt5.5` hit `LLM worker thread still running after 300s + 5s grace`, then AED attempt 2 failed with `Cannot append user message while the tail assistant turn has unanswered tool_calls`.
> **Companions:** `discussions/llm-hang-watchdog-patch.md` (visibility signal) and `discussions/llm-read-timeout-audit-patch.md` (adapter timeout reduction).

## Problem

`llm_utils._send()` already knows the dangerous condition:

```python
# llm_utils.py
_wait_for_worker_settle(future, elapsed, agent_name)
raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
```

`_wait_for_worker_settle()` waits `_WORKER_SETTLE_GRACE = 5.0` seconds for the provider worker to finish after the main-thread timeout. If the worker is still alive, it currently logs:

```text
LLM worker thread still running after 300s + 5s grace — interface state may be inconsistent.
```

Then it returns normally, and `_send()` raises an ordinary `TimeoutError`.

That is the bug: an ordinary timeout means "the provider call failed, but the worker is done or cleaning up." The observed condition means "the provider worker may still be mutating the same `ChatInterface` AED is about to repair and retry against." Those two states need different control flow.

The current design lets AED proceed against a shared interface after logging that the interface may be inconsistent. In the live incident, the sequence was:

1. provider worker hung beyond 300s + 5s;
2. `_send()` raised ordinary `TimeoutError`;
3. AED attempt 1 called `close_pending_tool_calls()` and injected a recovery user message;
4. the still-running provider worker mutated the adapter-held `agent._chat.interface` reference again;
5. AED attempt 2 tried to append a user message and hit `PendingToolCallsError` because the tail was again `assistant[tool_calls]`.

## Goal

Tier 1 should be a minimal safety fix:

- distinguish **worker still alive after grace** from ordinary provider timeout;
- stop AED from retrying on a possibly-live, shared `ChatInterface`;
- preserve current ordinary-timeout behavior when the worker settles during grace;
- leave the deeper transactional-interface redesign to Tier 2.

Non-goals:

- Do not redesign adapters in this patch.
- Do not solve total HTTP timeout semantics here; that remains the read-timeout patch.
- Do not make provider workers cancellable; Python cannot safely kill the stuck thread.

---

## Core change: raise a distinct exception

Add a domain exception in `llm_utils.py`:

```python
class WorkerStillRunningError(TimeoutError):
    """Raised when the main-thread LLM timeout expires but the provider
    worker is still alive after the settle grace period.

    This is stronger than an ordinary TimeoutError: the shared ChatInterface
    may still be owned by the provider adapter, so AED must not repair/retry
    against it in-process.
    """

    def __init__(self, *, elapsed: float, grace: float, agent_name: str):
        self.elapsed = elapsed
        self.grace = grace
        self.agent_name = agent_name
        super().__init__(
            f"LLM worker still running after {elapsed:.0f}s + {grace:.0f}s grace; "
            "ChatInterface is unsafe for AED retry"
        )
```

Change `_wait_for_worker_settle()` from "log and proceed" to "log and raise":

```python
def _wait_for_worker_settle(future: Future, elapsed: float, agent_name: str) -> None:
    try:
        future.result(timeout=_WORKER_SETTLE_GRACE)
    except TimeoutError:
        _logger.error(
            "[%s] LLM worker thread still running after %.0fs + %.0fs grace — "
            "interface state may be inconsistent. Refusing AED retry.",
            agent_name, elapsed, _WORKER_SETTLE_GRACE,
        )
        raise WorkerStillRunningError(
            elapsed=elapsed,
            grace=_WORKER_SETTLE_GRACE,
            agent_name=agent_name,
        )
    except Exception:
        # Worker raised something while settling; ordinary timeout semantics
        # remain. Its adapter except-block should already have run cleanup.
        pass
```

Then `_send()` remains structurally the same. Calls at both timeout exits already invoke `_wait_for_worker_settle()`; if the worker settles, `_send()` still raises ordinary `TimeoutError`. If not, the distinct exception escapes instead.

```python
if remaining <= 0:
    _wait_for_worker_settle(future, elapsed, agent_name)
    raise TimeoutError(f"LLM API call timed out after {elapsed:.0f}s")
```

Because `WorkerStillRunningError` subclasses `TimeoutError`, broad compatibility is preserved for outer callers that only log the error string. The run loop still needs a specific branch before the generic AED branch.

---

## Caller behavior: choose fail-closed, not in-process session refresh

The caller seeing `WorkerStillRunningError` is `base_agent/turn.py` through `_send_with_watchdog()` and `_handle_request()` / `_handle_tc_wake()`. It currently treats all exceptions the same inside the AED loop:

1. increment `aed_attempts`;
2. call `close_pending_tool_calls()`;
3. set `STUCK`;
4. rebuild the session and inject a recovery message;
5. retry until exhaustion.

For `WorkerStillRunningError`, step 2 and step 4 are exactly what we must avoid.

### Option A: fail closed into STUCK/ASLEEP

On `WorkerStillRunningError`:

- log `llm_worker_still_running` / `aed_unsafe_retry_blocked`;
- set state to `STUCK` for visibility;
- do **not** call `close_pending_tool_calls()`;
- do **not** call `_session._rebuild_session()` and retry;
- do **not** inject a recovery prompt into the possibly-live interface;
- stop the current turn and enter a quiescent state.

Pure "set STUCK and let heartbeat later move to ASLEEP" is not quite enough under the current run loop. The run loop does not block inbox processing while `agent._state == STUCK`; it will accept a new message and set `ACTIVE` at the top of the next turn. That means a human ping during the heartbeat wait could restart LLM work while the old worker is still alive.

So the Tier 1 version of this option should be **fail-closed STUCK → ASLEEP for processing**, not just passive STUCK:

```python
except WorkerStillRunningError as e:
    err_desc = str(e)
    agent._log("llm_worker_still_running", error=err_desc)
    agent._set_state(AgentState.STUCK, reason=err_desc)
    # Keep the agent reachable by mail, but do not process another LLM turn
    # in this process after an unsafe interface timeout.
    agent._asleep.set()
    sleep_state = AgentState.ASLEEP
    break
```

This mirrors AED exhaustion's operational shape: the process remains alive and mail can wake it later, but the unsafe AED retry chain stops immediately.

Caveat: ASLEEP does not kill the stuck provider worker. If mail wakes the same process immediately, the old worker may still be alive. For that reason this option is safe only if the supervisor/TUI treats `.llm_hang` / `llm_worker_still_running` as a prompt to refresh the process before asking it to work again. If no supervisor refresh exists, the run loop should consider staying ASLEEP until explicit refresh or until the stuck future eventually completes — but the current code has no direct handle exposed at the run-loop level once `_send()` returns with `WorkerStillRunningError`.

### Option B: attempt in-process session refresh from persisted history

A tempting alternative is: catch `WorkerStillRunningError`, rebuild a fresh `ChatInterface` from `chat_history.jsonl`, create a new adapter session, then continue.

Pros:

- It can isolate the **current** `agent._session.chat` from the old adapter-held interface object. The still-running worker holds a reference to the old adapter / old interface; if the agent swaps to a new session, late old-worker mutations should not touch the new canonical interface.
- It offers faster autonomous recovery than sleeping.

Cons:

- `Session` owns `self._timeout_pool = ThreadPoolExecutor(max_workers=1)`. If the old provider call is still occupying that single worker, in-process retries submitted to the same pool will queue behind the stuck job and can fail as `cannot schedule new futures after shutdown` or never execute.
- A correct in-process refresh would therefore need to replace the timeout pool as well as the chat session, and must ensure the old future cannot later save or affect canonical history. That is already beyond a minimal Tier 1 patch.
- Rebuilding from persisted history may persist a snapshot taken while the old worker was mid-mutation. The current `agent._save_chat_history()` calls are not a transactional barrier against the provider worker.
- The live incident already showed refresh/relaunch machinery is the robust escape hatch; trying to invent a lighter in-process refresh here risks a second recovery mechanism with subtle ownership bugs.

### Recommendation

Pick **Option A, fail closed**, for Tier 1.

Specifically: `WorkerStillRunningError` should stop AED retries and put the agent into a visible unsafe/hung state, preferably ASLEEP after logging STUCK, without touching the live interface. The main reason is correctness: once a worker is still alive after grace, the process no longer has exclusive ownership of `ChatInterface`, and Tier 1 should not pretend it does.

If the product wants automatic recovery, the safe automation is **process refresh**, not in-process session refresh. `base_agent/lifecycle.py::_perform_refresh()` already uses a `.refresh` handshake and deferred relaunch. A future Tier 1.5 could decide to call `_perform_refresh()` on `WorkerStillRunningError` after saving only durable, already-committed state. I would not include that in the first patch because process refresh from inside an active error path needs its own focused test coverage.

---

## Interaction with `.llm_hang` watchdog

The newly shipped watchdog in `base_agent/turn.py` fires independently of `llm_utils._send()`:

- at 120s, `_on_llm_hang()` logs `llm_hang_detected`, writes `.llm_hang`, and calls `_set_state(STUCK, reason="LLM API unresponsive")`;
- at 300s, `_send()` reaches `retry_timeout`;
- at 305s, `_wait_for_worker_settle()` either observes worker settlement or raises `WorkerStillRunningError`.

These layers should cooperate as follows:

### Flow: worker eventually settles

1. 120s watchdog fires: state becomes `STUCK`, `.llm_hang` appears, soul timer is cancelled by `_set_state()`.
2. Worker raises or returns during the 300–305s settle window.
3. `_wait_for_worker_settle()` swallows the worker exception (current ordinary-timeout behavior).
4. `_send()` raises ordinary `TimeoutError`.
5. `_send_with_watchdog()` `finally` cancels the timer and removes `.llm_hang`.
6. AED can safely close pending tool calls and retry because the worker is done.

### Flow: worker still alive after grace

1. 120s watchdog fires: state becomes `STUCK`, `.llm_hang` appears, soul timer is cancelled.
2. 305s worker-settle check raises `WorkerStillRunningError`.
3. `_send_with_watchdog()` should cancel the timer but **must not delete `.llm_hang`** for this exception. The hang is not transient; the worker is still alive.
4. Run loop special-cases `WorkerStillRunningError`, logs, refuses AED retry, and enters the fail-closed state.

Implementation shape:

```python
def _send_with_watchdog(agent, content):
    from ..llm_utils import WorkerStillRunningError

    keep_hang_signal = False
    hang_timer = threading.Timer(...)
    hang_timer.start()
    try:
        return agent._session.send(content)
    except WorkerStillRunningError:
        keep_hang_signal = True
        # Optionally update .llm_hang with worker_still_running_at/error.
        raise
    finally:
        hang_timer.cancel()
        if not keep_hang_signal:
            (agent._working_dir / ".llm_hang").unlink(missing_ok=True)
```

This avoids a state-machine fight:

- the 120s watchdog owns early visibility;
- the 305s exception upgrades the visible condition from "LLM slow/hung" to "worker still running; interface unsafe";
- neither layer performs chat-interface repair;
- `.llm_hang` remains a durable TUI/supervisor clue until refresh or a later successful send cleans it.

I would also extend the signal-file payload:

```json
{
  "detected_at": 1777875600.0,
  "threshold_seconds": 120.0,
  "worker_still_running_at": 1777875905.0,
  "error": "LLM worker still running after 300s + 5s grace; ChatInterface is unsafe for AED retry"
}
```

If the file does not exist because the 120s timer never fired (e.g. config changes threshold above `retry_timeout`), the `WorkerStillRunningError` handler should create it.

---

## Soul consultation contributing factor

### Verification

After commit `1acd183`, soul-flow timer ownership moved to `intrinsics/soul/flow.py`.

Current behavior:

- `BaseAgent._set_state()` imports `_start_soul_timer` / `_cancel_soul_timer` from `intrinsics.soul.flow`.
- `_set_state()` defines fire-eligible states as `{ACTIVE, IDLE}`.
- Transition from `ACTIVE`/`IDLE` to `STUCK`/`ASLEEP`/`SUSPENDED` calls `_cancel_soul_timer()`.
- `_cancel_soul_timer()` cancels `agent._soul_timer` if present and sets it to `None`.
- `_soul_whisper()` defensively checks `agent._state in (ACTIVE, IDLE)` before running consultation.

So: once `_on_llm_hang()` calls `_set_state(STUCK)` at 120s, any **pending** soul timer should be cancelled, and a timer callback that races after cancellation should skip if it sees `STUCK`.

### Does that cover the observed 23:38:50 consultation?

No, not fully.

The observed consultation fired at 23:38:50. The hung LLM call began at 23:36:02. The 120s `.llm_hang` watchdog would fire around 23:38:02 in the patched code. If that code had been active at the time, `_set_state(STUCK)` should have cancelled the pending soul timer before 23:38:50.

However, there is still a narrow race window:

1. `_soul_whisper()` fires while the agent is still `ACTIVE` because the LLM call has not yet crossed the 120s watchdog threshold.
2. `_soul_whisper()` starts `_run_consultation_fire()`.
3. Later, `_on_llm_hang()` sets `STUCK` and cancels only the **timer object**.
4. The already-running consultation worker is not cancelled by `_cancel_soul_timer()`.

That is a smaller race than the observed one but still possible. Timer cancellation only cancels future callbacks; it cannot stop a consultation already in progress.

### Proposed Tier 1 soul fix

Keep it small:

1. Before enqueuing/persisting a completed consultation result, re-check state. If the agent is no longer `ACTIVE`/`IDLE`, discard or log `consultation_discarded_state` instead of injecting a TC wake.
2. Add the same state check immediately after expensive consultation returns, before `_persist_soul_entry()` / `_tc_inbox.enqueue()`.

Pseudo-shape in `intrinsics/soul/flow.py`:

```python
def _run_consultation_fire(agent) -> None:
    from ...state import AgentState

    if agent._state not in (AgentState.ACTIVE, AgentState.IDLE):
        agent._log("consultation_skipped_state", state=agent._state.value)
        return

    result = _consult_past_self(...)

    if agent._state not in (AgentState.ACTIVE, AgentState.IDLE):
        agent._log("consultation_discarded_state", state=agent._state.value)
        return

    # persist + enqueue TC wake as before
```

This does not cancel the provider work already spent by the soul consultation, but it prevents a late soul result from injecting synthetic tool calls into an interface that has since become unsafe/STUCK.

That is enough for Tier 1. A deeper design could share a cancellation token with consultation workers, but that is not needed to block this race.

---

## Test plan

### 1. Unit-test `_wait_for_worker_settle()` raises distinct exception

Add to `tests/test_llm_utils.py`:

```python
from concurrent.futures import Future
import pytest
from lingtai_kernel.llm_utils import WorkerStillRunningError, _wait_for_worker_settle


def test_wait_for_worker_settle_raises_when_future_still_running(monkeypatch):
    # Avoid a real 5s grace wait.
    monkeypatch.setattr("lingtai_kernel.llm_utils._WORKER_SETTLE_GRACE", 0.01)
    future = Future()  # never completed

    with pytest.raises(WorkerStillRunningError) as exc:
        _wait_for_worker_settle(future, elapsed=300.0, agent_name="test")

    assert exc.value.elapsed == 300.0
    assert exc.value.grace == 0.01
```

This is the most direct simulation: a `Future` with no result exactly represents "provider worker still alive after grace." No real thread needs to be stuck.

### 2. Unit-test `send_with_timeout()` propagates `WorkerStillRunningError`

Current `test_send_with_timeout_waits_for_worker_to_settle_after_timeout()` verifies the settled-worker path. Add the complement:

```python
def test_send_with_timeout_raises_worker_still_running_when_worker_never_settles(monkeypatch):
    import threading
    import pytest
    from lingtai_kernel.llm_utils import WorkerStillRunningError

    monkeypatch.setattr("lingtai_kernel.llm_utils._WORKER_SETTLE_GRACE", 0.01)
    pool = ThreadPoolExecutor(max_workers=1)
    blocker = threading.Event()
    chat = _FakeChat(blocker, result=FakeLLMResponse())  # send blocks until event, never set

    try:
        with pytest.raises(WorkerStillRunningError):
            send_with_timeout(
                chat=chat,
                message="hi",
                timeout_pool=pool,
                retry_timeout=0.01,
                agent_name="test",
                logger=None,
            )
    finally:
        blocker.set()
        pool.shutdown(wait=True)
```

Important cleanup: set the blocker in `finally` so the test suite does not leak a worker thread.

### 3. Run-loop test: AED does not close/retry on `WorkerStillRunningError`

Use a lightweight fake agent/session rather than a real provider:

- fake `_session.send()` raises `WorkerStillRunningError`;
- fake chat interface records calls to `close_pending_tool_calls()`;
- enqueue one `MSG_REQUEST`;
- run one loop iteration or directly exercise the extracted AED handler if available;
- assert:
  - `close_pending_tool_calls()` was **not** called;
  - `_session._rebuild_session()` was **not** called;
  - no recovery message was injected;
  - state was set to `STUCK` and/or `_asleep` set according to the chosen fail-closed branch;
  - event log contains `llm_worker_still_running` / `aed_unsafe_retry_blocked`.

If the existing run loop is hard to unit-test, introduce a small helper such as `_handle_worker_still_running(agent, err)` in `base_agent/turn.py` and unit-test that helper directly. Then the integration risk is just one `except WorkerStillRunningError as e:` branch.

### 4. Watchdog signal test

Patch `agent._session.send` to raise `WorkerStillRunningError` while `.llm_hang` already exists. Assert `_send_with_watchdog()` does not unlink the file for this exception.

Also test ordinary exception cleanup:

- `.llm_hang` exists;
- `_session.send` raises `TimeoutError("ordinary")`;
- `_send_with_watchdog()` removes `.llm_hang`.

This locks in the "do not fight with `.llm_hang`" flow.

### 5. Soul-flow late-result test

For `intrinsics/soul/flow.py`:

- set fake agent state to `ACTIVE`;
- mock consultation call to change agent state to `STUCK` before returning a result;
- assert `_run_consultation_fire()` logs `consultation_discarded_state` and does not enqueue into `_tc_inbox`.

This verifies the late-result guard, not just timer cancellation.

---

## Tier 2 sketch: transactional interface ownership

Tier 1 only prevents AED from retrying when it knows ownership is unsafe. Tier 2 should remove the underlying shared-mutation hazard.

The structural fix is to make provider sends transactional with respect to `ChatInterface`:

1. Build a candidate interface for the outbound provider call.
2. Let the adapter mutate that candidate interface, not the canonical `agent._chat.interface`.
3. If the provider call succeeds, commit the user + assistant mutations back to the canonical interface under a lock.
4. If the provider call fails or times out, discard the candidate; AED sees a canonical interface that was never partially owned by the provider worker.

The nuance: adapters currently hold and mutate an interface reference directly (`self._interface.add_user_message(...)`, `self._interface.add_assistant_message(...)`). Tier 2 must account for that. Either:

- create a new adapter/session whose `self._interface` is the cloned candidate for each send, then atomically swap/merge on success; or
- make `ChatInterface` itself copy-on-write / transactional, so adapter calls write into an isolated transaction object until commit.

A plain `copy.deepcopy(interface)` outside the adapter is not sufficient if the existing adapter still holds `self._interface` pointing at the canonical object. The adapter must be handed the clone, or the interface object must redirect writes into a transaction.

This should be a separate design because it touches adapter construction, persistence semantics, and tool-call pairing invariants across all providers.

---

## Proposed implementation checklist for Tier 1

1. `llm_utils.py`
   - Add `WorkerStillRunningError`.
   - Change `_wait_for_worker_settle()` to raise it when the future is still running after grace.
   - Update docstrings/comments that currently say "log loudly and proceed".

2. `base_agent/turn.py`
   - Import/special-case `WorkerStillRunningError` in the AED loop before the generic `except Exception` repair path, or branch inside the generic except before `close_pending_tool_calls()`.
   - Do not call `close_pending_tool_calls()` for this exception.
   - Do not rebuild session or inject AED recovery message.
   - Log a structured event and fail closed.
   - Update `_send_with_watchdog()` so `.llm_hang` survives `WorkerStillRunningError`.

3. `intrinsics/soul/flow.py`
   - Add a post-consultation state guard before persisting/enqueuing soul-flow results.

4. Tests
   - Add the five tests above.
   - Run at minimum:
     - `pytest tests/test_llm_utils.py`
     - relevant turn/watchdog tests (new or existing)
     - relevant soul-flow tests
     - full suite if time allows.

## Open question for human review

For the fail-closed branch, should Tier 1:

1. set `STUCK` and then immediately set `_asleep` / `ASLEEP`, requiring mail or refresh to wake; or
2. set `STUCK` only and add a run-loop guard that refuses to process new inbox messages while `STUCK` until heartbeat moves it to `ASLEEP`?

I recommend option 1 for smaller code and clearer behavior, but I want human confirmation because it makes `WorkerStillRunningError` more terminal than ordinary AED timeout.
