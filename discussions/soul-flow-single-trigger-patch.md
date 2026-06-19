# Soul flow: single wall-clock trigger + IDLE/ACTIVE-only firing

## Problem

`_run_consultation_fire` has **two independent triggers** — both active by default:

1. **Wall-clock cadence** (`_start_soul_timer` → `_soul_whisper`, every `soul_delay = 300s`, perpetual since boot).
2. **Turn-count cadence** (`_post_llm_call` → `_maybe_fire_consultation`, every `consultation_interval = 20` main-chat LLM calls).

The doc-comment at `base_agent.py:1019-1021` even states explicitly: *"0 disables the turn-count trigger; the wall-clock timer keeps running independently."* So with default config, fires happen on whichever clock arrives first — and the other one keeps ticking on its own schedule.

The `tc_inbox` `coalesce=True` only collapses the visible spliced pair; the expensive M=1+K LLM fan-out **still runs per fire** (`base_agent.py:1034-1036`). So a busy agent ends up paying double cost.

Conceptual problem: soul flow is "subconscious" by design (per `project_soul_philosophy.md`). Coupling it to LLM-call frequency means a busy agent gets *more* reflection than a resting one, which inverts the intended semantics. Wall-clock is the right anchor.

## Goal

- One trigger: the wall-clock timer.
- Fire only when the agent is **`ACTIVE` or `IDLE`**.
- On entry to a no-fire state (`STUCK`, `ASLEEP`, `SUSPENDED`): **cancel the timer outright** — no pause, no resume-with-stored-remainder.
- On entry back to `ACTIVE` / `IDLE`: **start a fresh `soul_delay`-second timer**. Recovery time anchors the next fire; agents don't get a "flurry of stale fires" after a long stuck/asleep period.
- Result still queued onto `tc_inbox` (no behavior change downstream — the drain side already handles the splice on the next safe boundary).
- Remove the entire turn-count cadence: config field, schema entry, init.json reader, agent-callable `config` action handling, i18n strings.

## Design notes

- **Single chokepoint: `_set_state`.** Every state transition funnels through `_set_state` (line 815) — AED entry to `STUCK` (line 1536), sleep-signal entry to `ASLEEP` (line 1334), wake-from-asleep back to `ACTIVE` (line 1497), suspend (line 1322), refresh (line 1310). Driving the timer from `_set_state` handles every entry/exit "for free" without needing per-call-site bookkeeping.
- **Cancel-and-restart, not pause-and-resume.** Pausing would require capturing remaining time, restoring it on resume, and a "was-running-before" flag. Cancel-and-restart is simpler, anchors recovery to a clean cadence, and avoids the footgun where a 10-minute-stuck agent resumes with two stale fires already overdue.
- **Defensive state check still in `_soul_whisper`.** Tiny race window: timer thread fires concurrently with `_set_state(STUCK)` on the run-loop thread. The state allowlist (`ACTIVE`/`IDLE`) catches that race so a barely-fired timer doesn't splice into a state that just left `IDLE`.
- **`count_main_api_calls` in `token_ledger.py` after the patch.** It has no other caller. Recommend keep + update the docstring (useful diagnostic helper, ~10 lines, no maintenance burden). If you'd rather delete, flag it.

---

## Files to change

### 1. `src/lingtai_kernel/base_agent.py`

#### 1a. Drive the timer from `_set_state` — cancel on no-fire entry, restart on fire-eligible entry

The timer should only run when the agent is in a fire-eligible state (`ACTIVE` / `IDLE`). Every other state transition cancels it; every transition back to a fire-eligible state starts a fresh `soul_delay`-second timer.

`_set_state` is the single chokepoint for all transitions, so the logic lives there.

**Current `_set_state` (line 815):**
```python
def _set_state(self, new_state: AgentState, reason: str = "") -> None:
    """Transition to a new state.

    State no longer drives the soul cadence timer — the timer runs
    perpetually on a wall clock (started at boot, cancelled at
    shutdown / sleep). Only the idle event flag is updated here.
    """
    old = self._state
    if old == new_state:
        return
    self._state = new_state
    if new_state == AgentState.ACTIVE:
        self._idle.clear()
    else:
        self._idle.set()
    self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
    self._workdir.write_manifest(self._build_manifest())
```

