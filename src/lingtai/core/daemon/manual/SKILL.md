---
name: daemon-manual
description: >
  Operational guide for the `daemon` tool — how to inspect, debug, and reason
  about emanations (the agent fragments you spawn for parallel sub-tasks).

  Reach for this manual when:
    - You called `daemon(action="emanate", ...)` and the emanation has been
      running long enough that you want to know if it's stuck, slow, or
      working — without killing it on a hunch.
    - An emanation finished with `state=failed` or `state=timeout` and you
      need to figure out why.
    - You want to inspect on-disk artifacts of a past emanation (chat
      transcript, token spend, event log) — the folders persist forever and
      `daemon(action="list")` only shows currently-active runs.
    - You're trying to understand why your token totals don't match the sum
      of what your tools claim — daemon spend is tagged into your parent
      ledger and may explain the gap.

  This manual covers: the on-disk folder layout under `daemons/em-N-*/`,
  exactly which fields in `daemon.json` answer "is it stuck?" vs "is it
  thinking?", how to tail `chat_history.jsonl` to see latest progress,
  where token attribution lives, how `reclaim` interacts with persistence,
  and a worked example of inspecting a 5-minute-running emanation.

  Does NOT cover: the daemon tool's argument schema (in the tool description
  itself) or the cross-process recovery / orphan-detection mechanics
  (those need separate spec work).

  Companion: `lingtai-kernel-anatomy reference/runtime-loop.md` covers the broader
  agent runtime that emanations are mini-versions of. Read that first if
  you don't yet understand the turn loop, then come here for daemon-specific
  inspection patterns.
version: 0.2.0
---

# daemon manual

The `daemon` tool's schema description covers the happy path. This manual is the deeper reference: how to inspect a slow or failed emanation, the on-disk artifact layout, and worked examples.

## Each emanation is a forensic mini-avatar

Every time you call `daemon(action="emanate", tasks=[...])`, each task gets a working folder under `daemons/` in your own directory. The folder is named:

    daemons/em-<N>-<YYYYMMDD-HHMMSS>-<6 hex>/

where `em-<N>` is the in-context handle (e.g. `em-3`). The handle resets to `em-1` after `reclaim`, but the timestamp+hash means historical folders never collide. **Folders persist forever** — `reclaim` only stops processes, not files. They're cleaned up incidentally when you molt (which wipes the working directory).

This means: when an emanation looks stuck, you can read its actual state instead of guessing. Don't kill it on a hunch — inspect first.

## Folder layout

```
daemons/em-3-20260427-094215-a1b2c3/
├── daemon.json                  ← identity card + live status snapshot
├── result.txt                   ← full terminal result when available
├── .prompt                      ← system prompt as built (forensic)
├── .heartbeat                   ← mtime touched on every write
├── history/
│   └── chat_history.jsonl       ← full LLM transcript
└── logs/
    ├── token_ledger.jsonl       ← per-call token usage
    └── events.jsonl             ← daemon_start, tool_call, tool_result, cli_output, daemon_done/...
```

## Inspection patterns

### "Is this emanation actually doing anything?"

Read `daemon.json` once. The fields you want:

- `state` — `running` / `done` / `failed` / `cancelled` / `timeout`
- `current_tool` — `"read"` / `"bash"` / null. If null while `state=running`, the emanation is waiting on the LLM. If non-null, it's executing that tool.
- `turn` — which LLM round the emanation is on
- `tool_call_count` — how many tool dispatches it has done
- `tokens` — running totals (lingtai backend only; stays at 0 for `claude-code` and `codex` backends — see "CLI backends" below)
- `last_output` / `last_output_at` — recent stdout/stderr from CLI backends
- `result_preview` / `result_path` — bounded terminal preview and full `result.txt` path after completion
- `elapsed_s` — wall clock since start

