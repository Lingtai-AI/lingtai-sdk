# core/daemon

Daemon capability (分神) — dispatch ephemeral subagents (分神) that operate
in parallel on the agent's working directory. Each emanation is a disposable
`ChatSession` with a curated tool surface, not an agent. Results are
persisted in per-run daemon folders; terminal completion/failure is surfaced
as a compact `.notification/system.json` event instead of ordinary parent
request text.

## Components

- `daemon/__init__.py` — public capability surface. `get_description`, `get_schema`, and `setup`; the core class is `DaemonManager`, which manages the full emanation lifecycle. Key internals: `_ToolCollector` (`daemon/__init__.py:37-64`) intercepts `add_tool` calls during preset-driven capability setup to build a sandboxed tool surface without mutating the parent's registry. `EMANATION_BLACKLIST` (`daemon/__init__.py:34`) prevents recursion by blocking `daemon`, `avatar`, `psyche`, `skills`, and deprecated `codex` tools in subagents.
- `daemon/run_dir.py` — per-emanation filesystem run directory. `DaemonRunDir` owns every filesystem effect for one run: folder layout, `daemon.json` atomic writes, JSONL appends, CLI progress events, heartbeat touches, `result.txt`, and terminal state markers. The `DaemonManager` calls into a `DaemonRunDir` at every lifecycle hook without itself touching the filesystem.

## Public API

The `daemon` tool exposes five actions:

| Action     | Description |
|------------|-------------|
| `emanate`  | Spawn one or more subagents with specified task + tools + optional preset |
| `list`     | List running/completed/failed emanations with status and elapsed time |
| `ask`      | Send a follow-up message to a running emanation |
| `check`    | Read-only progress tail: `daemon.json` state + last N events from `events.jsonl` |
| `reclaim`  | Cancel all running emanations, shut down thread pools, reset ID counter |

## Internal Module Layout

```
daemon/__init__.py
  ├── DaemonManager.__init__        — stores agent ref, config ceilings, emanation registry
  ├── handle()                      — top-level dispatcher (emanate/list/ask/check/reclaim)
  ├── _build_tool_surface()         — filters requested tools against blacklist, expands groups
  ├── _instantiate_preset_capabilities() — sets up preset tool surface in a sandbox
  ├── _build_emanation_prompt()     — composes the subagent's system prompt
  ├── _run_emanation()              — lingtai-backend worker tool loop (send → tool_calls → results)
  ├── _run_claude_code_emanation()  — claude-code backend; parses `--output-format stream-json --verbose` events in real time so `claude_session_id`, per-turn text, and tool_use/tool_result land in DaemonRunDir during the run (vs. post-hoc). Claude Code's own `usage` fields are deliberately NOT forwarded to append_tokens (external billing path; semantics don't match the kernel's adapter accounting).
  ├── _run_codex_emanation()        — codex backend; parses `codex exec --json` JSONL events (thread.started → codex_session_id, item.completed → agent_message text, turn.completed → terminal). Symmetric with the claude-code backend. Codex tokens are also NOT forwarded to append_tokens for the same reason.
  ├── _run_opencode_emanation()     — opencode backend; spawns `opencode run --format json <prompt>` and parses one JSON event per stdout line via defensive helpers (`_opencode_extract_session_id`, `_opencode_extract_text`) because opencode's event field naming is less standardized than claude-code or codex. Session id is stored as `opencode_session_id` in daemon.json on the first event that carries one; terminal-shaped events (`*.completed`, `*.done`, `*.finished`, `result`, `final`) override intermediate streaming text. Non-JSON lines are still recorded as cli_output. `_build_opencode_prompt` wraps the user task with the daemon operating contract (write detailed work product to files; end with a concise final answer).
  ├── _find_claude_session_id()     — legacy `~/.claude/projects/` JSONL scan; now only a fallback when the stream-json `session_id` capture fails
  ├── _handle_emanate()             — validates presets, creates DaemonRunDir, submits to pool
  ├── _handle_list/check/reclaim    — individual action handlers
  ├── _handle_ask()                 — dispatcher: routes claude-code, codex, and opencode asks to their stream-json followup handlers; routes lingtai asks to the in-process followup buffer
  ├── _handle_ask_cli()             — claude-code follow-up via `claude --resume <claude_session_id>`. Spawns the subprocess, hands the stream-json parse to `_ask_pool`, returns `{"status":"sent","async":true}` immediately so the parent's tool turn isn't held for the duration of the follow-up
  ├── _run_ask_claude_code_stream() — background worker for the claude-code ask. Same stream-json parse as `_run_claude_code_emanation`; clears `ask_in_flight` on exit
  ├── _handle_ask_codex()           — codex follow-up via `codex exec resume <codex_session_id> --json`. Symmetric with `_handle_ask_cli`: spawns + dispatches to `_ask_pool`, returns immediately
  ├── _run_ask_codex_stream()       — background worker for the codex ask. Same JSONL parse as `_run_codex_emanation`; `daemon(check)` therefore sees progress on follow-ups too
  ├── _handle_ask_opencode()        — opencode follow-up via `opencode run --session <opencode_session_id> --format json <message>`. Symmetric with claude-code / codex ask: spawns, dispatches to `_ask_pool`, returns immediately. Returns a clear error if no `opencode_session_id` has been captured yet.
  ├── _run_ask_opencode_stream()    — background worker for the opencode ask. Same defensive JSON-line parse as `_run_opencode_emanation`; clears `ask_in_flight` on exit; terminal-shaped events override intermediate text
  ├── _watchdog()                   — timeout enforcement thread
  ├── _publish_daemon_notification() — publishes compact system notifications
  └── _drain_followup()             — drains per-emanation follow-up buffer (lingtai backend only)

daemon/run_dir.py
  ├── DaemonRunDir.__init__         — creates folder on disk, writes daemon.json + .prompt
  ├── Path properties               — run_id, path, daemon_json_path, prompt_path, heartbeat_path, chat_path, events_path, token_ledger_path, result_path
  ├── record_user_send()            — appends user-role entry to chat_history.jsonl
  ├── bump_turn()                   — marks end of LLM round (daemon.json + chat_history + heartbeat)
  ├── set_current_tool()            — marks tool dispatch starting (daemon.json + events + heartbeat)
  ├── clear_current_tool()          — marks tool dispatch finished
  ├── record_cli_output()           — records CLI backend stdout/stderr as cli_output events
  ├── append_tokens()               — dual-ledger token accounting (daemon's + parent's)
  ├── mark_done/failed/cancelled/timeout — terminal state markers (result.txt + preview on done)
  └── _atomic_write_json()          — tempfile + os.replace for crash-safe writes
```

