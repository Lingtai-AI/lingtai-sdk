---
name: daemon-cli-backends
description: >
  Nested daemon-manual reference for daemon API details and CLI backends:
  daemon(action=list), claude/claude-p/codex/opencode behavior,
  backend_options flag passing, preset/capability inheritance, and Codex modal
  capabilities.
version: 1.2.0
---

# Daemon CLI Backend Reference

Nested daemon-manual reference. Open this when choosing a daemon backend,
inspecting `daemon(action="list")`, or passing CLI flags through `backend_options`.

## API note: `daemon(action="list")`

`list` reports only currently-active emanations (in-memory registry). It includes
`run_id` and `path` so you know where to read on disk. Historical
(completed/failed/cancelled) emanations don't appear in `list` — find them by
inspecting the agent's `daemons/` folder.

## CLI backends

The `backend` parameter selects the execution engine for emanations. Default is
`lingtai` (the built-in ChatSession loop). External CLI backends are also
available:

| Backend | CLI command | Session resume | Notes |
|---------|-------------|----------------|-------|
| `lingtai` | (built-in) | N/A — in-process `ask` | Default. Uses preset resolution, tool surface curation, model routing. |
| `claude` | interactive `claude --settings <hook-json> --append-system-prompt-file <managed-prompt>` under a PTY, cwd under `~/.lingtai-claude/runs/<run_id>/worktree` | interactive `claude --resume <claude_session_id> --settings <hook-json> --append-system-prompt-file <managed-prompt>` via `ask` (async) | Experimental interactive Claude Code backend. LingTai drives the TUI through a PTY, creates a LingTai-managed ephemeral workspace, injects a managed system prompt, answers terminal probes, uses `SessionStart`/`Stop` hooks for synchronization, and reads Claude's transcript JSONL for the daemon result. It does **not** directly edit Claude global config, auto-login, handle MFA/tokens, automate `claude.ai/code`, or auto-trust arbitrary workspaces. It may auto-select Claude's workspace trust prompt only for the verified LingTai-managed workspace. |
| `claude-interactive` | same as `claude` | same as `claude` | Compatibility/descriptive alias for `claude`. Prefer `claude` for new calls unless you want the explicit experimental name in artifacts. |
| `claude-p` | `claude --print --dangerously-skip-permissions --output-format stream-json --verbose --name <em_id> <task>` | `claude --resume <claude_session_id> --print ...` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Explicit name for the existing print-mode Claude Code backend. Session ID is captured from stream-json output, so `ask` is usable as soon as `emanate` returns. |
| `claude-code` | same as `claude-p` | same as `claude-p` | Backward-compatible alias retained for existing callers and stored daemon entries. |
| `codex` | `codex exec --json --dangerously-bypass-approvals-and-sandbox <task>` | `codex exec resume <codex_session_id>` via `ask` (async — returns immediately, reply arrives via notification / `check`) | Mirrors the print-mode Claude backend. `thread.started` event carries the session id (codex internally calls it `thread_id`), captured immediately. `ask` resumes the same conversation context. |
| `opencode` | `opencode run --format json <prompt>` | `opencode run --session <opencode_session_id> ...` via `ask` (async) | Uses opencode's session id/event vocabulary. |
| `mimocode` / `mimo` | `mimo run --format json <prompt>` | `mimo run --session <mimocode_session_id> --format json ...` via `ask` (async) | MiMo Code CLI backend (npm package `@mimo-ai/cli`, binary `mimo`). `mimo` canonicalizes to `mimocode`. |
| `qwen-code` / `qwen` | `qwen --yolo -p <prompt>` | Not supported yet; `ask` returns an explicit unsupported-backend error | Qwen Code CLI backend (npm package `@qwen-code/qwen-code`, binary `qwen`). `qwen` canonicalizes to `qwen-code`. |
| `oh-my-pi` / `omp` | `omp --mode json --approval-mode yolo <prompt>` | `omp --mode json --approval-mode yolo --session <oh_my_pi_session_id> ...` via `ask` (async) | Oh-My-Pi pi-coding-agent CLI backend (npm package `@oh-my-pi/pi-coding-agent`, binary `omp`). `--mode json` is non-interactive JSON event-stream print mode; the first `type:session` header line carries the resumable session id. `omp` canonicalizes to `oh-my-pi`. |
| `cursor` | `agent -p <prompt>` | `agent -p --resume <cursor_session_id> ...` via `ask` (async) | Cursor Agent CLI backend. |

