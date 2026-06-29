---
name: daemon-cli-backends
description: >
  Nested daemon-manual reference for daemon API details and CLI backends:
  daemon(action=list), claude-p/codex/opencode behavior,
  backend_options flag passing, preset/capability inheritance, and Codex modal
  capabilities.
version: 1.3.0
last_changed_at: "2026-06-15T22:36:44-07:00"
---

# Daemon CLI Backend Reference

Nested daemon-manual reference. Open this when choosing a daemon backend,
inspecting `daemon(action="list")`, or passing CLI flags through `backend_options`.

## API note: `daemon(action="list")`

`list` is a compact index over both currently tracked runs and historical run
folders. By default it scans `daemons/*/daemon.json` and returns completed,
failed, cancelled, timed-out, and running entries with `run_id`, `group_id`,
`status`, `backend`, task preview, visible call parameters (`task`, `tools`,
`skills`, redacted `mcp`, system-prompt preview when recorded), result preview,
and filesystem paths. If a historical run folder has no `daemon.json`, has
invalid JSON, or has a mismatched `data_version`, `list` lazily writes a
best-effort replacement using the folder name, `.prompt`, `result.txt`,
`.heartbeat`/mtimes, and recent `events.jsonl`. Use `contains` for
case-insensitive substring search over that visible index, `status` for status
filtering, `last` as a positive result limit, and `include_done=false` when
you only want currently tracked in-memory runs. This is the first layer of progressive disclosure; read the returned
`.prompt`/`result.txt` paths for detail, and grep `events.jsonl` /
`chat_history.jsonl` / `token_ledger.jsonl` only for forensic depth.


## Bash harness subskills

Daemon backend integration and user-facing shell execution guidance now split by
ownership:

- This page owns the daemon API contract: backend names, `daemon(...)` behavior,
  `backend_options`, result/session capture, `ask`/resume, and backend-specific
  parser caveats.
- `bash-manual` owns the shell subprocess recipes for the underlying CLIs. Before
  launching or troubleshooting a long-running coding CLI directly from bash,
  read the matching nested bash reference:
  - Claude Code: `bash-manual` → `reference/bash-claude-code/SKILL.md`
  - OpenAI Codex: `bash-manual` → `reference/bash-openai-codex/SKILL.md`
  - OpenCode: `bash-manual` → `reference/bash-opencode/SKILL.md`
  - Cursor Agent: `bash-manual` → `reference/bash-cursor-agent/SKILL.md`
  - MiMo Code: `bash-manual` → `reference/bash-mimocode/SKILL.md`
  - Qwen Code: `bash-manual` → `reference/bash-qwen-code/SKILL.md`
  - Oh-My-Pi / Pi Coding Agent: `bash-manual` →
    `reference/bash-oh-my-pi/SKILL.md`

Candidate harnesses that are not daemon backends yet (Gemini CLI, Aider, Goose,
OpenHands, Crush, and Zed/ACP bridges) are tracked under `bash-manual` as
`reference/bash-*/SKILL.md` pages until their command/session contracts are
stable enough for backend promotion.

## CLI backends

The `backend` parameter selects the execution engine for emanations. Default is
`lingtai` (the built-in ChatSession loop). External CLI backends are also
available:

For Claude Code daemon work, use the print-mode backend `claude-p` (alias
`claude-code`). The legacy interactive PTY/TUI backend (`claude` /
`claude-interactive`) is **no longer a user-selectable backend** — it is hidden
from the daemon schema enum and description. Do not select it for new work; it
proved unreliable under the daemon watchdog (mid-exploration SIGTERM / exit code
143). The print-mode backend covers the same Claude Code use cases without
driving a TUI. The interactive code path remains in the tree only so older
callers and stored daemon entries that recorded `backend="claude"` keep
resolving.