**New `_set_state`:**
```python
def _set_state(self, new_state: AgentState, reason: str = "") -> None:
    """Transition to a new state.

    Drives the soul cadence timer: the timer runs only when the agent
    is in a fire-eligible state (ACTIVE / IDLE). Entering STUCK,
    ASLEEP, or SUSPENDED cancels it outright; returning to ACTIVE or
    IDLE starts a fresh ``soul_delay``-second timer. No pause/resume
    semantics — recovery time anchors the next fire.
    """
    old = self._state
    if old == new_state:
        return
    self._state = new_state
    if new_state == AgentState.ACTIVE:
        self._idle.clear()
    else:
        self._idle.set()

    fire_eligible = {AgentState.ACTIVE, AgentState.IDLE}
    was_eligible = old in fire_eligible
    is_eligible = new_state in fire_eligible
    if was_eligible and not is_eligible:
        # Leaving fire-eligible — cancel any pending timer.
        self._cancel_soul_timer()
    elif is_eligible and not was_eligible:
        # Returning to fire-eligible — start a fresh timer.
        self._start_soul_timer()

    self._log("agent_state", old=old.value, new=new_state.value, reason=reason)
    self._workdir.write_manifest(self._build_manifest())
```

#### 1b. Update `_soul_whisper` — defensive state check, no skip-and-reschedule

The timer should only be running when fire-eligible (per 1a), but a tiny race exists between the timer firing and `_set_state(STUCK)` flipping state on the run-loop thread. Keep a defensive check.

**Current (line 863):**
```python
def _soul_whisper(self) -> None:
    """Cadence timer callback. Fires past-self consultation on the
    soul_delay wall clock, then reschedules itself.
    ...
    """
    self._soul_timer = None
    try:
        if self._state in (AgentState.ASLEEP, AgentState.SUSPENDED):
            self._log("soul_whisper_skipped", reason=self._state.value)
        else:
            self._run_consultation_fire()
    except Exception as e:
        self._log("soul_whisper_error", error=str(e))
    finally:
        # Perpetual cadence — reschedule unless shutting down
        self._start_soul_timer()
```

**New:**
```python
def _soul_whisper(self) -> None:
    """Cadence timer callback. Fires past-self consultation on the
    soul_delay wall clock, then reschedules itself.

    Only fires under ACTIVE or IDLE. The timer is normally cancelled
    on entry to STUCK/ASLEEP/SUSPENDED via _set_state, so the state
    check here is defensive — it catches the narrow race between the
    timer firing on its own thread and a concurrent state transition
    on the run-loop thread.

    Reschedules itself only when fire-eligible. Recovery from STUCK
    or wake-from-ASLEEP starts a fresh timer via _set_state, so the
    finally clause does not need to reschedule unconditionally.
    """
    self._soul_timer = None
    try:
        if self._state in (AgentState.ACTIVE, AgentState.IDLE):
            self._run_consultation_fire()
        else:
            self._log("soul_whisper_skipped", reason=self._state.value)
    except Exception as e:
        self._log("soul_whisper_error", error=str(e))
    finally:
        if self._state in (AgentState.ACTIVE, AgentState.IDLE):
            self._start_soul_timer()
```

#### 1c. Update `_start_soul_timer` docstring (line 833)

The "runs perpetually" comment is now stale.

**Current:**
```python
def _start_soul_timer(self) -> None:
    """Start the soul cadence timer.

    Runs perpetually — fires every ``_soul_delay`` seconds regardless of
    agent state, reschedules itself in the timer callback. Stops only on
    shutdown or when explicitly cancelled (e.g. when entering ASLEEP).

    The cadence model means soul flow surfaces on a wall clock, not when
    the agent goes idle. Busy agents still get reflection; rest is no
    longer a precondition for the inner voice.
    """
```

**New:**
```python
def _start_soul_timer(self) -> None:
    """Start the soul cadence timer.

    Runs only while the agent is fire-eligible (ACTIVE or IDLE).
    Cancelled by _set_state on entry to STUCK / ASLEEP / SUSPENDED;
    restarted by _set_state on return to ACTIVE / IDLE. Reschedules
    itself in the timer callback (also gated on fire-eligibility).

    The cadence model means soul flow surfaces on a wall clock, not
    when the agent goes idle. Busy agents still get reflection; rest
    is not a precondition for the inner voice. But agents in failure
    or hibernation get no cadence — recovery anchors a fresh interval.
    """
```

#### 1d. Boot — start the timer only if booting into a fire-eligible state

`run()` at line 618 currently calls `_start_soul_timer()` unconditionally. After the patch, the agent boots into `IDLE` (line 392) which is fire-eligible, so the unconditional start is still correct. **No change needed**, but worth a one-line comment so the symmetry with `_set_state` is visible.

**Current (line 617–618):**
```python
self._start_heartbeat()
# Soul cadence runs perpetually from boot — independent of state.
self._start_soul_timer()
```