## Key Invariants

- **No recursion:** `EMANATION_BLACKLIST` prevents emanations from spawning sub-emanations, avatars, psyche, the skill catalog, or deprecated codex.
- **Tool surface isolation:** `_ToolCollector` ensures preset-driven capability setup does not mutate the parent agent's tool registry.
- **Filesystem isolation:** Each emanation gets its own `daemons/em-<N>-<YYYYMMDD-HHMMSS>-<hash6>/` directory. `DaemonRunDir` uses atomic `os.replace` for `daemon.json` and single-writer append-only JSONL for events/chat history.
- **Timeout vs. cancel distinction:** Separate `timeout_event` and `cancel_event` allow the run loop to call `mark_timeout()` vs. `mark_cancelled()` based on which signal fired first.
- **Capacity control:** `max_emanations` caps concurrent subagents; completed futures are pruned before each new batch.
- **Preset validation is pre-flight:** Preset connectivity and capability instantiation are checked before any emanation is scheduled. A single failure refuses the whole batch.
- **Dual token ledger (lingtai backend only):** For lingtai-backend emanations, token usage is written to both the daemon's own ledger and the parent's ledger with `source=daemon` attribution. **CLI backends (claude-code, codex) deliberately do NOT write to either ledger** — they run as external processes with their own billing paths, and their cache-creation/cache-read semantics do not map cleanly onto the kernel's adapter accounting. Mixing them in would produce a misleading "lifetime totals" number. CLI-backend spend is visible to the agent through `daemon(check)` output (`last_output`, `cli_output` events, stderr), not through `sum_token_ledger`.
- **CLI progress stays inspectable, not conversational:** Claude Code/Codex stdout is persisted as `cli_output` events plus `daemon.json.last_output`; completion/failure publishes a bounded `system` notification pointing the parent to `daemon(action="check", id=...)`.
- **Full results live on disk:** `mark_done()` writes complete terminal output to `result.txt`; `daemon.json.result_preview` and notification bodies stay bounded.
- **Claude Code spawns get a sanitized env.** Both `_run_claude_code_emanation` and `_handle_ask_cli` build the subprocess env via `_claude_code_env()`, which copies `os.environ` and pops `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`. LingTai loads `.env` from `~/.lingtai-tui/` early in startup, so an API key intended for the lingtai LLM adapter would otherwise leak into spawned `claude` processes and force them off the user's Claude Code subscription onto API billing — surfacing as `Credit balance is too low` even when the subscription is healthy. Stripped vars are logged once per spawn via `daemon_claude_code_env_stripped`. Codex spawns are unaffected (they use OpenAI creds). See GH #107.
- **`backend_options` is a CLI-backend-only argv passthrough.** Per-task `backend_options` (JSON object) is converted to argv tokens by `_backend_options_to_argv` (`daemon/__init__.py`) and appended to the CLI command before the task prompt by `_run_claude_code_emanation`, `_run_codex_emanation`, and `_run_opencode_emanation`. Validation happens pre-flight in `_handle_emanate_cli` — a single bad spec refuses the whole batch with a clear `ValueError`. The resolved object + argv are persisted to `daemon.json` (`backend_options`, `backend_argv`) and logged as `daemon_backend_options`. The lingtai backend silently ignores the field. `daemon(action="ask")` does not re-pass options — `--resume` / `exec resume` / `--session` reuses the session as-is.
- **Ask workers cannot block on silent stdout.** Both `_run_ask_claude_code_stream` and `_run_ask_codex_stream` read stdout via `_iter_stdout_with_deadline` (module-level helper), which moves the blocking `for line in proc.stdout` onto a daemon reader thread and has the worker pull from a `queue.Queue` with `queue.get(timeout=...)`. The deadline is therefore enforced even when the resumed CLI subprocess writes nothing and never exits — without this, an unresponsive `claude --resume` / `codex exec resume` would strand `ask_in_flight`, leave a proc in `_cli_procs`, and consume an `_ask_pool` slot until manual reclaim. The reader thread is `daemon=True` so a deadline kill leaves it to exit naturally as the pipe closes. **The initial emanation runners (`_run_claude_code_emanation`, `_run_codex_emanation`) use the same blocking-iterator pattern, but they are covered by `_watchdog` which directly kills `_cli_procs` when the per-batch timer expires; widening the queue-based reader there is deferred to keep this PR scoped to the ask path.**
- **CLI-backend `ask` is non-blocking; lingtai-backend `ask` is in-process.** `_handle_ask_cli` / `_handle_ask_codex` spawn the resumed CLI subprocess on the calling thread (so subprocess-launch errors like missing CLI surface synchronously) but hand the stream-json/JSONL parse loop to a dedicated `ThreadPoolExecutor` (`_ask_pool`, sized to `max_emanations`). The agent's `daemon(action="ask")` call returns `{"status":"sent","async":true}` within milliseconds; progress lands as `cli_output` events + `last_output` in the run_dir, and the final reply (or failure) is announced via `_publish_daemon_notification("follow-up completed"/"follow-up failed")`. The lingtai-backend path is unchanged — it buffers into the emanation's `followup_buffer` and is drained by the in-process run loop. A per-entry `ask_in_flight` flag (guarded by `followup_lock`) refuses a second concurrent ask with `{"status":"busy", ...}` because `claude --resume` / `codex exec resume` serialize per session and a second spawn would either error or interleave reply text. `_handle_reclaim` shuts down `_ask_pool` alongside the regular emanation pools and rebuilds a fresh one. This fixes the regression where a single `daemon(ask)` could hold the parent agent's tool turn for up to `self._timeout` seconds (default 3600).
- **CLI backends stream structured events, not buffered text.** Both `_run_claude_code_emanation` / `_handle_ask_cli` (using `claude --output-format stream-json --verbose`) and `_run_codex_emanation` / `_handle_ask_codex` (using `codex exec --json`) parse one JSON event per stdout line as it arrives. The first event that carries a session id writes it to `daemon.json` (`claude_session_id` or `codex_session_id`) immediately — typically within ms of process start, well before any LLM work — so `daemon(action="ask")` is usable from the moment `emanate` returns rather than after the initial task completes. stderr drains in a background thread to its own pipe (no longer merged into stdout), so API/auth/rate-limit errors surface as `cli_output` events with `stream="stderr"`. For claude-code: a final `result` event with `is_error=true` is surfaced as `mark_failed`, so an error inside the LLM stream doesn't masquerade as success even when the underlying process exits 0. For codex: absence of a `turn.completed` event (combined with no captured `agent_message`s) is treated as failure similarly. Codex's `--ephemeral` flag is intentionally NOT passed: it would disable session persistence and break `daemon(ask)`. See GH issues #99 / #100 / #101 for the prior buffered-text failure mode that motivated this design.