If `current_tool` is null AND `tool_call_count` hasn't changed for a while, the LLM is thinking — wait. If `current_tool` is set and stays set, that tool is slow (e.g., a big file read or a long bash command).

### "What has it figured out so far?"

Tail `history/chat_history.jsonl`. Each line is one role/turn entry:

- `{role: "user", kind: "task"}` — the original task
- `{role: "assistant", text: "..."}` — what the emanation said
- `{role: "user", kind: "tool_results"}` — what the tools returned
- `{role: "user", kind: "followup"}` — your `daemon(action="ask", ...)` messages

Read the most recent assistant text to see the latest progress narrative.

### "What did it spend?"

Either of:
- `daemon.json` field `tokens` — running totals across the whole run
- `logs/token_ledger.jsonl` — per-call entries, sortable by line

The same per-call entries are also in your own `logs/token_ledger.jsonl` (the parent's), tagged with `source: "daemon"` and `em_id`. Your lifetime token totals (what `sum_token_ledger` reports) include all daemon spend.

### "Why did it fail?"

Read `daemon.json`'s `error` field — `{type, message}`. For more depth, tail `logs/events.jsonl` for the `daemon_error` event and look at the preceding `tool_call`/`tool_result` entries to see what was happening just before the failure.

## Worked example: a daemon that's been running 5 minutes

You called `daemon(action="emanate", ...)` for `em-3`, asked it to "scan src/ for security issues", and it's been running 5 minutes. You're nervous.

```bash
# What's the live state?
read("daemons/em-3-20260427-094215-abc123/daemon.json")
# → state=running, turn=8, current_tool=null, tool_call_count=15, tokens.input=22000

# Last few lines of the transcript
bash("tail -n 20 daemons/em-3-20260427-094215-abc123/history/chat_history.jsonl")
# → assistant: "Found a potential SQL injection in db.py:42. Continuing..."

# Recent tool activity
bash("tail -n 10 daemons/em-3-20260427-094215-abc123/logs/events.jsonl")
# → series of read/grep events on src/db/, src/auth/
```

That's a healthy pattern: the LLM is between tool calls, has good progress narrative, and is steadily working through files. **Don't reclaim.** Let it cook.

## API note: `daemon(action="list")`

`list` reports only currently-active emanations (in-memory registry). It includes `run_id` and `path` so you know where to read on disk. Historical (completed/failed/cancelled) emanations don't appear in `list` — find them with `bash("ls daemons/")` instead.

## CLI backends

The `backend` parameter selects the execution engine for emanations. Default is `lingtai` (the built-in ChatSession loop). Two external CLI backends are also available:

| Backend | CLI command | Session resume | Notes |
|---------|------------|----------------|-------|
| `lingtai` | (built-in) | N/A — in-process `ask` | Default. Uses preset resolution, tool surface curation, model routing. |
| `claude-code` | `claude --print --dangerously-skip-permissions --output-format stream-json --verbose --name <em_id> <task>` | `claude --resume <claude_session_id>` via `ask` | Session ID captured from the first event of the stream-json output (typically within ms of process start), so `ask` is usable as soon as `emanate` returns — even while the initial task is still running. |
| `codex` | `codex exec --json --dangerously-bypass-approvals-and-sandbox <task>` | `codex exec resume <codex_session_id>` via `ask` | Mirrors claude-code. `thread.started` event carries the session id (codex internally calls it `thread_id`), captured immediately. `ask` resumes the same conversation context. |

**When to use CLI backends:** When the task benefits from a different agent runtime's tool surface (e.g., Claude Code's built-in file editing, Codex's sandboxed execution) rather than the lingtai emanation's curated tool set.

**CLI backends skip preset resolution** — the external CLI manages its own model, tools, and permissions. The `tools` field in the task spec is ignored for CLI backends.

**Working directory:** Both CLI backends run in the parent agent's working directory (`_working_dir`), not in the emanation's `daemons/em-N-*/` folder. The `daemons/` folder is used for tracking state (`daemon.json`, logs) and terminal output (`result.txt`).

**Progress delivery:** CLI stdout/stderr is persisted to the run directory as `cli_output` events and `daemon.json.last_output`; it is not injected into the parent as ordinary `[daemon:em-N]` request text. Completion/failure publishes one compact `system` notification telling the parent which daemon finished and to inspect it with `daemon(action="check", id="em-N")`.

**Claude Code backend specifics.** The backend streams structured JSON events from Claude Code in real time (`--output-format stream-json --verbose`):

- `daemon(check)` sees live progress as each assistant turn arrives — `last_output` updates per turn and `current_tool` tracks Claude Code's own tool calls (`set` on `tool_use` blocks, `clear` on the matching `tool_result`). Note that `tokens` stays at 0 — Claude Code runs through its own provider account and we deliberately don't merge its `usage` fields into the kernel's token ledger (they'd mix with native LLM-adapter accounting that has different cache semantics). Spend is visible to the human via Claude Code's own UI and the `cli_output` event stream.
- `claude_session_id` is set on the first event that carries a session id (typically the system `init` event, within ms of process start). This means `daemon(action="ask", id="em-N", message="...")` works the moment `emanate` returns — you don't have to wait for the initial task to complete. (Earlier versions wrote the session id only post-hoc by scanning `~/.claude/projects/`; that scan is now a fallback for the unusual case where the stream never carried a session id.)
- stderr is captured to its own pipe (no longer merged into stdout) and persisted as `cli_output` events with `stream="stderr"`, so API errors, auth failures, and rate limits are visible during the run rather than buried in a buffered stdout.
- `turn` is not incremented for CLI backends — Claude Code runs its own LLM loop and we don't see "turns" in the same sense. Use `last_output` and `cli_output` events to gauge progress.
- An `is_error=true` in the final `result` event is surfaced as a failed emanation even when the underlying process exited 0, so an error reported inside the LLM stream doesn't masquerade as success.