**New:**
```python
self._start_heartbeat()
# Boot state is IDLE (fire-eligible) — start the timer here. Subsequent
# state transitions are managed by _set_state.
self._start_soul_timer()
```

#### 1e. Delete `_post_llm_call` and `_maybe_fire_consultation` (lines 1001–1050)

Delete the whole block — both methods, plus their docstrings.

#### 1f. Remove the two call sites

**Line 1834 (in the text-input handler):** delete the line `self._post_llm_call()`.

**Lines 2078–2079 (in the tool-loop continuation):**

Current:
```python
response = self._session.send(tool_results)
self._last_usage = response.usage
if ledger_source == "main":
    self._post_llm_call()
self._save_chat_history(ledger_source=ledger_source)
```

New:
```python
response = self._session.send(tool_results)
self._last_usage = response.usage
self._save_chat_history(ledger_source=ledger_source)
```

(The `if ledger_source == "main":` gate goes away with the call.)

#### 1g. Drop the now-unused import (if present)

If `count_main_api_calls` is imported at module top in `base_agent.py`, remove the import. Quick check:

```bash
grep -n "count_main_api_calls" src/lingtai_kernel/base_agent.py
```

Verified: only used inside `_maybe_fire_consultation`. If imported at module level, drop it. If imported inside the function, no action needed (function is being deleted).

---

### 2. `src/lingtai_kernel/config.py`

#### Line 37 — remove `consultation_interval`:

**Current:**
```python
consultation_interval: int = 20  # main-chat LLM calls between past-self consultation fires (counts source="main" ledger entries); 0 = off (wall-clock timer still runs independently)
consultation_past_count: int = 2  # K random past-snapshot consultations per fire (M = 1 insights + K)
```

**New:**
```python
consultation_past_count: int = 2  # K random past-snapshot consultations per fire (M = 1 insights + K)
```

---

### 3. `src/lingtai/agent.py`

#### Line 820 — remove the init.json reader:

**Current:**
```python
soul_delay=soul.get("delay", 300.0),
consultation_interval=soul.get("consultation_interval", 20),
```

**New:**
```python
soul_delay=soul.get("delay", 300.0),
```

