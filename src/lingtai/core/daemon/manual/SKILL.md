---
name: daemon-manual
description: >
  Operational router for the `daemon` tool: inspect slow/stuck/failed emanations,
  read daemon artifact folders, choose polling cadence, avoid reclaiming on a
  hunch, understand `daemon(action="list")`, use CLI backends and `backend_options`,
  and clean up daemon footprint. Read this after dispatching daemon work that is
  slow, failed, timed out, or needs backend-specific reasoning.
version: 0.5.0
---

# Daemon Manual — Router

The `daemon` tool schema covers dispatch/follow-up/check/reclaim. This manual
routes to deeper operational references: how to inspect daemon artifacts, decide
whether work is stuck, use CLI backends safely, and clean up old emanations.

Scope note: this manual does **not** restate the daemon tool argument schema, and
it does not document cross-process recovery/orphan-detection internals. For the
broader runtime turn loop that daemon emanations mirror, use `lingtai-kernel-anatomy`
and its runtime-loop reference.

Use the smallest reference that matches the problem. Do not kill or reclaim a
daemon on a hunch; inspect first.

## Nested reference catalog

`daemon-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: daemon-forensics
  location: reference/forensics/SKILL.md
  description: |
    Daemon artifact forensics: persistent daemons/em-* folders, daemon.json
    status fields, chat_history.jsonl, token_ledger.jsonl, events.jsonl, and how
    to inspect progress without guessing.
- name: daemon-inspection
  location: reference/inspection/SKILL.md
  description: |
    Polling cadence, stall heuristics, anti-patterns, backend-specific polling
    notes, and reminders before resting while daemon work remains pending.
- name: daemon-cli-backends
  location: reference/cli-backends/SKILL.md
  description: |
    Daemon API details and CLI backends: daemon(action=list), claude/claude-p/codex/opencode behavior,
    backend_options flag passing, preset/capability inheritance, and Codex
    modal capabilities.
- name: daemon-cleanup
  location: reference/cleanup/SKILL.md
  description: |
    Scope boundaries and daemon footprint cleanup: what the manual does not
    cover, reclaim persistence, and safe cleanup of old daemon artifacts.
```

## Router table

| Need / keywords | Read |
|---|---|
| Find an emanation's folder; inspect `daemon.json`, transcript, token ledger, event log; understand result paths or token attribution | `reference/forensics/SKILL.md` |
| Decide whether a daemon is stuck; choose when to list/check/tail; avoid polling too often; set a reminder before resting | `reference/inspection/SKILL.md` |
| Use `daemon(action="list")`; choose `lingtai` vs `claude`/`claude-p`/`codex`/`opencode`; pass `backend_options`; understand CLI backend limitations | `reference/cli-backends/SKILL.md` |
| Retire or audit old daemon artifacts; understand what `reclaim` does and does not delete; scope boundaries | `reference/cleanup/SKILL.md` |

## Quick decision tree

1. **Need only the daemon tool argument schema?** Use the tool description.
2. **Daemon seems slow?** Read `reference/forensics/SKILL.md`, then
   `reference/inspection/SKILL.md` if you might intervene.
3. **Daemon failed/timed out?** Read recent events/transcript via the forensics
   reference before retrying.
4. **Choosing an execution backend or flags?** Read
   `reference/cli-backends/SKILL.md`.
5. **Cleaning old folders?** Read `reference/cleanup/SKILL.md` and avoid deleting
   useful forensic evidence without a reason.

## Core rules to keep resident

- Keep daemon lightweight. If the task needs long-lived persona, molt/pad,
  durable knowledge, or ongoing ownership, spawn an avatar/agent instead of
  stretching daemon.
- Per-task `system_prompt` is a one-run parent instruction overlay. It may narrow
  how the daemon works, but it does not override lifecycle limits, available tool
  schema, or tool execution/approval guards.
- `email` is available to LingTai daemons only when explicitly requested in
  `tools:["email"]`; parent prompts still own who the daemon may contact.
- LingTai-backend daemon tool calls go through the kernel `ToolExecutor` /
  `ToolCallGuard` path before dispatch, so guarded side effects are not allowed
  to bypass normal proposal/execution policy just because they run in a daemon.
- Each emanation is disposable memory but durable evidence: its folder persists
  after completion or reclaim until cleanup.
- `daemon(action="list")` is a status overview, not a full transcript.
- Completion is push-notified; do not poll only to ask "is it done yet".
- **Idle care before resting: completion is push-notified, but do not rest on
  that alone.** With daemon work pending and unverified-healthy, arm at least
  one self-wake (a `.notification/cron.json` reminder) before going IDLE, with
  the delay set from the task's expected duration. On wake, health-check — daemon
  `state`/`last_output_at` advancing, `current_tool`/`tool_call_count` changing,
  events alive — and if there is no progress, reclaim/downgrade/switch path and
  report rather than waiting indefinitely. See `reference/inspection/SKILL.md`.
- If repeated-call `_advisory` appears on `daemon(list/check)`, the call still
  ran; treat it as a signal to stop the loop, centralize status checking in the
  parent, and read `reference/inspection/SKILL.md` before polling again.
- If an emanation might be stuck, inspect state changes, recent transcript, and
  event activity before reclaiming.
- CLI backend flags are passthroughs. Verify the current CLI's `--help` before
  relying on a flag.

## Maintenance

Keep this router short. Put new backend recipes, inspection examples, and cleanup
procedures in nested references so agents load only the needed detail.
