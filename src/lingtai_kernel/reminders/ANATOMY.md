---
related_files:
  - CLAUDE.md
  - src/lingtai_kernel/ANATOMY.md
  - src/lingtai_kernel/reminders/__init__.py
  - src/lingtai_kernel/reminders/context_pressure.py
  - src/lingtai_kernel/config.py
  - src/lingtai_kernel/session.py
  - src/lingtai_kernel/meta_block.py
  - src/lingtai_kernel/tool_executor.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# Reminders

Runtime reminder abstractions. Today this package holds exactly one:
`ContextPressureReminder`, the unified home for the molt / context-pressure
reminder. This is deliberately **one owned abstraction for one reminder**, not a
global reminder registry — that generalization is explicitly out of scope for
now (CLAUDE.md: "three similar lines beats a premature abstraction"; a registry
lands only when a second reminder proves the shape).

## What it owns

Before this package existed the reminder was split in two: `SessionManager` held
raw `_context_pressure_streak` / `_context_pressure_last_round_id` counters, and
`meta_block` held both the warn decision and the natural-language prose (in
`build_molt_context` and `build_reconstruction_tool_meta`). `ContextPressureReminder`
pulls the state machine + decisions + prose into one debuggable object:

- **Provider-round input** — `note_round(usage, *, round_id)` records one *fresh
  provider round*'s context usage. `round_id` (the `SessionManager._api_calls`
  counter) dedups multiple observations of the same round; usage semantics:
  `>= reconstruction_ratio` (0.75) advances the streak, `0 <= usage < ratio`
  resets it to 0 (relieved), `< 0` sentinel leaves it untouched.
- **Transient streak state** — `streak`, `last_round_id`, `last_usage`,
  `active` (derived: `streak >= warn_after_rounds`), and
  `last_transition_reason` (why the last observation moved the way it did:
  `initial` / `high_round` / `warning_active` / `relieved` / `duplicate_round` /
  `unknown_usage`). Not persisted — a fresh/restored session starts fresh.
- **Channel B — current-state reminder** — `current_molt_context(usage)` returns
  the natural-language string for `_meta.tool_meta.context.molt` (PERMANENT
  per-result metadata — moved off the sparse `agent_meta` so it persists on every
  result while the warning is active), or `None` unless `active`.
- **Channel A — reconstruction annotation** — `annotate_reconstruction(after_usage,
  *, recovery_target=None)` returns the `_meta.tool_meta.reconstruction.molt`
  string, or `None` when the rebuilt after-context is below the recovery target.
  It owns only the *warning decision + prose*; the event assembly (provider-vs-
  local after-context resolution, event shape, one-shot pop) stays in
  `meta_block.build_reconstruction_tool_meta`.
- **Emission descriptors** — `reminder_message_hash(text)` (short stable hex
  hash), `current_molt_emission_descriptor(reminder, *, usage, message)`, and
  `reconstruction_molt_emission_descriptor(event, *, message)` return a compact,
  JSON-safe `{event_name, payload}` used by the `_meta` assembly layer to log a
  structured runtime event when a reminder is *actually attached* to `tool_meta`.
  Event names: `context_pressure_current_molt_reminder_emitted` and
  `context_pressure_reconstruction_molt_reminder_emitted`. Payloads carry a
  `message_hash` (never the full prose), the thresholds, and the state/branch
  fields. The abstraction stays pure — it builds descriptors, it does not log.
- **Debug view** — `to_debug_dict()` (aliased `snapshot()`) returns a flat
  JSON-friendly dict with the thresholds that drove decisions, the streak/active
  state, the last usage/round, and the last transition reason — suitable for
  tests, logs, and debugging.

Thresholds default to the kernel-fixed `CONTEXT_PRESSURE_*` constants
(`config.py`) but are stored on the instance so the debug dict reports exactly
which values applied and tests can inject variants.

## File layout

- `context_pressure.py` — `ContextPressureReminder` plus the pure prose
  renderers `render_current_molt_context(...)` and `render_reconstruction_molt(...)`.
  The renderers are the single source of truth for the wording and are shared by
  the class methods and by `meta_block`'s compatibility fallback (see below).
- `__init__.py` — re-exports `ContextPressureReminder` and the two renderers.
- `ANATOMY.md` — this file.

## Connections

- `session.py` — `SessionManager.__init__` constructs one
  `ContextPressureReminder` (`self._context_pressure`). `_track_usage` calls
  `note_context_pressure_round(...)` per real provider round. The properties
  `context_pressure_streak` / `context_pressure_warning_active`, the method
  `note_context_pressure_round`, and the new `context_pressure_reminder`
  accessor are thin compatibility shims that delegate to the reminder; existing
  callers/tests that read the streak surface keep working unchanged.
- `meta_block.py` — `build_molt_context` keeps the psyche-intrinsic gate and
  session lookup, then delegates rendering: it prefers
  `session.context_pressure_reminder.current_molt_context(usage)` and falls back
  to `render_current_molt_context(streak, usage)` for lightweight session
  stand-ins that only expose the compat `context_pressure_*` attributes.
  `build_meta` (side-effect-free) routes the returned text into permanent
  `tool_meta.context.molt` via a transit key (`_tool_meta_context`) that
  `ToolExecutor._attach_tool_block` promotes, and always carries the
  emission-event payload (`_tool_meta_context_event`) while active.
  `build_reconstruction_tool_meta` keeps the event assembly (molt text stays on
  `tool_meta.reconstruction.molt`). Both emission events fire only on a real
  attach.
- `tool_executor.py` — `ToolExecutor._attach_tool_block` promotes the transit
  keys into permanent `tool_meta.context` and emits both reminder events via
  `self._log` (best-effort, never breaks a turn). The current-molt event is
  deduped to once per provider round using the per-turn executor's own
  `_last_current_molt_event_round` memory (one executor per turn), so the
  permanent per-result restamping does not flood the log; the reconstruction
  event is one-shot at source. Dedup lives here (at the real emission site), not
  in the render-path `build_meta`, which must stay pure.
- `config.py` — owns the kernel-fixed thresholds
  (`CONTEXT_PRESSURE_RECONSTRUCTION_RATIO` 0.75,
  `CONTEXT_PRESSURE_WARN_AFTER_ROUNDS` 3, `CONTEXT_PRESSURE_RECOVERY_TARGET`
  0.60) that this abstraction defaults from.

## Behavior invariants

- Warn only after sustained high provider rounds (3 consecutive `>= 0.75`); the
  old immediate `>= 0.60` trip-wire stays retired.
- The reconstruction event is one-shot permanent evidence (channel A) at
  `tool_meta.reconstruction`, distinct from the current-state reminder (channel B).
- Reminder prose and thresholds are unchanged. The one `_meta` contract change:
  the current sustained-pressure reminder moved from `agent_meta.context.molt`
  to PERMANENT `tool_meta.context.molt` (so it persists on every result while
  active); the reconstruction reminder stays at `tool_meta.reconstruction.molt`.
- Reminder-emission events are logged only when the reminder text is actually
  attached to the outgoing `tool_meta` (in `_attach_tool_block`), never on a bare
  render / condition check. The current-molt event is deduped to once per
  provider round so the permanent restamping does not flood the log.
