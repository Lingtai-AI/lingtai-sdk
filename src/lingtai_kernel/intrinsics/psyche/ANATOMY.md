---
related_files:
  - src/lingtai/intrinsic_skills/psyche-manual/assets/session-journal-entry-template.md
  - src/lingtai_kernel/intrinsics/ANATOMY.md
  - src/lingtai_kernel/intrinsics/psyche/__init__.py
  - src/lingtai_kernel/intrinsics/psyche/_lingtai.py
  - src/lingtai_kernel/intrinsics/psyche/_molt.py
  - src/lingtai_kernel/intrinsics/psyche/_pad.py
  - src/lingtai_kernel/intrinsics/psyche/_session_journal.py
  - src/lingtai_kernel/intrinsics/psyche/_snapshots.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# intrinsics/psyche

Agent identity, working notes, and context lifecycle — the "bare essentials of self." Provides the agent with tools to manage its own identity, working notes (pad), and conversation context (molt). The core shed-and-reload machinery that enables cross-session persistence.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — Package surface. Re-exports all public API for backward compatibility. Contains:
  - `get_description` / `get_schema` (`__init__.py:41-84`) — tool registration; schema intentionally avoids top-level JSON Schema combinators for strict-mode provider compatibility.
  - `_VALID_ACTIONS` / `_DISPATCH` (`__init__.py:91-105`) — action→handler dispatch table replacing the former `globals().get()` pattern; preserves per-object action validation that is not encoded with schema combinators.
  - `handle()` (`__init__.py:108-128`) — main dispatcher.
  - `boot()` (`__init__.py:136-145`) — boot-time hook: loads lingtai + pad, registers post-molt reload callback.

