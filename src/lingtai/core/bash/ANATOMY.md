# core/bash

Bash capability — shell command execution with file-based policy. Adds the
ability to run shell commands. This is a capability (not intrinsic) because
not every agent should have shell access — it's a powerful ability that should
be explicitly opted into.

## Components

- `bash/__init__.py` — the entire capability in a single file. `get_description` (`bash/__init__.py:32-33`), `get_schema` (`bash/__init__.py:36-70`), `resolve_policy` / `make_manager` / `make_handler` / `setup`. Two core classes: `BashPolicy` (`bash/__init__.py:74-162`) for command filtering, `BashManager` (`bash/__init__.py:165-503`) for execution. `make_manager(agent, policy_file, yolo)` is the stable single-source-of-truth seam: it resolves the policy (`resolve_policy`) and builds the `BashManager`; `setup()` and `make_handler(agent)` both go through it, and the SDK bundle bridge `lingtai.core.bash_bundle` hosts the *same* factory's handler, so the bundle-hosted `bash` tool cannot drift from the registered one.
- `bash/bash_policy.json` — default denylist policy shipped with the kernel. Denies destructive (`rm`, `rmdir`, `shred`, `dd`), privilege escalation (`sudo`, `su`, `doas`), permission changes (`chmod`, `chown`, `chgrp`), disk management (`mount`, `umount`, `mkfs`, `fdisk`), package managers (`apt`, `apt-get`, `yum`, `dnf`, `brew`), process control (`kill`, `killall`, `pkill`, `shutdown`, `reboot`, `systemctl`), network (`nc`, `ncat`), and code execution (`eval`, `exec`).

## Public API

The `bash` tool supports synchronous and asynchronous execution:

| Parameter      | Type     | Description |
|----------------|----------|-------------|
| `command`      | string   | Shell command to execute (required for `run`) |
| `timeout`      | number   | Timeout in seconds (default: 30, sync only) |
| `working_dir`  | string   | Working directory for execution (default: agent's working dir) |
| `action`       | string   | `run` (default), `poll`, or `cancel` |
| `async`        | boolean  | If true, run in background and return job_id immediately (default: false) |
| `job_id`       | string   | Job ID for `poll` and `cancel` actions |

**Sync mode** (`async=false`, default): Returns `{status, exit_code, stdout, stderr}` on success, or `{status: "error", message}` on failure. Identical to pre-async behavior.

**Async mode** (`async=true`): Returns `{status: "ok", job_id, pid, message}` immediately. Use `action="poll"` with the job_id to check status: returns `{status: "running", job_id, pid}` or `{status: "done", exit_code, stdout, stderr}`. Use `action="cancel"` to kill the process group.

Job files are stored under `system/jobs/{job_id}/` (stdout.log, stderr.log, pid, status). Cleaned up automatically on poll-completion or cancel.

## Internal Module Layout

```
bash/__init__.py
  ├── BashPolicy                     — command execution policy
  │   ├── __init__(allow, deny)      — two modes: allowlist (if allow present) or denylist
  │   ├── from_file(path)            — loads policy from JSON file
  │   ├── yolo()                     — creates a policy that allows everything
  │   ├── describe()                 — human-readable summary of policy rules
  │   ├── is_allowed(command)        — checks command against policy
  │   ├── _check_single(cmd)         — checks a single command name
  │   └── _extract_commands(command) — parses pipes, chains, subshells to extract all command names
  │
  ├── BashManager                    — execution manager
  │   ├── __init__(policy, working_dir, max_output) — stores policy + config
  │   ├── handle(args)               — dispatches to _handle_run / _handle_poll / _handle_cancel
  │   ├── _handle_run(args)          — validates + runs sync or async
  │   ├── _run_sync(command, cwd, timeout) — original subprocess.run path
  │   ├── _run_async(command, cwd)   — subprocess.Popen with start_new_session, returns job_id
  │   ├── _handle_poll(args)         — checks job status via Popen.poll() or os.waitpid
  │   ├── _handle_cancel(args)       — SIGTERM to process group, cleanup
  │   └── _close_handles(job_id)     — closes open file handles for a job
  │
  ├── resolve_policy(policy_file, yolo) — yolo / explicit file / bundled default → BashPolicy
  ├── make_manager(agent, policy_file, yolo) — single source of truth: resolve_policy + build BashManager
  ├── make_handler(agent, policy_file, yolo) — make_manager(...).handle (the SDK bridge's seam)
  └── setup(agent, policy_file, yolo) — make_manager, then registers bash tool with policy-aware description
```

## Key Invariants

- **Two policy modes:** Allowlist mode (when `allow` key is present in policy) — only listed commands permitted, everything else blocked. Denylist mode (only `deny` key) — everything allowed except denied commands. The mode is implicit.
- **Pipe-aware command extraction:** `_extract_commands()` parses `|`, `&&`, `||`, `;`, newlines, `$()`, backticks, and env-var prefixes to find every command name in a compound expression.
- **Working directory sandbox:** `working_dir` is validated to be under the agent's working directory. Paths are resolved and checked with `startswith(sandbox + "/")`.
- **Output truncation:** `max_output = 50_000` chars. Both stdout and stderr are truncated with a note showing total length.
- **Subprocess isolation:** Commands run via `subprocess.run(shell=True, capture_output=True, text=True, timeout=...)` in the agent's working directory by default.
- **Async subprocess:** Async commands use `subprocess.Popen(shell=True, start_new_session=True)` with stdout/stderr redirected to files under `system/jobs/{job_id}/`. `start_new_session=True` ensures the process gets its own session, enabling `os.killpg()` for clean cancellation.
- **Job lifecycle:** Jobs are created on async run, tracked via PID files, and cleaned up (directory deleted) after poll-completion or cancel. File handles are closed via `_close_handles()` to avoid resource leaks.
- **Policy file location:** Default policy is `bash/bash_policy.json` (shipped with the kernel). Can be overridden via `policy_file` arg or bypassed with `yolo=True`. `resolve_policy()` is the single resolution path shared by `setup()`, `make_manager()`, and the SDK bridge.
- **SDK bundle bridge (stage 3H):** `lingtai.core.bash_bundle` (the wrapper-side bridge) injects `make_handler(agent)` into the `lingtai_sdk.bash_tools` shell-execution bundle host. The SDK declares `bash` as a non-privileged, in-process bundle with a per-action risk table (`run`/`cancel` → DESTRUCTIVE, `poll` → CAUTION; bundle posture DESTRUCTIVE). Additive only — `setup()` remains the live registration path; the bridge installs no guard and changes no dispatch.

## Dependencies

- `lingtai.i18n` — `t()` for localized strings
- `lingtai_kernel.base_agent.BaseAgent` — agent type (TYPE_CHECKING only)

## Composition

- **Parent:** `src/lingtai/core/` (capability package).
- **Siblings:** `daemon/`, `avatar/`, `mcp/`, `knowledge/` (private durable memory), `skills/` (skill catalog).
- **Manual:** `bash/manual/SKILL.md` — operational guide for agents (currently focused on scheduled / cron-driven work — when to schedule, the wake-by-mailbox-drop contract, hygiene rules, OS-specific recipes for launchd / systemd / crontab, and debugging walkthroughs).
- **Kernel hooks:** `setup()` is called during capability initialization; `BashManager.handle()` is registered as the `bash` tool handler.
