---
name: bash-manual
description: >
  **Read this before running long-lived agent/coding CLIs (`claude -p`,
  `codex exec`, `opencode run`, Cursor Agent, Gemini CLI, Aider, Goose,
  OpenHands, Crush, or similar harnesses), or before setting up cron,
  launchd, systemd timers, crontab jobs, or scheduled reminders.** Router for
  Bash-related operational depth beyond the bash tool schema: async + poll
  discipline for long-running child agents, host-scheduler setup, LingTai
  wake-by-mailbox-drop, script hygiene, one-shot `.notification/cron.json`
  reminders, debugging silent jobs, and safe cleanup. Start here for any
  long-running agent CLI, time-driven recurring work ("every hour", "weekdays at
  9", "remind me later"), or when a scheduled job misbehaves.
version: 1.6.1
last_changed_at: "2026-06-24T14:38:03-07:00"
---

# Bash Manual — Router

The `bash` tool schema covers one-off command execution. This manual routes to
operational depth that is too long for the schema: host scheduling, mailbox-drop
wakeups, reminder files, debugging, and cleanup.

For ordinary short, deterministic one-off shell commands, use the tool schema
synchronously. For anything involving time, recurring work, external schedulers,
a silent scheduled job, or a **long-running agent/coding CLI** (see the resident
rule below), start here.

## Nested reference catalog

`bash-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: bash-scheduled-work
  location: reference/scheduled-work/SKILL.md
  description: |
    Cron-driven scheduled work: when to use host schedulers, the LingTai
    wake-by-mailbox-drop contract, prompt boundaries, script hygiene, macOS
    launchd, Linux systemd timers, crontab fallback, and the launchd
    process-tree reaping gotcha.
- name: bash-notification-reminders
  location: reference/notification-reminders/SKILL.md
  description: |
    One-shot wakeup reminders via `.notification/cron.json`: payload shape,
    atomic writer, shell example, and the rest checklist for agents leaving work
    pending.
- name: bash-debugging-cleanup
  location: reference/debugging-cleanup/SKILL.md
  description: |
    Debugging and cleanup for scheduled jobs: scheduler fired, script ran, work
    landed, agent saw mail, worked launchd diagnosis, retiring cron jobs, and
    bash work footprint hygiene.
- name: bash-claude-code
  location: reference/bash-claude-code/SKILL.md
  description: |
    Claude Code CLI as a long-running bash subprocess: explicit model selection,
    async/poll discipline, allowed tools, JSON output, and stuck-run recovery.
- name: bash-openai-codex
  location: reference/bash-openai-codex/SKILL.md
  description: |
    OpenAI Codex CLI (`codex exec`) subprocess usage: sandbox/approval flags,
    model selection, async handling, and automation caveats.
- name: bash-opencode
  location: reference/bash-opencode/SKILL.md
  description: |
    OpenCode CLI (`opencode run` / `opencode serve`) subprocess usage, provider
    configuration, JSON output, session caveats, and daemon-harness notes.
- name: bash-cursor-agent
  location: reference/bash-cursor-agent/SKILL.md
  description: |
    Cursor Agent CLI subprocess usage and daemon-harness checks.
- name: bash-mimocode
  location: reference/bash-mimocode/SKILL.md
  description: |
    MiMo Code CLI subprocess usage; provider discovery stays with swiss-knife,
    while shell execution hygiene lives here.
- name: bash-qwen-code
  location: reference/bash-qwen-code/SKILL.md
  description: |
    Qwen Code CLI subprocess usage and daemon-harness checks.
- name: bash-oh-my-pi
  location: reference/bash-oh-my-pi/SKILL.md
  description: |
    Oh-My-Pi / Pi Coding Agent (`omp`) subprocess usage, JSON mode, approval
    mode, and session-resume caveats.
- name: bash-gemini-cli
  location: reference/bash-gemini-cli/SKILL.md
  description: |
    Gemini CLI as a candidate coding harness: non-interactive prompt mode,
    approval flags, resume questions, and promotion checklist.
- name: bash-aider
  location: reference/bash-aider/SKILL.md
  description: |
    Aider as a scriptable coding harness: `--message` mode, git behavior,
    one-shot automation, and daemon suitability caveats.
- name: bash-goose
  location: reference/bash-goose/SKILL.md
  description: |
    Goose CLI as a candidate coding harness: session/no-session modes and
    daemon promotion checklist.
- name: bash-openhands
  location: reference/bash-openhands/SKILL.md
  description: |
    OpenHands CLI headless mode as a candidate harness: `--task`/`--file`, JSONL,
    dependency footprint, and daemon promotion checklist.
- name: bash-crush
  location: reference/bash-crush/SKILL.md
  description: |
    Charm Crush CLI as a candidate harness: `crush run`, permission/session
    questions, and daemon promotion checklist.
- name: bash-zed-acp
  location: reference/bash-zed-acp/SKILL.md
  description: |
    Zed/ACP external-agent bridge notes: ecosystem integration, not a direct
    daemon backend unless a headless ACP client command is available.
```

## Router table

| Need / keywords | Read |
|---|---|
| Running a long-running agent/coding CLI as a sub-process: `claude -p`, `codex exec`, `opencode run`, Cursor Agent, MiMo Code, Qwen Code, Oh-My-Pi, Gemini CLI, Aider, Goose, OpenHands, Crush; "run an agent in the background"; avoid blocking the turn | `reference/bash-claude-code/SKILL.md`, `reference/bash-openai-codex/SKILL.md`, `reference/bash-opencode/SKILL.md`, or the matching `reference/bash-*/SKILL.md`; keep the core async/poll rules below resident |
| Human asks for time-driven recurring work: "every hour", "daily", "weekdays at 9", "write/check/send on a schedule"; choose cron vs event watcher; create launchd/systemd/crontab wiring; understand wake-by-mailbox-drop; write scheduler prompt/script hygiene | `reference/scheduled-work/SKILL.md` |
| Need a one-shot reminder or wakeup nudge while work is pending; `.notification/cron.json`; atomic reminder writer; rest checklist | `reference/notification-reminders/SKILL.md` |
| Scheduled job is silent, fires twice, exits immediately, gets killed by launchd, fails to deliver mail, or must be retired/cleaned up | `reference/debugging-cleanup/SKILL.md` |

## Quick decision tree

1. **Short deterministic host work** (finishes in seconds: `ls`, `git status`,
   `grep`, a quick build)? Use `bash` synchronously; this manual is not needed
   unless the command is risky, scheduled, or failing mysteriously.
2. **Long-running agent/coding CLI** (`claude -p`, `codex exec`, `opencode run`,
   Cursor Agent, MiMo Code, Qwen Code, Oh-My-Pi, Gemini CLI, Aider, Goose,
   OpenHands, Crush, or any sub-agent that may think/run tools for minutes)?
   **Never run it synchronously.** Use `bash(async=true)` and poll — see the
   resident rule below.
3. **Time itself is the trigger?** Read `reference/scheduled-work/SKILL.md`.
4. **You only need a single future nudge?** Read
   `reference/notification-reminders/SKILL.md`.
5. **A scheduled job already exists and is misbehaving?** Read
   `reference/debugging-cleanup/SKILL.md` before editing blindly.

## Reading command results — never trust top-level `status` alone

The top-level `status` of a `bash` result (`ok`/`done`) means only that the
**shell spawned** the command — **not** that the command succeeded. A failed
build, a missing file, a Python traceback, or a missing import all come back
under `status: "ok"`. Proceeding on that false success is the single most
common way agents corrupt their own downstream work.

- **Check `exit_code` / `ok` on every command whose success matters.** The
  result carries `ok` (bool) and `command_status` (`"success"`/`"failed"`)
  keyed off the exit code. `exit_code != 0` means the command failed even
  though `status` says `ok`.
- **Read the `warning` field when present.** On failure **or** a suspicious
  zero-exit (a traceback/missing-module signature in the output despite exit
  code 0) the result includes a one-line `warning` naming the nonzero exit, any
  detected Python traceback or missing module, and a stderr tail. The tail is
  run through the kernel's secret redactor, so secret-shaped lines are masked in
  `warning`; the raw `stderr` field is unchanged. If `warning` is present, stop
  and read it before acting on the output.
- **Use the venv interpreter for project code.** Bare `python3` lacks
  third-party packages and LingTai's own modules (`lingtai`, `lingtai_kernel`).
  A `No module named …` / `missing_module` warning usually means you ran the
  wrong interpreter — invoke the project's virtualenv `python`, not the system
  one.

## Avoid broad recursive scans

Unbounded recursive walks over large roots (`work/projects/.lingtai`) are the
top cause of `bash` timeouts. On a timeout the tool appends an `rg` recipe when
it detects this shape, but prefer it from the start:

- Replace `find <root> -name …`, `Path(...).rglob(...)`, `os.walk(...)`, and
  `glob('**/…')` over big trees with:
  `rg --files --hidden -g '!**/{.git,node_modules,daemons,.worktrees}/**' <root>`
  then filter the file list — `rg` honors `.gitignore` and skips the expensive
  directories by default.
- Narrow the root, add `-maxdepth`, or raise `timeout` only when the tree is
  genuinely large and you have a reason to walk all of it.

## Parse JSONL line-by-line

Event/log files (`events.jsonl`, daemon logs) are **JSON Lines** — one JSON
object per line, **not** a single JSON document. `json.loads(whole_file)` fails
on them. Iterate lines and `json.loads` each non-empty line, or pipe through
`jq -c .` / `rg` to filter before parsing. Tail with `tail -n` instead of
reading the whole file when you only need recent events.

## Core rules to keep resident

- **Synchronous `bash` is only for short, deterministic commands.** A long-running
  agent/coding CLI session — `claude -p`, `codex exec`, `opencode run`, the Cursor
  agent CLI, or any sub-agent that may think and run tools for minutes — must
  **never** be a synchronous `bash` call. Run it with `bash(async=true)` and poll
  the returned `job_id`. A synchronous call blocks the whole turn until the child
  exits: you stay `ACTIVE` and stop seeing channel notifications (mail, refresh,
  interrupts) for the entire duration. Async + poll keeps you responsive and
  prevents ACTIVE blockage while the child CLI works.

  ```text
  # Start the child agent in the background — returns immediately with a job_id:
  bash(async=true, command="claude -p 'refactor the auth module' --output-format json")
  # → {"status": "ok", "job_id": "ab12…", "pid": 4321}

  # Later turns: poll until done (handle mail/other work between polls):
  bash(action="poll", job_id="ab12…")
  # → {"status": "running", …}   then eventually
  # → {"status": "done", "exit_code": 0, "ok": true, "command_status": "success", "stdout": "…", "stderr": "…"}
  #   On failure: {"status": "done", "exit_code": 1, "ok": false,
  #                "command_status": "failed", "warning": "command exited with code 1; …"}

  # Abandon it if needed:
  bash(action="cancel", job_id="ab12…")
  ```

- **If repeated-call `_advisory` appears on `bash(action="poll")`, stop
  tight polling.** The poll already executed; the advisory is not a block. If
  the job is still running and nothing meaningful changed, handle any human
  messages, do other work, or set one future reminder (`bash` notification
  reminder or internal delayed self-email) and yield/idle. Poll again only when
  a completion notification arrives, the reminder fires, or you have a concrete
  reason to expect new state.

- **Idle care: never hand a launched async job entirely to its completion
  notification.** Once you start a long-running agent/coding CLI with
  `bash(async=true)` (or any child sub-process), do not go fully IDLE relying
  only on the completion/IDLE signal. Before resting, arm **at least one**
  self-wake (a `.notification/cron.json` reminder or an internal delayed
  self-email). Pick the delay from the task's *expected* duration — not a fixed
  number; a 30 s scan and a 40 min build warrant different windows. When the
  wake fires, health-check rather than assume progress: confirm the log is
  **growing**, the PID/child is **alive**, the output file/worktree shows
  **progress**, and the job is not stuck on an interactive prompt or a
  provider/model error. If there is no progress, do not keep waiting — cancel,
  downgrade, or switch path, and report to the human. A job that exits
  immediately or sits silent past its expected window is the failure mode this
  rule exists to catch.

- LingTai has no built-in recurring scheduler. Host schedulers wake agents by
  producing channel input, usually a mailbox-drop or notification file.
- Prefer event watchers/webhooks when an external event is the real trigger;
  prefer cron/launchd/systemd only when time is the trigger or polling is truly
  the right tradeoff.
- Scheduler scripts must be idempotent, audited, logged, absolute-path based,
  and explicit about how they wake the agent.
- On macOS, remember launchd process-tree reaping; use the documented
  double-fork pattern when a child process must outlive the launchd job.
- Do not leave silent janitors or hidden recurring jobs behind. Document and
  clean them up when the human no longer needs them.

## Maintenance

Keep this top-level router short. Add detailed examples, platform recipes, and
troubleshooting trees to nested references so agents can load only the section
needed for the current task.