## Dependencies

- `lingtai_kernel.llm.base.FunctionSchema` — tool schema type
- `BaseAgent._enqueue_system_notification` — compact daemon completion/failure events
- `lingtai_kernel.token_ledger` — `append_token_entry` for token accounting
- `lingtai.i18n` — `t()` for localized strings
- `lingtai.capabilities` — `setup_capability`, `_GROUPS` for preset sandbox instantiation
- `lingtai.presets` — `load_preset`, `expand_inherit` for per-emanation preset resolution
- `lingtai_kernel.preset_connectivity` — `check_connectivity` for LLM reachability pre-flight
- `lingtai_kernel.config_resolve` — `resolve_env` for API key resolution
- `lingtai.llm.service` — `LLMService` for dedicated preset LLM services
- `lingtai.agent.Agent` — parent agent type (TYPE_CHECKING only)

## Composition

- **Parent:** `src/lingtai/core/` (capability package).
- **Siblings:** `avatar/`, `mcp/`, `knowledge/` (private durable memory), `skills/` (skill catalog), `bash/`.
- **Manual:** `daemon/manual/SKILL.md` — skill documentation for the LLM.
- **Kernel hooks:** `setup()` is called during capability initialization; `DaemonManager.handle()` is registered as the `daemon` tool handler.