| Backend | CLI command | Session resume | Notes |
|---------|-------------|----------------|-------|
| `lingtai` | (built-in) | N/A — in-process `ask` | Default. Uses preset resolution, tool surface curation, model routing. |
| `claude-p` | `claude --print --dangerously-skip-permissions --output-format stream-json --verbose --name <em_id> <task>` | `claude --resume <claude_session_id> --print ...` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Print-mode Claude Code backend (the recommended Claude backend). Wraps Claude Code's official `--print`/stream-json mode. Session ID is captured from stream-json output, so `ask` is usable as soon as `emanate` returns. |
| `claude-code` | same as `claude-p` | same as `claude-p` | Backward-compatible alias retained for existing callers and stored daemon entries. |
| `codex` | `codex exec --json --dangerously-bypass-approvals-and-sandbox <task>` | `codex exec resume <codex_session_id>` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Mirrors the print-mode Claude backend. `thread.started` event carries the session id (codex internally calls it `thread_id`), captured immediately. `ask` resumes the same conversation context. |
| `opencode` | `opencode run --format json <prompt>` | `opencode run --session <opencode_session_id> ...` via `ask` (async) | Uses opencode's session id/event vocabulary. |
| `mimocode` / `mimo` | `mimo run --format json <prompt>` | `mimo run --session <mimocode_session_id> --format json ...` via `ask` (async) | MiMo Code CLI backend (npm package `@mimo-ai/cli`, binary `mimo`). `mimo` canonicalizes to `mimocode`. |
| `qwen-code` / `qwen` | `qwen --yolo -p <prompt>` | Not supported yet; `ask` returns an explicit unsupported-backend error | Qwen Code CLI backend (npm package `@qwen-code/qwen-code`, binary `qwen`). `qwen` canonicalizes to `qwen-code`. |
| `oh-my-pi` / `omp` | `omp --mode json --approval-mode yolo <prompt>` | `omp --mode json --approval-mode yolo --session <oh_my_pi_session_id> ...` via `ask` (async) | Oh-My-Pi pi-coding-agent CLI backend (npm package `@oh-my-pi/pi-coding-agent`, binary `omp`). `--mode json` is non-interactive JSON event-stream print mode; the first `type:session` header line carries the resumable session id. `omp` canonicalizes to `oh-my-pi`. |
| `cursor` | `agent -p <prompt>` | `agent -p --resume <cursor_session_id> ...` via `ask` (async) | Cursor Agent CLI backend. |

**Per-task system prompt.** Every task item may include `system_prompt`. Use it
as the parent agent's one-run behavior contract: the daemon's role, constraints,
tool-use policy, collaboration boundaries, safety posture, and interpretation
rules. Keep `task` focused on the concrete objective and deliverable; put the
explanation of *how to behave while doing it* in `system_prompt`. When the
daemon needs a workflow, pass `skills: [...]` as skill directories or direct
`SKILL.md` paths; the daemon runtime renders them into a compact YAML skill list
in the one-run prompt. Omit `system_prompt` or leave it blank for the default
daemon persona. For the built-in `lingtai` backend
it is appended to the daemon's system prompt as a bounded oneshot parent
instruction; it cannot override lifecycle limits, tool schemas, or the
ToolExecutor/ToolCallGuard execution gate. For CLI backends, the same text is
also embedded at the top of the task prompt and persisted in the daemon `.prompt`
file for forensics.

**LingTai backend tool surface.** The built-in `lingtai` backend uses preset
resolution plus daemon tool curation. Parent MCP tools are not auto-inherited:
provide full one-run MCP registrations per task with `mcp: [{name, transport,
...}]`. The runtime serializes those registrations into the oneshot prompt as
YAML for every backend. For the built-in LingTai backend, it also starts the
registered MCP clients for this run and exposes their tools in the daemon tool
surface; clients are closed when the run finishes. CLI backends do not receive
native config injection in this PR, but they do receive the same YAML context and
may load it if their runtime supports MCP. Secret `env`/`headers` values are
redacted in prompts. The daemon-eligible `email` intrinsic is available by
default so an emanation can communicate in the local agent network when the task
requires it; other intrinsics remain unavailable to keep daemon lightweight and
non-recursive. As with file/bash/web/MCP tools, technical availability is not a
policy by itself: the parent should use `system_prompt` to say when and how the
daemon may use any available tool, including who it may contact and what context
it may share if email is involved.

**When to use CLI backends:** Use them when the task benefits from a different
agent runtime's tool surface (for example Claude Code's built-in file editing or
Codex's sandboxed execution) rather than the LingTai emanation's curated tool
set. `mimocode`/`mimo`, `qwen-code`/`qwen`, and `oh-my-pi`/`omp` are accepted as canonical backend names plus short aliases; persisted daemon entries use the canonical name.

