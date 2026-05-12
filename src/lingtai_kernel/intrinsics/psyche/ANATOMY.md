# intrinsics/psyche

Agent identity, working notes, and context lifecycle — the "bare essentials of self." Provides the agent with tools to manage its own identity, working notes (pad), and conversation context (molt). The core shed-and-reload machinery that enables cross-session persistence.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — Package surface. Re-exports all public API for backward compatibility. Contains:
  - `get_description` / `get_schema` (`__init__.py:41-93`) — tool registration.
  - `_VALID_ACTIONS` / `_DISPATCH` (`__init__.py:97-112`) — action→handler dispatch table replacing the former `globals().get()` pattern.
  - `handle()` (`__init__.py:114-140`) — main dispatcher.
  - `boot()` (`__init__.py:142-151`) — boot-time hook: loads lingtai + pad, registers post-molt reload callback.

- `_snapshots.py` — Snapshot and summary persistence for the molt machinery.
  - `SNAPSHOT_SCHEMA_VERSION` (`_snapshots.py:16`) — schema version tag for snapshots.
  - `_write_molt_summary()` (`_snapshots.py:19-80`) — persist agent/system-authored molt summary to `system/summaries/` as YAML-frontmatter markdown.
  - `_write_molt_snapshot()` (`_snapshots.py:83-148`) — serialize pre-molt `ChatInterface` to `history/snapshots/` as a discrete JSON file for past-self consultation.

- `_pad.py` — Pad (working notes) management.
  - Append-file management: `_APPEND_LIST_PATH` / `_APPEND_TOKEN_LIMIT` constants (`_pad.py:17-18`), `_append_list_file` (`_pad.py:21`), `_load_append_list` (`_pad.py:24-33`), `_save_append_list` (`_pad.py:36-39`), `_resolve_path` (`_pad.py:42-44`), `_read_append_content` (`_pad.py:47-55`), `_is_text_file` (`_pad.py:58-69`).
  - `_pad_edit()` (`_pad.py:76-111`) — write content + optional file imports to `system/pad.md`.
  - `_pad_load()` (`_pad.py:114-155`) — load `system/pad.md` + pinned append-files into the prompt.
  - `_pad_append()` (`_pad.py:158-198`) — set/clear/query the list of files pinned as read-only pad reference.

- `_lingtai.py` — Lingtai (identity/character) management.
  - `_lingtai_update()` (`_lingtai.py:8-20`) — write content to `system/lingtai.md` then auto-load.
  - `_lingtai_load()` (`_lingtai.py:23-54`) — merge `system/covenant.md` + `system/lingtai.md` and write to the protected `covenant` prompt section.

- `_molt.py` — Context molt core, name handlers, and system-initiated molt.
  - `_context_molt()` (`_molt.py:21-223`) — agent-initiated molt: validates summary & keep_tool_calls, snapshots, archives history, wipes wire session, replays the molt's own ToolCallBlock + kept pairs into the fresh interface. The core of the psyche. Resets wire-level tracking (`_notification_block_id` plus any legacy `_pending_notification_*` attributes) and `_notification_fp` but preserves `.notification/` files — notifications are system state, not conversation memory. Still calls `agent._tc_inbox.drain()` defensively for pre-redesign items that survived a restart.
  - `_name_set()` (`_molt.py:230-239`) — set the agent's true (immutable) name.
  - `_name_nickname()` (`_molt.py:242-245`) — set or clear the agent's mutable nickname.
  - `context_forget()` (`_molt.py:252-362`) — system-initiated forced molt: synthesizes a complete ToolCallBlock+ToolResultBlock pair, wipes context, replays them into the fresh interface. Called by `base_agent` when the warning ladder is exhausted or an external `.forget` signal arrives. Same notification tracking reset as `_context_molt` (wire-level only; `.notification/` files survive).

## Connections

- **Inbound:** `handle()` is called by the tool dispatcher (via `base_agent._dispatch_tool`). `boot()` is called during agent construction in `base_agent/__init__.py:398`.
- **Inbound (cross-module):** `context_forget` is called by `base_agent/lifecycle.py:235-236` (warning ladder), `base_agent/turn.py:341-342` (AED), and `base_agent/turn.py:353-354` (`.forget` signal).
- **Inbound (cross-module):** `_write_molt_snapshot` is imported by `intrinsics/soul/consultation.py` for snapshot loading via `_load_snapshot_interface`.
- **Outbound:** Depends on `..i18n` (translations), `..llm.interface` (`ToolCallBlock`, `ToolResultBlock`), `..token_counter` (token budget checks in `_pad_append`).
- **Data flow:** All state lives in the filesystem under `system/` (`pad.md`, `lingtai.md`, `covenant.md`, `pad_append.json`, `summaries/`) and `history/` (`chat_history.jsonl`, `chat_history_archive.jsonl`, `snapshots/`). The molt path also touches `.notification/` (deletes everything in it) and the agent's notification-tracking attributes.

## Key invariants

- `_context_molt` is the only path that archives `chat_history.jsonl`, increments `molt_count`, and replays the molt's own ToolCallBlock. `context_forget` is the only path that synthesizes both the call and result entries.
- The `keep_tool_calls` list is validated BEFORE any state mutation — if any id is unmatched, the molt is refused and `molt_count` is not incremented.
- `context_forget` always adds `_initiator: "system"` to the ToolCallBlock args so the agent can distinguish system-initiated from agent-initiated molts.
- `boot()` registers a post-molt hook that reloads lingtai + pad into the prompt. This hook runs BEFORE the fresh session is created during molt.
- `handle()` uses an explicit dispatch table (`_DISPATCH`) rather than `globals().get()`, so it works correctly across sub-modules.