**When to use CLI backends:** Use them when the task benefits from a different
agent runtime's tool surface (for example Claude Code's built-in file editing or
Codex's sandboxed execution) rather than the LingTai emanation's curated tool
set. `mimocode`/`mimo`, `qwen-code`/`qwen`, and `oh-my-pi`/`omp` are accepted as canonical backend names plus short aliases; persisted daemon entries use the canonical name.

**Claude backend naming:** `claude` is the interactive PTY/TUI backend. It
runs Claude Code in a LingTai-created managed workspace instead of the parent
agent directory. `claude-p` is the print-mode backend that wraps Claude Code's
official `--print`/stream-json mode. `claude-code` remains a compatibility alias
for `claude-p` so older calls and persisted daemon entries keep working.

**Claude Code auth environment hygiene.** All Claude backends (`claude`,
`claude-interactive`, `claude-p`, and compatibility `claude-code`) start
`claude` with auth override variables stripped from the subprocess environment.
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
// Interactive Claude backend
{
  "action": "emanate",
  "backend": "claude",
  "tasks": [{
    "task": "Refactor auth.py for clarity.",
    "tools": [],
    "backend_options": {
      "model": "claude-opus-4-7",
      "managed_worktree_from": "/absolute/path/to/source/repo"
    }
  }]
}

// Print-mode Claude backend
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

**Claude reserved flags:** Claude daemon backends own their execution mode.
`backend_options` cannot override harness-owned flags such as `--settings`,
`--print`, `--output-format`, or (for interactive `claude`) the managed system
prompt flags `--append-system-prompt` / `--append-system-prompt-file`; attempts
are rejected before spawn. Interactive `claude` must keep LingTai's inline hook
settings and managed prompt, while `claude-p` must keep stream-json output so
daemon progress/result extraction remains reliable.

**Interactive Claude managed workspace:** `backend="claude"` always starts in
`~/.lingtai-claude/runs/<run_id>/worktree` (or the test-only
`LINGTAI_CLAUDE_MANAGED_ROOT` override). By default this is an empty managed
workspace. If the task needs repository files, pass
`backend_options: {"managed_worktree_from": "/absolute/path/to/git/repo"}`; the
bridge consumes that LingTai-owned option, creates a detached git worktree from
that repo's `HEAD` at the managed workspace path, and does not forward the option
to Claude. The source must be inside a git repository. Claude sees the managed
workspace cwd plus a LingTai-managed system prompt that forbids credential
handling, global config mutation, and writes outside the managed workspace.

Because the cwd is created and verified under the LingTai managed runs root, the
interactive backend may answer Claude Code's workspace trust prompt with the
trust option for that workspace only. It still refuses login/onboarding prompts
and refuses workspace trust prompts outside the verified managed root.

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

**Working directory:** Most CLI backends run in the parent agent's working
directory (`_working_dir`), not in the emanation's `daemons/em-N-*/` folder. The
interactive `claude` backend is the exception: it runs in the per-run managed
workspace under `~/.lingtai-claude/runs/<run_id>/worktree`. The `daemons/` folder
still tracks daemon state (`daemon.json`, logs) and terminal output
(`result.txt`).

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

**Interactive Claude (`claude`).** The daemon writes extra fields to
`daemon.json`:

- `claude_session_id`: captured from hook payloads or transcript rows.
- `claude_interactive_transcript_path`: Claude's transcript JSONL path from the
  `Stop` hook.
- `claude_interactive_raw_pty_log`: raw ANSI PTY log for debugging terminal
  startup/hangs, written under the managed run root's `harness/` folder.
- `claude_interactive_prompt_sent`: set once the daemon pasted the task after
  `SessionStart`.
- `claude_interactive_cwd`: the Claude process cwd (the managed workspace).
- `claude_interactive_managed_root`: per-run managed root.
- `claude_interactive_managed_worktree`: managed workspace path.
- `claude_interactive_managed_source`: git root used to create the detached
  worktree, or `null` for an empty managed workspace.
- `claude_interactive_managed_source_request`: explicit `managed_worktree_from`
  request, when provided.
- `claude_interactive_system_prompt`: path to LingTai's managed system prompt.
- `claude_interactive_auto_trust`: currently `managed-workspace-only`.
- `claude_interactive_managed_trust_answered`: set only if the backend answered a
  Claude workspace trust prompt inside the verified managed workspace.

If the backend appears to be waiting for login/onboarding, or for workspace trust
outside the verified managed root, LingTai records a stderr warning and
fails/terminates rather than handling credentials or trusting arbitrary folders.

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
