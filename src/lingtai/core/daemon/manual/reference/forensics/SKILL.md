---
name: daemon-forensics
description: >
  Nested daemon-manual reference for daemon artifact forensics: persistent
  daemons/em-* folders, daemon.json status fields, artifacts.json manifest,
  chat_history.jsonl, token_ledger.jsonl, events.jsonl, exit code 143 / SIGTERM,
  and how to inspect progress without guessing.
version: 1.2.0
last_changed_at: "2026-06-24T16:53:19-07:00"
---

# Daemon Forensics Reference

Nested daemon-manual reference. Open this when you need to inspect an emanation's
on-disk state, transcript, or token/event artifacts.

## Each emanation is a forensic mini-avatar

Every time you call `daemon(action="emanate", tasks=[...])`, each task gets a working folder under `daemons/` in your own directory. The folder is named:

    daemons/em-<N>-<YYYYMMDD-HHMMSS>-<6 hex>/

where `em-<N>` is the in-context handle (e.g. `em-3`). The handle resets to `em-1` after `reclaim`, but the timestamp+hash means historical folders never collide. **Folders persist forever** — `reclaim` only stops processes, not files. They're cleaned up incidentally when you molt (which wipes the working directory).

This means: when an emanation looks stuck, you can read its actual state instead of guessing. Don't kill it on a hunch — inspect first.

## Folder layout

```
daemons/em-3-20260427-094215-a1b2c3/
├── daemon.json                  ← versioned identity card + live status snapshot + visible call parameters
├── artifacts.json               ← compact artifact manifest (path/size/mtime/role per file) — written at terminal time
├── result.txt                   ← full terminal result when available
├── .prompt                      ← system prompt as built (forensic)
├── .heartbeat                   ← mtime touched on every write
├── history/
│   └── chat_history.jsonl       ← full LLM transcript
└── logs/
    ├── token_ledger.jsonl       ← per-call token usage
    └── events.jsonl             ← daemon_start, tool_call, tool_result, cli_output, daemon_done/...
```

### `artifacts.json` — the manifest, and how `check` surfaces it

Each terminal transition writes a compact **artifact manifest** to
`artifacts.json`: a metadata-only index of the run dir's important files. Each
entry is `{path, size, mtime, role}` (run-dir-relative path, byte size, ISO-8601
mtime, and an inferred role like `status` / `result` / `prompt` / `transcript` /
`events` / `token_ledger`, or `null` for unrecognized work-product files). The
manifest also carries run-level `state`, `result_path`, and `error_path` (the
last set to `result.txt` for failed/timeout/cancelled runs). Artifact entries are
run-dir-relative; run-level `result_path` / `error_path` intentionally preserve the
absolute-path convention already returned by `daemon(check)`, so humans can open
the full files directly. It lists **paths and metadata only — never file contents**
— and caps the number of entries (recording `artifacts_total` and `truncated` when
a run drops more files than the cap). Because names and relative paths are still
visible metadata, avoid creating daemon work products whose filenames themselves
contain secrets.

`daemon(action="check")` surfaces this as an `artifacts` block so you don't have
to scan the folder yourself: it prefers the persisted `artifacts.json`
(`source: "manifest"`) and, for a still-running run or an old run that predates
the manifest, computes an equivalent listing on the fly (`source: "fallback"`).
Read the `artifacts` block first to learn which files exist and how big they are,
then open `result_path` / `error_path` for the full content.

Progressive disclosure starts with `daemon(action="list")`: it reads these
per-run JSON/files and returns a compact searchable index (metadata, visible call
parameters, prompt/result previews, paths). `daemon.json` carries a
`data_version`; if the file is missing, corrupt, or stale, list-time lazy
migration rebuilds a best-effort replacement from the folder name, `.prompt`,
`result.txt`, mtimes, and recent `events.jsonl`, recording a `migration`
reason. If the index is not enough, read the returned `.prompt` or
`result.txt` directly. Only then drop to full forensic grep over
`logs/events.jsonl`, `history/chat_history.jsonl`, or `logs/token_ledger.jsonl`.

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