**Claude backend naming:** Select `claude-p` for Claude Code daemon work; it is
the print-mode backend that wraps Claude Code's official `--print`/stream-json
mode. `claude-code` remains a compatibility alias for `claude-p` so older calls
and persisted daemon entries keep working. The interactive PTY/TUI names
(`claude` / `claude-interactive`) are hidden from the daemon schema and are not
user-selectable; only existing stored entries still resolve through them.

**Claude Code auth environment hygiene.** The print-mode Claude backend
(`claude-p` / compatibility `claude-code`) — and the hidden legacy interactive
path it shares code with — start `claude` with auth override variables stripped
from the subprocess environment.
This includes `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` (which force API
billing; GH #107) and `CLAUDE_CODE_OAUTH_TOKEN` (a stale inherited token can
override a refreshed `~/.claude/.credentials.json` and appear as a false
"weekly limit"; see Lingtai-AI/lingtai#189). If a manual shell invocation of
`claude` reports a quota/weekly-limit error, run a tiny smoke test with the stale
env token unset before concluding the account is actually exhausted:

```bash
env -u CLAUDE_CODE_OAUTH_TOKEN claude -p 'Reply exactly OK' --allowedTools Read -c
```

Do not print token values while diagnosing; `claude auth status` plus redacted
environment variable names are enough.

**CLI backends skip preset resolution** — the external CLI manages its own model,
tools, and permissions. The `tools` field in the task spec is ignored for CLI
backends.

## Passing free-form CLI flags via `backend_options`

For CLI backends, each task may carry an optional `backend_options` JSON object
that is converted to argv tokens and appended to the CLI command before the task
prompt. This lets you reach the underlying CLI's flag surface (model selection,
search/web access, effort levels, sandbox/policy switches, etc.) without the
daemon needing to hard-code every flag.

This is intentionally a passthrough, not a fixed table. Claude Code, Codex,
OpenCode, MiMo Code, Qwen Code, Oh-My-Pi, and Cursor rev their flag lists between
releases. Before adding new options, run the installed CLI's `--help` in `bash`
to discover what it supports today (`claude --help`, `codex exec --help`,
`opencode run --help`, `mimo run --help`, `qwen --help`, `omp --help`, or
`agent --help`). Anything here is illustrative, not authoritative. Note that
each backend reserves its own harness-owned flags (e.g. Oh-My-Pi reserves
`--mode`, `--approval-mode yolo`, and the session/`--resume` flags) — passing a
reserved flag in `backend_options` refuses the whole batch with a clear error.

```jsonc
// Print-mode Claude backend (the recommended Claude backend)
{
  "action": "emanate",
  "backend": "claude-p",
  "tasks": [{
    "task": "Review this PR and summarize risks.",
    "tools": [],
    "backend_options": {
      "model": "claude-opus-4-7"
    }
  }]
}

// Codex with model + web search
{
  "action": "emanate",
  "backend": "codex",
  "tasks": [{
    "task": "Find the breaking change in the last release.",
    "tools": [],
    "backend_options": {
      "model": "gpt-5",
      "search": true
    }
  }]
}
```

**Conversion rules** (validated before any process is spawned — a single bad
spec refuses the whole batch with a clear `ValueError`):

| Value type | Result |
|---|---|
| `true` | flag only, e.g. `{"search": true}` → `--search` |
| `false` / `null` | flag omitted entirely |
| string / int / float | `--flag <value>` |
| list of scalars | repeated: `--flag v1 --flag v2 ...` |
| nested object / array of objects | **rejected** with `ValueError` |

**Key safety:** keys must look like CLI flag names (letters/digits with `-` or
`_` separators, no leading `-`, no spaces). Underscores in keys are converted to
dashes in the emitted flag, so JSON-friendly `{"approval_policy":"never"}`
becomes `--approval-policy never`. Unsafe keys are rejected before any subprocess
call.