- `_session_journal.py` — Session-journal gate for agent-initiated molt (issue #350).
  - `validate_session_journal_path()` — pure, fail-closed validator. Given the agent workdir and the agent-supplied `session_journal_path`, returns `(ok, error, resolved_relpath)`. Checks, in order: path present/non-empty → resolves INSIDE the workdir (no traversal/absolute escape) → lives at the canonical `knowledge/session-journal/<entry>/KNOWLEDGE.md` sub-entry (rejects the parent index `knowledge/session-journal/KNOWLEDGE.md` and scratch dirs / non-`KNOWLEDGE.md` filenames) → exists, non-empty, UTF-8 → valid YAML frontmatter with `name` + `description` → carries a session-journal marker (`type: session-journal` or `session_journal: true`). Every failure returns an actionable recovery message; on success the resolved path is normalized to a workdir-relative posix path for recording in molt metadata.

- `_snapshots.py` — Snapshot and summary persistence for the molt machinery.
  - `SNAPSHOT_SCHEMA_VERSION` (`_snapshots.py:16`) — schema version tag for snapshots.
  - `_write_molt_summary()` (`_snapshots.py`) — persist agent/system-authored molt summary to `system/summaries/` as YAML-frontmatter markdown. Accepts optional `session_journal_path`; when present it is recorded as a `session_journal_path:` frontmatter line so audits can pair a molt with the journal that backed it.
  - `_write_molt_snapshot()` (`_snapshots.py`) — serialize pre-molt `ChatInterface` to `history/snapshots/` as a discrete JSON file for past-self consultation.

- `_pad.py` — Pad (working notes) management.
  - Append-file management: `_APPEND_LIST_PATH` / `_APPEND_TOKEN_LIMIT` constants (`_pad.py:17-18`), `_append_list_file` (`_pad.py:21`), `_load_append_list` (`_pad.py:24-33`), `_save_append_list` (`_pad.py:36-39`), `_resolve_path` (`_pad.py:42-44`), `_read_append_content` (`_pad.py:47-55`), `_is_text_file` (`_pad.py:58-69`).
  - `_pad_edit()` (`_pad.py:76-111`) — write content + optional file imports to `system/pad.md`.
  - `_pad_load()` (`_pad.py:114-155`) — load `system/pad.md` + pinned append-files into the prompt.
  - `_pad_append()` (`_pad.py:158-198`) — set/clear/query the list of files pinned as read-only pad reference.

- `_lingtai.py` — Lingtai (identity/character) management.
  - `_lingtai_update()` (`_lingtai.py:11-22`) — write content to `system/lingtai.md` then auto-load.
  - `_lingtai_load()` (`_lingtai.py:25-54`) — the single canonical writer of the protected `character` prompt section, composed from `system/lingtai.md` alone. Empty/missing file deletes the section. Distinct from `covenant` (operator contract, owned by `Agent._reload_prompt_sections`) and from the mechanical `identity` section (written by BaseAgent).

- `_molt.py` — Context molt core, name handlers, and system-initiated molt.
  - `_context_molt()` (`_molt.py`) — agent-initiated molt: validates summary, **session journal (issue #350)**, & keep_tool_calls, snapshots, archives history, wipes wire session, replays the molt's own ToolCallBlock + kept pairs into the fresh interface. The core of the psyche. The session-journal gate runs FIRST (after the summary check, before any state mutation) and is **unconditional**: every molt that reaches this function is dispatched from a model-issued psyche tool call, so it always calls `validate_session_journal_path` on `args["session_journal_path"]` and refuses the molt with the validator's recovery message if invalid — no snapshot, no archive, no `molt_count` increment. The gate does **not** consult `args["_initiator"]`: that is a model-provided arg, not trusted kernel state, so a model passing `_initiator: "system"` cannot bypass the gate (issue #350). True system-forced molts never re-enter this function — they go through `context_forget`. The accepted relative path is threaded into `_write_molt_summary` (frontmatter), `_publish_post_molt` (notification `data`), and the result dict (`session_journal_path`). Resets wire-level tracking (`_notification_block_id` plus any legacy `_pending_notification_*` attributes) and `_notification_fp` but preserves `.notification/` files — notifications are system state, not conversation memory. Still calls `agent._tc_inbox.drain()` defensively for pre-redesign items that survived a restart. After the wipe completes, calls `_publish_post_molt` to drop a `.notification/post-molt.json` reminder so the fresh agent reads what it was doing and how to dismiss.
  - `_name_set()` (`_molt.py`) — set the agent's true (immutable) name.
  - `_name_nickname()` (`_molt.py`) — set or clear the agent's mutable nickname.
  - `context_forget()` (`_molt.py`) — system-initiated forced molt: synthesizes a complete ToolCallBlock+ToolResultBlock pair, wipes context, replays them into the fresh interface, and persists the system-authored summary. Called by `base_agent/lifecycle.py` when an external `.clear` signal arrives; tests also exercise `source="warning_ladder"` and `source="aed"` directly. Same notification tracking reset as `_context_molt` (wire-level only; `.notification/` files survive). Also publishes a `post-molt` notification (`initiator: "system"`, `source` propagated) so the fresh agent has the same recall hook agent-initiated molts get.
  - `_publish_post_molt()` (`_molt.py`) — internal helper. Writes `.notification/post-molt.json` via `publish_notification` (channel `post-molt`, icon 🌱, priority high). Payload carries `molt_id`, `molt_at`, `source_agent`, `initiator`, `source`, `molt_count`, `reminder`, `ack_options` (`["continue", "defer", "obsolete"]`), `reasoning` (agent molts only), `summary_path`, `session_journal_path` (agent molts; the validated journal path, else null), and `tokens_before/after`. The kernel does **not** excerpt or parse the summary — there is no `next_action` field (PR #190 removed the heuristic); the agent reconstructs context itself. Instructions tell the agent to reconstruct from pad / `summary_path` (latest summary) / recent human-channel messages, decide (never auto-execute stored text), then explicitly choose continue/defer/obsolete and dismiss via `notification(action='dismiss_channel', channel='post-molt', reason='<choice>: ...')` (a reason is required). The channel is intentionally separate from the legacy pressure `molt` channel; context-pressure reminders are no longer published as dismissible notifications — they live under `_meta.agent_meta.context.molt` via `meta_block.build_molt_context`. `base_agent.turn._check_molt_pressure` now only clears any stale legacy `molt` notification, so it can never sweep this reminder.

## Connections

- **Inbound:** `handle()` is called by the tool dispatcher (via `base_agent._dispatch_tool`). `boot()` is called during agent construction in `base_agent/__init__.py:398`.
- **Inbound (cross-module):** `context_forget` is called by `base_agent/lifecycle.py:401-414` for the `.clear` signal.
- **Inbound (cross-module):** `_write_molt_snapshot` is imported by `intrinsics/soul/consultation.py` for snapshot loading via `_load_snapshot_interface`.
- **Outbound:** Depends on `..i18n` (translations), `..llm.interface` (`ToolCallBlock`, `ToolResultBlock`), `..token_counter` (token budget checks in `_pad_append`).
- **Data flow:** All state lives in the filesystem under `system/` (`pad.md`, `lingtai.md`, `pad_append.json`, `summaries/`) and `history/` (`chat_history.jsonl`, `chat_history_archive.jsonl`, `snapshots/`). `_lingtai_load` reads `lingtai.md` alone (→ `character` section); `system/covenant.md` is owned by `Agent._reload_prompt_sections` (→ `covenant` section), not this package. The molt path preserves `.notification/` files and resets only the agent's in-memory notification-tracking attributes so the fresh session re-reads the files cleanly.

## Key invariants

- `_context_molt` is the only path that archives `chat_history.jsonl`, increments `molt_count`, and replays the molt's own ToolCallBlock. `context_forget` is the only path that synthesizes both the call and result entries.
- The `keep_tool_calls` list is validated BEFORE any state mutation — if any id is unmatched, the molt is refused and `molt_count` is not incremented.
- **Session-journal gate (issue #350):** every molt routed through `_context_molt` REQUIRES a valid `session_journal_path` — the gate is unconditional and does **not** consult `args["_initiator"]` (a model-provided arg is not trusted kernel state, so `_initiator: "system"` cannot bypass it). It is validated by `_session_journal.validate_session_journal_path` FIRST — after the summary check, before snapshot/archive/wipe/`molt_count` — so a missing or invalid journal fails closed with no partial shed. The only molt that skips the gate is the true system-forced path, `context_forget`, which synthesizes its own call/result pair end-to-end and never re-enters `_context_molt` (there is no agent turn to author a journal). The marker contract is fixed: `type: session-journal` or `session_journal: true` in the entry's YAML frontmatter; the canonical location is `knowledge/session-journal/<entry>/KNOWLEDGE.md` (sub-entry, not the parent index). The session-journal entry template (`src/lingtai/intrinsic_skills/psyche-manual/assets/session-journal-entry-template.md`) carries the `type: session-journal` marker to satisfy this gate.
- `context_forget` always adds `_initiator: "system"` to the ToolCallBlock args so the agent can distinguish system-initiated from agent-initiated molts.
- `boot()` registers a post-molt hook that reloads `character` (lingtai.md) + `pad` into the prompt via the canonical composers `_lingtai_load`/`_pad_load`. This hook runs BEFORE the fresh session is created during molt. On `Agent`, `_reload_prompt_sections` is *also* registered as a post-molt hook and routes through the same composers — so both hooks produce byte-identical `character`/`pad` content and the post-molt result is independent of hook order (the fix for the "lingtai folded into covenant, dropped after molt" race).
- `handle()` uses an explicit dispatch table (`_DISPATCH`) rather than `globals().get()`, so it works correctly across sub-modules.
- The post-molt notification channel name is the literal string `post-molt`. Context-pressure reminders moved out of the notification system to `_meta.agent_meta.context.molt` (`meta_block.build_molt_context`); `base_agent.turn._check_molt_pressure` now only clears any stale legacy `molt` notification and never publishes pressure, so it cannot sweep this reminder. Agents dismiss the reminder with `notification(action='dismiss_channel', channel='post-molt', reason='<continue|defer|obsolete>: ...')` after explicitly deciding how to handle the continuation.