### Exit code 143 / SIGTERM — *terminated*, not *failed assertion*

CLI-backend emanations (`claude-p`, `codex`, and the other coding-CLI backends)
report the child process's POSIX exit code. **Exit code 143 is `128 + 15`, i.e.
the process was killed by signal 15 (`SIGTERM`).** It almost never means the
agent's own logic failed, a test assertion broke, or the model produced a wrong
answer. It means *something outside the child sent it SIGTERM and the child obeyed
and exited*. Read it as "this run was terminated from the outside," not "this run
computed the wrong thing."

This matters because 143 is easy to misread. A short transcript that ends in exit
143 looks like a crash; it is usually a **starvation or reclaim**, where the agent
was still doing useful work (often still reading/orienting) when it was cut off.
Do not "fix" it by editing the agent's task or the code it was touching — there is
typically nothing wrong with either.

**Why 143 happens (who sends the SIGTERM):**

- **Turn/time budget hit a watchdog.** A `max_turns` (or wall-clock) cap set too
  low for the task's explore-then-act shape: the daemon watchdog SIGTERMs the
  child mid-exploration. This is the most common cause for `claude-p` / `codex`.
  See the `max_turns` guidance in `reference/cli-backends/SKILL.md`.
- **`reclaim` / cancel.** A parent (or a human) ran `daemon(action="reclaim")`,
  or otherwise cancelled the run. `reclaim` stops the process — that stop is a
  SIGTERM, surfacing as 143.
- **Parent reclaim on shutdown.** When the parent agent molts, restarts, or is
  itself terminated, its child daemons get SIGTERM'd as part of tearing down the
  process tree.
- **Outer harness / timeout.** An enclosing harness, supervisor, `timeout(1)`
  wrapper, container stop, or OS-level shutdown signals the process group. The
  CLI backend (Claude Code, Codex, …) is the immediate victim, but the origin is
  the layer above the daemon.
- **CLI backend process tree killed.** The underlying coding CLI spawns its own
  subprocesses; if that tree is signalled (or the CLI exits and propagates),
  the recorded code is 143.

**How to handle a 143 (inspect before reacting):**

1. **Read the artifacts first — do not blind-rerun.** Open `daemon.json`
   (`state`, `error`, `elapsed_s`, `last_output_at`, `result_preview`/
   `result_path`) and tail `logs/events.jsonl` and `result.txt`. Check whether the
   run *already produced the deliverable* before it was killed; partial results
   are common with 143 and may be enough.
2. **Decide whether it was starved or cancelled.** Short `elapsed_s` + a low turn
   cap in `daemon.json` (`backend_options` / `backend_argv`) → starvation; raise
   or unset the cap and re-dispatch the *same* task. A `reclaim`/cancel event in
   the parent's `logs/events.jsonl` near the same timestamp → it was intentional;
   confirm intent before re-running.
3. **Check for a real timeout.** If wall-clock matches a configured timeout, the
   task was too big for the window: split it, give it more budget, or hand it to a
   fresh daemon scoped to the remaining work.
4. **Hand off, don't hand-patch.** If work remains, dispatch a **new** daemon that
   *consumes the previous emanation's visible artifacts* (prior task prompt,
   `result.txt`, partial output files) and continues from there — rather than the
   parent editing the half-done files by hand or reviving the dead session. A
   daemon is disposable memory but durable evidence; point the successor at the
   evidence.
5. **Only treat 143 as a genuine error** if inspection shows the SIGTERM came from
   *inside* the task (e.g., the agent's own script killed its process group) — rare.

**How to report a 143 to a human.** Say *terminated*, not *failed*, and name the
likely sender. Example: "em-3 (`claude-p`) exited 143 = SIGTERM (terminated by the
turn-budget watchdog ~90s in, mid-exploration). It is **not** a test/code failure;
the task starved. It had read 6 files but not started the edit. Re-dispatching with
`max_turns` unset." Avoid phrasing like "the daemon failed / the tests failed / the
model crashed" — that misattributes an external kill to the agent's logic and
invites a pointless rerun or a manual fix.