**Codex backend specifics.** Identical observability + resumability story as claude-code, with codex's own event vocabulary (`--json`):

- `daemon(check)` sees live progress: `last_output` updates as each `item.completed` event with `type=agent_message` arrives. Note that `tokens` stays at 0 — codex runs through its own provider account, and its `cached_input_tokens` semantics differ from the kernel's LLM adapters (codex's `input_tokens` already includes the cached portion, anthropic's doesn't), so we deliberately don't merge codex's `usage` into the token ledger. Spend is visible via the codex CLI's own output and the `cli_output` event stream.
- `codex_session_id` (stored as `daemon.json.codex_session_id`) is set on the first event — `{"type":"thread.started","thread_id":"<uuid>"}` — within ms of process start. `daemon(action="ask", id="em-N", message="...")` runs `codex exec resume <codex_session_id> --json "<message>"` and surfaces the resumed turn's reply text the same way `emanate` does.
- stderr is captured to its own pipe (was: merged into stdout via `--ephemeral` mode) and persisted as `cli_output` events with `stream="stderr"`.
- Codex doesn't emit an `is_error` flag like Claude Code; the kernel treats absence of a `turn.completed` event (combined with no captured `agent_message` items) as failure even when the process exits 0.
- `--ephemeral` is intentionally NOT passed: it would disable session persistence and break `daemon(ask)`. Sessions persist under `~/.codex/sessions/` and can be re-resumed by ID for the lifetime of the session record.

## What the manual does NOT cover

- Provider routing / LLM presets — deferred to a separate spec.
- Cross-process recovery — if your kernel restarted mid-daemon, the folder may show `state=running` indefinitely. Compare `now()` vs `.heartbeat` mtime to detect orphans.
- Folder cleanup — there is none. Molts wipe the working dir. For non-molting agents, you may eventually want to `rm -rf daemons/em-*-2026-04-*` manually.