**Do not set a CLI backend's `max_turns` too low.** A turn budget that fits a
quick scripted task will kill a Claude Code / Codex backend mid-exploration — the
agent is still reading files and orienting when the watchdog terminates it,
surfacing as **exit code 143** (SIGTERM) with little or no useful output. The
exploration-then-act shape of these agents means early turns are spent on reads
and greps, not the deliverable; budget for that. If you must cap turns, size the
cap to the *full* task (explore + act + verify), not to a single edit, and prefer
leaving `max_turns` unset over guessing low. Treat a 143 exit with a short
transcript as "I starved it," not "the model failed."

**Claude reserved flags:** The `claude-p` daemon backend owns its execution mode.
`backend_options` cannot override harness-owned flags such as `--print` or
`--output-format`; attempts are rejected before spawn. `claude-p` must keep
stream-json output so daemon progress/result extraction remains reliable. (The
hidden legacy interactive `claude` path also reserves `--settings` and the
managed system-prompt flags, but that backend is no longer user-selectable.)

**When it applies:** `backend_options` is honored only at `emanate` time (when
the CLI session is first spawned). `daemon(action="ask", ...)` reuses the
existing session via `claude --resume` / `codex exec resume` / backend-specific
resume and does not re-pass `backend_options` — the runtime flags chosen at
emanate time persist for the life of the session.

**Where it shows up on disk:** resolved options are written into the emanation's
`daemon.json` (`backend_options` field for the raw object, `backend_argv` for the
converted argv tokens), plus a `daemon_backend_options` entry in the parent's
`logs/events.jsonl`. A later `daemon(action="check", id="em-N")` and the events
log together let you reconstruct exactly which flags were passed.

`lingtai` backend ignores `backend_options`: there is no CLI process to forward
it to.

## Progress, resume, and `ask`

**Working directory:** CLI backends run in the parent agent's working
directory (`_working_dir`), not in the emanation's `daemons/em-N-*/` folder. The
`daemons/` folder still tracks daemon state (`daemon.json`, logs) and terminal
output (`result.txt`).

**Progress delivery:** CLI stdout/stderr and parsed transcript events are
persisted to the run directory as `cli_output` events and
`daemon.json.last_output`; they are not injected into the parent as ordinary
`[daemon:em-N]` request text. Completion/failure publishes one compact system
notification.

**`ask` on CLI backends is asynchronous.** For resumable CLI emanations,
`daemon(action="ask", id="em-N", message="...")` spawns/resumes the backend and
returns immediately with `{"status":"sent","async":true}`. Progress streams
into the same run directory (`cli_output` events, `last_output`); the final reply
text arrives as a `follow-up completed` system notification and is also visible
via `daemon(action="check", id="em-N")`. Poll `check` (or wait for the
notification) instead of expecting the reply in the `ask` return value.

While one CLI `ask` is in flight against a given emanation, a second `ask` to the
same id returns `{"status":"busy", ...}`. CLI sessions serialize per session, so
wait for the first follow-up to complete before sending another. The `lingtai`
backend's `ask` is unchanged: it buffers into a per-emanation followup buffer and
is drained by the in-process run loop.

## Backend-specific observability

**Legacy interactive Claude (`claude` — hidden, not user-selectable).** This
backend is no longer offered in the daemon schema. Older stored runs may still
carry `claude_interactive_*` fields in `daemon.json` (managed-workspace path,
transcript path, raw PTY log, trust-answer flag, etc.); they are kept only for
forensics on historical runs. Do not start new emanations on this backend — use
`claude-p`.

**Print-mode Claude (`claude-p` / `claude-code`).** `claude_session_id` is set on
the first stream-json event that carries a session id (typically the system
`init` event, within milliseconds of process start). Earlier versions wrote the
session id only post-hoc by scanning `~/.claude/projects/`; that scan remains a
fallback if the stream never carries a session id.

**Codex.** `codex_session_id` (stored as `daemon.json.codex_session_id`) is set
on the first event — `{"type":"thread.started","thread_id":"<uuid>"}` — within
milliseconds of process start. `ask` runs `codex exec resume <codex_session_id>
--json "<message>"` asynchronously.

**Token accounting:** external CLI token/spend fields are deliberately not mixed
into the parent/kernel token ledger. They use separate billing paths and cache
semantics. Spend/progress remains visible through daemon run artifacts.