(Existing init.json files that still have `manifest.soul.consultation_interval` will be silently ignored — `soul.get` doesn't error on extra keys. No migration needed.)

---

### 4. `src/lingtai/init_schema.py`

#### Line 185 — remove the schema entry:

```python
"consultation_interval": int,
```

Delete this line. Existing init.json files with the field will pass validation (extra keys are typically permitted; if your schema is strict, this needs a tolerant pass on the soul block — flag if so and I'll write the migration).

---

### 5. `src/lingtai_kernel/intrinsics/soul.py`

#### 5a. Drop `CONSULTATION_INTERVAL_MIN` constant (line 27–29)

```python
# Lower bound on turn-counter cadence — below this, every few turns triggers
# a fire and consultation cost dominates work. 0 disables the turn counter.
CONSULTATION_INTERVAL_MIN = 5
```

Delete.

#### 5b. Remove `consultation_interval` from the schema (lines 69–73)

```python
"consultation_interval": {
    "type": "integer",
    "minimum": 0,
    "description": t(lang, "soul.consultation_interval_description"),
},
```

Delete this whole property block from `get_schema`.

#### 5c. Remove the field from `_handle_config` (lines 153–154, 188–206)

Delete:
- Lines 153–154 (the `provided` collection):
  ```python
  if "consultation_interval" in args:
      provided["consultation_interval"] = args["consultation_interval"]
  ```
- Line 161 mention in the error message — change `"delay_seconds, consultation_interval, consultation_past_count."` → `"delay_seconds, consultation_past_count."`
- Lines 188–206 (the entire validation + apply block for the field).

#### 5d. Remove from `_persist_soul_config` (lines 364, 396–397)

- Line 364 — remove from docstring map.
- Lines 396–397:
  ```python
  if "consultation_interval" in new_values:
      soul_block["consultation_interval"] = new_values["consultation_interval"]
  ```
  Delete.

#### 5e. Update top-of-file docstring (lines 15–19)

Currently lists `consultation_interval` as a `config` field. Update to reflect the two remaining knobs (`delay_seconds`, `consultation_past_count`).

---

### 6. i18n — three locales

#### 6a. `src/lingtai_kernel/i18n/en.json`

- **Line 14** (`soul.description`): remove the parenthetical *"(and/or every consultation_interval main-chat turns; both triggers run independently)"* and the *"consultation_interval (turn-counter cadence, 0=off or >=5),"* item from the config-knobs list.
- **Line 15** (`soul.action_description`): remove *"(or every consultation_interval main-chat turns)"* and the *"consultation_interval (turn cadence, 0 or >=5),"* item.
- **Line 18** (`soul.consultation_interval_description`): delete the entire key/value pair.

#### 6b. `src/lingtai_kernel/i18n/zh.json`

Same three edits as en.json — remove all `consultation_interval` mentions in `soul.description` and `soul.action_description`, delete the `soul.consultation_interval_description` key.

#### 6c. `src/lingtai_kernel/i18n/wen.json`

Same three edits.

> Note: per `feedback_no_i18n_procedures.md`, you've previously said procedure changes don't need zh/wen translation. But these are user-facing tool descriptions the LLM reads as system context, not procedure docs — they affect agent behavior in non-English locales. Recommend doing all three. If you'd rather only edit `en.json`, the zh/wen agent prompts will keep mentioning `consultation_interval`, which is technically wrong but won't break anything (the field just becomes a no-op).

---

### 7. (Optional) `src/lingtai_kernel/token_ledger.py` — `count_main_api_calls`

After this patch, the function has no caller. Two options:

- **Keep it** — useful diagnostic helper, ~10 lines, no maintenance burden, future code may want it. Update the docstring (line 65) which currently mentions `consultation_interval` as the use case.
- **Delete it** — remove the function entirely.

Flag your preference. Recommend keep + update docstring.

---

## Verification checklist

After applying:

```bash
# 1. Smoke-test imports — catches any dangling references
~/.lingtai-tui/runtime/venv/bin/python -c "import lingtai_kernel.base_agent; import lingtai_kernel.config; import lingtai_kernel.intrinsics.soul; import lingtai; print('ok')"

# 2. Confirm no lingering references in source
cd ~/Documents/GitHub/lingtai-kernel
grep -rn "consultation_interval\|_post_llm_call\|_maybe_fire_consultation" src/

# Expected output: empty (or only matches in tests/ — see step 3).

# 3. Update tests
grep -rn "consultation_interval\|_maybe_fire_consultation\|_post_llm_call" tests/

# Any tests that exercise the turn-count path will need to be updated or
# deleted. The wall-clock path tests should keep working unchanged.

# 4. Run the kernel test suite
cd ~/Documents/GitHub/lingtai-kernel && pytest -n auto

# 5. Boot an agent, confirm:
#    - logs/events.jsonl shows agent_state transitions and the timer
#      lifecycle is consistent (timer cancelled on transitions out of
#      ACTIVE/IDLE, restarted on transitions back in).
#    - consultation_fire only fires on the wall-clock interval — no
#      turn-count fires regardless of how busy the agent is.
#    - Force a STUCK transition (e.g. let an LLM call hang past the
#      retry_timeout) and confirm: no consultation_fire events occur
#      while STUCK; on return to ACTIVE, the next fire happens
#      ~soul_delay seconds later (not immediately, and not "stale").
#    - Sleep then wake an agent: same — no fire-on-wake, fresh interval.
#    - logs/soul_flow.jsonl receives one fire record per cadence tick
#      (only when ACTIVE/IDLE).
```

## Behavior changes (user-visible)

- Existing init.json files with `manifest.soul.consultation_interval = N` will silently ignore the field after the patch. No migration needed; the value just becomes inert.
- Agents currently calling `soul(action='config', consultation_interval=...)` will get a schema error ("unknown field"). This is a breaking change for any agent skill that has learned to set this field. Acceptable — the field's documented purpose (a second cadence) is going away.
- Default cadence behavior changes: instead of "every 20 main turns OR every 300s, whichever first", it becomes "every 300s when ACTIVE/IDLE." If you find 300s feels too sparse for your workflows, consider lowering `soul_delay` default in a separate change — out of scope for this patch.

## Lines of change estimate

- `base_agent.py`: ~50 lines net deletion. `_post_llm_call` + `_maybe_fire_consultation` (~50 lines) + 2 call sites (3 lines) come out. `_set_state` gains ~10 lines (timer-driving block). `_soul_whisper` and `_start_soul_timer` get docstring updates.
- `config.py`: 1 line removed.
- `agent.py`: 1 line removed.
- `init_schema.py`: 1 line removed.
- `intrinsics/soul.py`: ~30 lines removed (constant, schema field, validator block, persist branch, docstring touch-ups).
- 3× i18n files: ~3 keys + 2 inline mentions per locale.
- Total: ~85 lines net deletion. ~10 lines new code in `_set_state` for the cancel-and-restart logic.
