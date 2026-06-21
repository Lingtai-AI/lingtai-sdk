"""Daemon capability (神識) — dispatch ephemeral subagents (分神).

Gives an agent the ability to split its consciousness into focused worker
fragments that operate in parallel on the same working directory.  Each
emanation is a disposable ChatSession with a curated tool surface — not an
agent.  Results are persisted in daemon run directories; every terminal outcome
(done / failed / cancelled / timeout) is surfaced exactly once via a compact
system notification, so the parent can dispatch and go idle without polling.

Usage:
    Agent(capabilities=["daemon"])
    Agent(capabilities={"daemon": {"max_emanations": 100}})
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import yaml
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ...i18n import t

if TYPE_CHECKING:
    from ...agent import Agent

from lingtai_kernel.llm.base import FunctionSchema
from lingtai_kernel.loop_guard import LoopGuard
from lingtai_kernel.meta_block import build_meta
from lingtai_kernel.tool_executor import ToolExecutor
from .run_dir import DaemonRunDir
from .claude_interactive import ClaudeInteractiveError, run_claude_interactive

PROVIDERS = {"providers": [], "default": "builtin"}

# Default and author ceiling for per-emanation LLM tool-loop turns.
# Agents may request a smaller per-batch value via daemon(max_turns=...), but
# larger values are capped here.
DEFAULT_MAX_TURNS = 1000
_DAEMON_SKILL_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate the entire process group for *proc*, then force-kill if needed.

    Requires *proc* to have been started with ``start_new_session=True`` so
    that its PGID equals its own PID.  Sends SIGTERM to the group, waits up
    to 5 seconds, then escalates to SIGKILL for any survivors.

    Uses ``proc.pid`` directly as the PGID (since ``start_new_session=True``
    guarantees PGID == PID) to avoid a ``getpgid`` round-trip that could
    race with PID recycling.

    Silently ignores ``ProcessLookupError`` (process already dead) and
    ``OSError`` (permission denied on already-dead group).
    """
    # start_new_session=True guarantees pgid == pid
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

# Sentinel placed on the stdout-reader queue when the background reader
# thread observes EOF on the subprocess pipe. The consumer treats this as
# "no more lines will ever arrive — stop draining."
_STDOUT_EOF = object()


def _iter_stdout_with_deadline(
    proc: subprocess.Popen,
    deadline: float,
    thread_name: str,
):
    """Yield stdout lines from *proc* until EOF, deadline, or process exit.

    The fundamental problem this solves: ``for line in proc.stdout`` blocks
    the caller's thread until the subprocess writes a newline. If the
    resumed CLI hangs without producing output, the caller can never
    observe the deadline. We work around it by pushing the blocking
    read onto a small daemon thread that drops each line into a queue,
    while the caller pulls from the queue with ``timeout=remaining``.

    Yields raw lines (with trailing ``\\n`` preserved, matching the
    original iterator semantics). Stops iterating when:
      - the reader thread reports EOF (sentinel arrives), OR
      - ``time.monotonic() >= deadline`` (caller is expected to
        ``_kill_process_group`` after handling timeout — we do NOT do
        it here so the worker can record timeout state first).

    The reader thread is a daemon thread (won't block process exit) and
    is left orphaned if the deadline fires — it will exit naturally once
    the subprocess is killed and its pipe closes.
    """
    q: "queue.Queue[object]" = queue.Queue(maxsize=1024)

    def _reader():
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                q.put(raw_line)
        except (ValueError, OSError):
            # Pipe closed mid-read (e.g. after _kill_process_group). Treat
            # as EOF — the consumer either already noticed the timeout or
            # is about to.
            pass
        finally:
            q.put(_STDOUT_EOF)

    reader = threading.Thread(target=_reader, daemon=True, name=thread_name)
    reader.start()

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return  # caller handles timeout (kill + mark)
        try:
            item = q.get(timeout=min(remaining, 0.5))
        except queue.Empty:
            continue  # re-check deadline
        if item is _STDOUT_EOF:
            return
        yield item


# Tools emanations can never use (no recursion, no spawning, no identity mutation)
EMANATION_BLACKLIST = {
    "daemon",
    "avatar",
    "avatar_spawn",
    "avatar_rules",
    "psyche",
    "skills",
    "knowledge",
}

# Env vars that override Claude Code's normal first-party OAuth credentials.
# LingTai loads ``.env`` from ``~/.lingtai-tui/`` early, so auth intended for
# another LLM adapter can leak into spawned ``claude`` subprocesses.
# ``ANTHROPIC_*`` keys force API billing (GH #107); a stale
# ``CLAUDE_CODE_OAUTH_TOKEN`` can also beat a refreshed
# ``~/.claude/.credentials.json`` and surface as a false weekly-limit error
# (GH Lingtai-AI/lingtai#189). Strip these for Claude Code subprocesses
# only: print-mode Claude (claude-p/claude-code) and interactive Claude
# (claude/claude-interactive). Other backends (codex, lingtai, opencode,
# mimocode, qwen-code, oh-my-pi, cursor) are unaffected.
_CLAUDE_CODE_STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def _claude_code_env() -> dict[str, str]:
    """Return os.environ minus auth vars that override Claude Code's OAuth."""
    env = os.environ.copy()
    for key in _CLAUDE_CODE_STRIP_ENV:
        env.pop(key, None)
    return env


def _normalize_claude_usage(usage: dict | None) -> dict | None:
    """Normalize a Claude Code stream-json ``usage`` block to UI totals.

    Claude Code's final ``result`` event carries a ``usage`` block like::

        {"input_tokens": 6950, "cache_creation_input_tokens": 3068,
         "cache_read_input_tokens": 15621, "output_tokens": 4, ...}

    Returns ``{"input", "output", "cached", "thinking"}`` with::

        cached = cache_read_input_tokens + cache_creation_input_tokens

    ``thinking`` is 0 — Claude Code does not surface a separate thinking-token
    count in this block. Returns ``None`` if ``usage`` is missing/not a dict or
    carries no countable tokens, so callers can skip persistence cleanly.
    """
    if not isinstance(usage, dict):
        return None

    def _int(value) -> int:
        return value if isinstance(value, int) else 0

    input_tokens = _int(usage.get("input_tokens"))
    output_tokens = _int(usage.get("output_tokens"))
    cached = (_int(usage.get("cache_read_input_tokens"))
              + _int(usage.get("cache_creation_input_tokens")))
    thinking = 0
    if not (input_tokens or output_tokens or cached or thinking):
        return None
    return {"input": input_tokens, "output": output_tokens,
            "cached": cached, "thinking": thinking}


# Safe CLI option key: letters/digits with '-' or '_' separators. No leading
# '-' (the helper adds '--' itself). No spaces, no shell metachars — argv is
# passed as a list to subprocess, but we still refuse anything that doesn't
# look like a real CLI flag to keep error messages early and obvious.
_BACKEND_OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _backend_options_to_argv(options: dict | None) -> list[str]:
    """Convert a free-form backend_options dict into a list of argv tokens.

    Conversion rules:
      - key must match ``[A-Za-z0-9][A-Za-z0-9_-]*`` (no leading '-', no
        empty). Underscores in the key are converted to dashes for the
        emitted flag. Long flags only: ``--<flag>``.
      - value ``True`` → ``["--flag"]`` (presence flag, no argument).
      - value ``False`` or ``None`` → omitted entirely.
      - value ``str`` / ``int`` / ``float`` → ``["--flag", str(value)]``.
      - value ``list``/``tuple`` of scalars → repeated
        ``["--flag", v1, "--flag", v2, ...]``.
      - Nested dicts / nested lists / objects of unsupported type → raise
        ``ValueError`` with a clear message.

    Returns argv tokens ready to be appended to a subprocess command list
    (never a shell string). Empty / falsy input returns ``[]``.
    """
    if not options:
        return []
    if not isinstance(options, dict):
        raise ValueError(
            f"backend_options must be a JSON object, got {type(options).__name__}"
        )

    argv: list[str] = []
    for key, value in options.items():
        if not isinstance(key, str) or not _BACKEND_OPTION_KEY_RE.match(key):
            raise ValueError(
                f"backend_options key {key!r} is not a safe CLI flag name "
                "(letters/digits with '-' or '_' separators, no leading '-')"
            )
        flag = "--" + key.replace("_", "-")

        if value is False or value is None:
            continue
        if value is True:
            argv.append(flag)
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            argv.extend([flag, str(value)])
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                    raise ValueError(
                        f"backend_options[{key!r}] list items must be string/int/float scalars "
                        f"(got {type(item).__name__})"
                    )
                argv.extend([flag, str(item)])
            continue
        raise ValueError(
            f"backend_options[{key!r}] has unsupported value type "
            f"{type(value).__name__}; expected bool/str/int/float/list of scalars/null"
        )
    return argv


_CLAUDE_COMMON_RESERVED_BACKEND_FLAGS = {
    "--settings",
    "--print",
    "--output-format",
}
_CLAUDE_INTERACTIVE_RESERVED_BACKEND_FLAGS = {
    "--append-system-prompt",
    "--append-system-prompt-file",
}

# OpenCode-family (opencode, mimocode) own the run output format so daemon
# event parsing keeps working; callers must not override it via backend_options.
_OPENCODE_FAMILY_RESERVED_BACKEND_FLAGS = {
    "--format",
}

# Qwen Code owns the prompt/headless/approval flags that drive LingTai's
# non-interactive harness; overriding them via backend_options would break
# headless capture or re-enable interactive prompting.
_QWEN_RESERVED_BACKEND_FLAGS = {
    "--prompt",
    "-p",
    "--yolo",
    "-y",
    "--approval-mode",
}

# Oh-My-Pi owns the mode/headless/approval/session flags that drive LingTai's
# non-interactive JSON harness; overriding them via backend_options would break
# JSON event capture, re-enable interactive prompting, or hijack the session.
# ``--print`` is reserved because it is Oh-My-Pi's alternate print-mode switch
# (short form ``-p`` cannot be emitted by backend_options, which only creates
# long ``--flag`` tokens).
_OH_MY_PI_RESERVED_BACKEND_FLAGS = {
    "--mode",
    "--print",
    "--auto-approve",
    "--yolo",
    "--approval-mode",
    "--session",
    "--resume",
    "--continue",
    "--no-session",
    "--session-dir",
}

# Backend name aliases → canonical backend id. Kept tiny on purpose: only the
# obvious short forms callers reach for.
_BACKEND_ALIASES = {
    "mimo": "mimocode",
    "qwen": "qwen-code",
    "omp": "oh-my-pi",
}


def _normalize_backend(backend: str | None) -> str:
    """Map a caller-supplied backend (incl. aliases) to its canonical id."""
    if not backend:
        return "lingtai"
    return _BACKEND_ALIASES.get(backend, backend)


def _validate_claude_backend_argv(backend: str, argv: list[str]) -> None:
    """Refuse user flags that would override a daemon backend's own harness.

    ``backend_options`` is a pass-through for CLI-specific flags, but several
    daemon backends own their execution mode and must not let callers override
    it (doing so would silently break daemon progress/result extraction):

      * Claude print-mode owns ``--print`` / ``--output-format stream-json``;
        interactive mode also owns ``--settings`` hooks + managed system prompt.
      * OpenCode-family (``opencode``, ``mimocode``) own ``--format`` (JSON).
      * Qwen Code owns ``--prompt`` / ``--yolo`` / ``--approval-mode``.
      * Oh-My-Pi owns ``--mode`` / approval-yolo / session flags.

    Despite the historical name, this validator now covers all CLI backends.
    """
    if backend in ("claude", "claude-interactive", "claude-p", "claude-code"):
        reserved = set(_CLAUDE_COMMON_RESERVED_BACKEND_FLAGS)
        if backend in ("claude", "claude-interactive"):
            reserved.update(_CLAUDE_INTERACTIVE_RESERVED_BACKEND_FLAGS)
    elif backend in ("opencode", "mimocode"):
        reserved = set(_OPENCODE_FAMILY_RESERVED_BACKEND_FLAGS)
    elif backend == "qwen-code":
        reserved = set(_QWEN_RESERVED_BACKEND_FLAGS)
    elif backend == "oh-my-pi":
        reserved = set(_OH_MY_PI_RESERVED_BACKEND_FLAGS)
    else:
        return
    for token in argv:
        if token in reserved:
            raise ValueError(f"{token} is reserved by the {backend} daemon backend")



class _ToolCollector:
    """Captures add_tool calls during preset-driven capability setup.

    A capability's setup() expects something with add_tool plus the rest of
    the parent's interface (_log, _config, _working_dir, inbox, ...). The
    collector intercepts add_tool into local dicts and forwards every other
    attribute read to the real parent agent, so the parent's tool registry
    stays untouched while we still get the schema + handler the capability
    wanted to register.
    """

    def __init__(self, parent):
        self._parent = parent
        self.schemas: dict = {}
        self.handlers: dict = {}

    def add_tool(self, name, *, schema=None, handler=None,
                 description: str = "", system_prompt: str = ""):
        if handler is not None:
            self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = FunctionSchema(
                name=name, description=description,
                parameters=schema, system_prompt=system_prompt,
            )

    def __getattr__(self, n):
        return getattr(self._parent, n)


def get_description(lang: str = "en") -> str:
    return t(lang, "daemon.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["emanate", "list", "ask", "check", "reclaim"],
                "description": t(lang, "daemon.action"),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string"},
                        "tools": {"type": "array", "items": {"type": "string"}},
                        "skills": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": t(lang, "daemon.tasks.skills"),
                        },
                        "mcp": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": t(lang, "daemon.tasks.mcp"),
                        },
                        "preset": {
                            "type": "string",
                            "description": t(lang, "daemon.tasks.preset"),
                        },
                        "backend_options": {
                            "type": "object",
                            "description": t(lang, "daemon.tasks.backend_options"),
                        },
                        "system_prompt": {
                            "type": "string",
                            "description": t(lang, "daemon.tasks.system_prompt"),
                        },
                    },
                    "required": ["task", "tools"],
                },
                "description": t(lang, "daemon.tasks"),
            },
            "id": {
                "type": "string",
                "description": t(lang, "daemon.id"),
            },
            "message": {
                "type": "string",
                "description": t(lang, "daemon.message"),
            },
            "last": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": t(lang, "daemon.last"),
            },
            "truncate": {
                "type": "integer",
                "minimum": 0,
                "description": t(lang, "daemon.truncate"),
            },
            "contains": {
                "type": "string",
                "description": t(lang, "daemon.contains"),
            },
            "status": {
                "type": "string",
                "description": t(lang, "daemon.status"),
            },
            "include_done": {
                "type": "boolean",
                "description": t(lang, "daemon.include_done"),
            },
            "max_turns": {
                "type": "integer",
                "minimum": 1,
                "maximum": DEFAULT_MAX_TURNS,
                "description": t(lang, "daemon.max_turns"),
            },
            "timeout": {
                "type": "number",
                "minimum": 5,
                "description": t(lang, "daemon.timeout"),
            },
            "backend": {
                "type": "string",
                "enum": [
                    "lingtai",
                    "claude-p",
                    "claude-code",
                    "codex",
                    "opencode",
                    "mimocode",
                    "mimo",
                    "qwen-code",
                    "qwen",
                    "oh-my-pi",
                    "omp",
                    "cursor",
                ],
                "description": (
                    "Execution backend: 'lingtai' (default — parallel LLM reasoning, uses your current model), "
                    "'claude-p' (Claude Code print-mode backend; 'claude-code' is a compatibility alias), "
                    "'codex' (coding tasks via OpenAI Codex CLI), "
                    "'opencode' (multi-provider open-source agent via the opencode-ai CLI), "
                    "'mimocode' / 'mimo' (MiMo Code CLI), "
                    "'qwen-code' / 'qwen' (Qwen Code CLI), "
                    "'oh-my-pi' / 'omp' (Oh-My-Pi pi-coding-agent CLI), "
                    "'cursor' (coding tasks via Cursor Agent CLI). "
                    "CLI backends use external tools with no LLM overhead from the parent."
                ),
            },
        },
        "required": ["action"],
    }


class DaemonManager:
    """Manages subagent (emanation) lifecycle."""

    # Minimum text length to trigger a parent notification for a *successful*
    # (done) run — short happy-path results are suppressed to avoid notification
    # storms. Non-success terminal states (failed / cancelled / timeout) always
    # notify regardless of length; see _on_emanation_done.
    _NOTIFY_MIN_LEN = 20

    def __init__(self, agent: "Agent", max_emanations: int = 100,
                 max_turns: int = DEFAULT_MAX_TURNS, timeout: float = 3600.0,
                 notify_threshold: int = 20):
        self._agent = agent
        self._max_emanations = max_emanations
        self._max_turns = max_turns
        self._timeout = timeout
        self._default_model = agent.service.model
        self._notify_threshold = notify_threshold

        # Emanation registry: em_id → entry dict
        self._emanations: dict[str, dict] = {}
        self._next_id = 1
        # Pool tracking for reclaim
        self._pools: list[tuple[ThreadPoolExecutor, threading.Event]] = []
        # CLI process tracking for direct process-group kill on reclaim/timeout.
        # Guarded by _cli_lock — accessed from pool workers, watchdog, and reclaim.
        #
        # ``_cli_procs`` is the flat global list used by reclaim-all / agent stop.
        # ``_cli_proc_groups`` is the per-batch index keyed by daemon ``group_id``
        # so a batch's timeout watchdog kills only *its own* CLI subprocesses and
        # never a newer, unrelated batch's (GH overlapping-batch kill). Procs that
        # do not belong to a batch (e.g. CLI ``ask`` follow-ups) register with
        # ``group_id=None`` — they are tracked globally for reclaim but no batch
        # watchdog owns them.
        self._cli_procs: list[subprocess.Popen] = []
        self._cli_proc_groups: dict[str, set[subprocess.Popen]] = {}
        # LingTai-initiated termination reason per tracked proc, keyed by
        # id(proc) and guarded by ``_cli_lock``. Stamped at the out-of-loop kill
        # sites (reclaim/agent_stop/parent refresh via _drain_all_cli_procs,
        # batch timeout via _kill_cli_group) *before* SIGTERM is sent, then read
        # back when the read loop sees the resulting -15/143 returncode so the
        # exit is attributed to the local cause instead of an opaque
        # "claude CLI exited with code 143". See GH #455.
        self._cli_term_reasons: dict[int, str] = {}
        self._cli_lock = threading.Lock()
        # Dedicated pool for CLI-backend `ask` follow-ups so they run off the
        # caller's tool-dispatch thread. The agent's `daemon(action="ask")` call
        # returns immediately while progress + final reply land in the run_dir
        # (cli_output events, last_output, follow-up completion notification).
        # Workers are submitted lazily so the pool is only spun up on first use.
        self._ask_pool = ThreadPoolExecutor(
            max_workers=max(1, max_emanations),
            thread_name_prefix="daemon-cli-ask",
        )
        self._reap_dead_parent_daemon_records()

    def _reap_dead_parent_daemon_records(self) -> None:
        """Mark stale running daemon.json records failed after parent restart."""
        daemons_dir = self._agent._working_dir / "daemons"
        if not daemons_dir.is_dir():
            return

        current_pid = os.getpid()
        for daemon_json_path in daemons_dir.glob("*/daemon.json"):
            try:
                state = json.loads(daemon_json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue

            daemon_state = state.get("state")
            if not isinstance(daemon_state, str):
                continue
            if daemon_state.lower() not in {"running", "active"}:
                continue
            if state.get("finished_at") not in (None, ""):
                continue

            parent_pid = state.get("parent_pid")
            if not isinstance(parent_pid, int) or isinstance(parent_pid, bool):
                continue
            if parent_pid == current_pid:
                continue

            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                pass
            except (PermissionError, OSError):
                continue
            else:
                continue

            state["state"] = "failed"
            state["finished_at"] = DaemonRunDir._now_iso()
            state["current_tool"] = None
            state["error"] = {
                "type": "DaemonOrphaned",
                "message": (
                    "Reaped running daemon record because recorded parent_pid "
                    f"{parent_pid} is no longer alive after daemon manager startup."
                ),
            }
            tmp_path = daemon_json_path.with_suffix(
                daemon_json_path.suffix + ".tmp"
            )
            try:
                tmp_path.write_text(
                    json.dumps(state, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(tmp_path, daemon_json_path)
            except OSError:
                continue

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        backend = _normalize_backend(args.get("backend", "lingtai"))
        if action == "emanate":
            return self._handle_emanate(
                args.get("tasks", []),
                max_turns=args.get("max_turns"),
                timeout=args.get("timeout"),
                backend=backend,
            )
        elif action == "list":
            return self._handle_list(
                contains=args.get("contains", ""),
                status_filter=args.get("status", "all"),
                include_done=args.get("include_done", True),
                limit=args.get("last"),
            )
        elif action == "ask":
            return self._handle_ask(args.get("id", ""), args.get("message", ""))
        elif action == "check":
            return self._handle_check(
                args.get("id", ""),
                last=args.get("last", 20),
                truncate=args.get("truncate", 500),
            )
        elif action == "reclaim":
            return self._handle_reclaim()
        else:
            return {"status": "error", "message": f"Unknown action: {action}"}

    def _daemon_intrinsic_surface(self) -> tuple[dict[str, FunctionSchema], dict]:
        """Return daemon-eligible intrinsic schemas/handlers.

        Daemons do not inherit the whole intrinsic layer: identity/lifecycle and
        recursive mutation tools stay unavailable.  Email is the deliberately
        narrow exception so a parent can grant a running daemon local-network
        communication when the task prompt calls for it.
        """
        from lingtai_kernel.intrinsics import ALL_INTRINSICS

        allowed = {"email"}
        schemas: dict[str, FunctionSchema] = {}
        handlers: dict = {}
        lang = self._agent._config.language
        for name in sorted(allowed):
            if name not in self._agent._intrinsics:
                continue
            info = ALL_INTRINSICS.get(name)
            if not info:
                continue
            module = info["module"]
            schemas[name] = FunctionSchema(
                name=name,
                description=module.get_description(lang),
                parameters=module.get_schema(lang),
            )
            handlers[name] = self._agent._intrinsics[name]
        return schemas, handlers

    @staticmethod
    def _resolve_task_skill_path(raw_path: str, working_dir: Path) -> Path:
        """Resolve one daemon task skill path to a concrete SKILL.md file."""
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            p = working_dir / p
        p = p.resolve(strict=False)
        if p.is_dir():
            p = p / "SKILL.md"
        if not p.is_file():
            raise ValueError(f"skill path does not resolve to a file: {raw_path}")
        if p.name != "SKILL.md":
            raise ValueError(f"skill file path must point to SKILL.md: {raw_path}")
        return p

    @staticmethod
    def _parse_task_skill_file(skill_file: Path) -> dict:
        """Parse a selected SKILL.md into the compact daemon skill catalog row."""
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as e:
            raise ValueError(f"cannot read skill file {skill_file}: {e}") from e
        m = _DAEMON_SKILL_FRONTMATTER_RE.match(text)
        if not m:
            raise ValueError(f"skill file missing YAML frontmatter: {skill_file}")
        try:
            loaded = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"skill file has invalid YAML frontmatter: {skill_file}: {e}") from e
        if not isinstance(loaded, dict):
            raise ValueError(f"skill file frontmatter must be a mapping: {skill_file}")
        raw_name = loaded.get("name")
        raw_description = loaded.get("description")
        name = " ".join(str(raw_name).split()) if raw_name is not None else ""
        description = (
            " ".join(str(raw_description).split())
            if raw_description is not None
            else ""
        )
        if not name:
            raise ValueError(f"skill file missing required frontmatter field: name: {skill_file}")
        if not description:
            raise ValueError(f"skill file missing required frontmatter field: description: {skill_file}")
        return {"name": name, "location": str(skill_file), "description": description}

    @staticmethod
    def _render_task_skill_catalog(skills: list[dict]) -> str | None:
        if not skills:
            return None
        lines = [
            "The parent selected these skills for this daemon run. Read/apply them only when relevant to your task:",
            "skills:",
        ]
        for sk in skills:
            lines.append(f"  - name: {sk['name']}")
            lines.append(f"    location: {sk['location']}")
            lines.append("    description: |")
            desc_lines = sk["description"].splitlines() or [""]
            for dl in desc_lines:
                lines.append(f"      {dl}" if dl else "      ")
        return "\n".join(lines)

    @staticmethod
    def _task_mcp_registrations(spec: dict) -> tuple[list[dict], str | None]:
        """Return normalized full MCP registrations and rendered YAML context."""
        raw = spec.get("mcp")
        if raw is None:
            return [], None
        if not isinstance(raw, list):
            raise ValueError("mcp must be an array of MCP registration objects")
        rows: list[dict] = []
        seen: set[str] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"mcp[{idx}] must be an MCP registration object")
            cfg = dict(item)
            name = cfg.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"mcp[{idx}].name must be a non-empty string")
            name = name.strip()
            if name in seen:
                raise ValueError(f"duplicate MCP registration name: {name}")
            seen.add(name)
            transport = cfg.get("transport", cfg.get("type", "stdio"))
            if transport not in ("stdio", "http"):
                raise ValueError(
                    f"mcp[{idx}].transport/type must be 'stdio' or 'http'"
                )
            normalized = dict(cfg)
            normalized["name"] = name
            normalized["transport"] = transport
            normalized.pop("type", None)
            if transport == "stdio":
                if not isinstance(normalized.get("command"), str) or not normalized["command"]:
                    raise ValueError(f"mcp[{idx}] stdio registration requires command")
                args = normalized.get("args", [])
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise ValueError(f"mcp[{idx}].args must be an array of strings")
                normalized["args"] = list(args)
                env = normalized.get("env")
                if env is not None and (
                    not isinstance(env, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                ):
                    raise ValueError(f"mcp[{idx}].env must be an object of string values")
            else:
                if not isinstance(normalized.get("url"), str) or not normalized["url"]:
                    raise ValueError(f"mcp[{idx}] http registration requires url")
                headers = normalized.get("headers")
                if headers is not None and (
                    not isinstance(headers, dict)
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())
                ):
                    raise ValueError(f"mcp[{idx}].headers must be an object of string values")
            rows.append(normalized)
        return rows, DaemonManager._render_task_mcp_catalog(rows)

    @staticmethod
    def _redact_mcp_registration_for_prompt(cfg: dict) -> dict:
        """Return a prompt-safe MCP registration copy.

        The daemon runtime uses the full object for LingTai-backend MCP startup,
        but the serialized context should not leak secret env/header values into
        model prompts. Keys remain visible so CLI backends know what must be
        supplied by their own environment/config.
        """
        out = dict(cfg)
        for field in ("env", "headers"):
            value = out.get(field)
            if isinstance(value, dict):
                out[field] = {k: "<redacted>" for k in value}
        return out

    @staticmethod
    def _render_task_mcp_catalog(registrations: list[dict]) -> str | None:
        if not registrations:
            return None
        safe = [DaemonManager._redact_mcp_registration_for_prompt(r)
                for r in registrations]
        body = yaml.safe_dump(
            {"mcp": safe},
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        return (
            "The parent provided these MCP registrations for this daemon run. "
            "They are one-run context: LingTai backend may load them directly; "
            "CLI backends should use them only if their runtime can load MCP "
            "registrations. Secret env/header values are redacted in this prompt.\n"
            f"{body}"
        )

    def _connect_task_mcp_registrations(
        self,
        registrations: list[dict],
    ) -> tuple[dict[str, FunctionSchema], dict, list[object]]:
        """Start task-scoped MCP clients and return schemas/handlers/clients."""
        if not registrations:
            return {}, {}, []
        from ...services.mcp import HTTPMCPClient, MCPClient

        schemas: dict[str, FunctionSchema] = {}
        handlers: dict = {}
        clients: list[object] = []
        licc_env = {"LINGTAI_AGENT_DIR": str(self._agent._working_dir)}
        try:
            for cfg in registrations:
                name = cfg["name"]
                if cfg["transport"] == "http":
                    client = HTTPMCPClient(
                        url=cfg["url"],
                        headers=cfg.get("headers"),
                    )
                else:
                    merged_env = {
                        **licc_env,
                        "LINGTAI_MCP_NAME": name,
                        **(cfg.get("env") or {}),
                    }
                    client = MCPClient(
                        command=cfg["command"],
                        args=cfg.get("args"),
                        env=merged_env,
                    )
                client.start()
                clients.append(client)
                for tool in client.list_tools():
                    tool_name = tool["name"]
                    if tool_name in schemas:
                        raise ValueError(f"duplicate MCP tool name: {tool_name}")
                    schema = dict(tool.get("schema", {}) or {})
                    schema.pop("additionalProperties", None)

                    def _make_handler(c, tn: str):
                        def handler(tool_args: dict) -> dict:
                            return c.call_tool(tn, tool_args)
                        return handler

                    schemas[tool_name] = FunctionSchema(
                        name=tool_name,
                        description=tool.get("description", ""),
                        parameters=schema,
                    )
                    handlers[tool_name] = _make_handler(client, tool_name)
        except Exception:
            self._close_task_mcp_clients(clients)
            raise
        return schemas, handlers, clients

    @staticmethod
    def _close_task_mcp_clients(clients: list[object] | None) -> None:
        for client in clients or []:
            try:
                client.close()
            except Exception:
                pass

    def _task_skill_catalog(self, spec: dict) -> str | None:
        """Return rendered YAML skill context selected for one daemon task."""
        raw = spec.get("skills")
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise ValueError("skills must be an array of skill directory or SKILL.md paths")
        rows: list[dict] = []
        seen: set[Path] = set()
        for idx, item in enumerate(raw):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"skills[{idx}] must be a non-empty string path")
            skill_file = self._resolve_task_skill_path(item.strip(), self._agent._working_dir)
            if skill_file in seen:
                continue
            seen.add(skill_file)
            rows.append(self._parse_task_skill_file(skill_file))
        return self._render_task_skill_catalog(rows)

    @staticmethod
    def _combine_oneshot_context(
        system_prompt: str | None,
        skill_catalog: str | None,
        mcp_catalog: str | None = None,
    ) -> str | None:
        parts = []
        if system_prompt:
            parts.append(system_prompt)
        if skill_catalog:
            parts.append("## Parent-selected skills\n" + skill_catalog)
        if mcp_catalog:
            parts.append("## Parent-provided MCP registrations\n" + mcp_catalog)
        return "\n\n".join(parts) or None

    @staticmethod
    def _task_system_prompt(spec: dict) -> str | None:
        """Return the task-level oneshot daemon prompt.

        ``system_prompt`` is an optional one-run behavior contract from the
        parent: role, constraints, tool-use policy, collaboration boundaries,
        and safety posture. If present, it must be a string, but it may be
        blank; a blank prompt means "no extra oneshot prompt" rather than a
        validation error.
        """
        if "system_prompt" not in spec:
            return None
        value = spec.get("system_prompt")
        if not isinstance(value, str):
            raise ValueError("system_prompt must be a string")
        return value.strip() or None

    @staticmethod
    def _compose_cli_task(task: str, system_prompt: str | None) -> str:
        """Embed a daemon oneshot prompt into a CLI backend task string."""
        if not system_prompt:
            return task
        return (
            "Parent-provided daemon context (oneshot; bounded to this "
            "daemon run and unable to override tool/safety limits):\n"
            f"{system_prompt}\n\n"
            "Task:\n"
            f"{task}"
        )

    @staticmethod
    def _daemon_codex_session_anchor(run_dir) -> str:
        """Return the per-run Codex cache-affinity anchor for a daemon."""
        return str((run_dir.path / "daemon.json").resolve())

    def _daemon_provider_defaults(
        self,
        provider: str,
        base_defaults: dict | None,
        run_dir,
    ) -> dict | None:
        """Return provider defaults for a daemon-scoped LLM service.

        Daemon-scoped services preserve the parent/preset provider defaults for
        every provider, so a non-Codex daemon keeps the same adapter behavior as
        its parent. Codex is the one exception: the normal Codex agent path uses
        the agent's resolved ``init.json`` path as its cache-affinity anchor, but
        a LingTai daemon is a disposable run, so Codex daemon calls need a per-run
        anchor rather than the parent agent's anchor; otherwise parent and child
        traffic collide in one REST cache slot.
        """
        provider_key = str(provider).lower()
        bucket = dict(base_defaults or {})
        if provider_key == "codex":
            # A manifest-level fixed id is an agent-level override; daemon traffic
            # must still use the daemon run identity so it gets its own cache slot.
            bucket.pop("codex_session_id", None)
            bucket["codex_session_anchor"] = self._daemon_codex_session_anchor(run_dir)
        if not bucket:
            return None
        return {provider_key: bucket}

    @staticmethod
    def _llm_defaults_from_manifest(llm: dict) -> dict:
        """Extract adapter-consulted defaults from a preset ``manifest.llm``."""
        keys = (
            "api_compat",
            "base_url",
            "codex_session_id",
            "codex_session_anchor",
            "codex_thread_salt",
            "compact_threshold",
            "default_headers",
            "max_rpm",
        )
        return {key: llm[key] for key in keys if key in llm}

    def _build_tool_surface(
        self,
        requested: list[str],
        preset_surface: tuple[dict, dict] | None = None,
        mcp_surface: tuple[dict[str, FunctionSchema], dict] | None = None,
    ) -> tuple[list[FunctionSchema], dict]:
        """Build filtered tool schemas and dispatch map for an emanation.

        When ``preset_surface`` is provided (preset-driven emanation), the
        capability tools come from the preset's pre-instantiated sandbox
        (``preset_surface = (schemas_by_name, handlers_by_name)``). Parent MCP
        tools do not auto-inherit; task ``mcp`` provides complete one-run MCP
        registrations whose tools are added through ``mcp_surface``. When
        ``preset_surface`` is None, the parent's currently registered regular
        capability surface is used, again plus only task-scoped MCP tools.
        """
        from ...capabilities import _GROUPS

        # Expand groups and filter blacklist
        tool_names: set[str] = set()
        for name in requested:
            if name in EMANATION_BLACKLIST:
                continue
            if name in _GROUPS:
                tool_names.update(_GROUPS[name])
            else:
                tool_names.add(name)

        intrinsic_schemas, intrinsic_handlers = self._daemon_intrinsic_surface()
        tool_names |= set(intrinsic_schemas)
        mcp_schemas, mcp_handlers = mcp_surface or ({}, {})
        parent_mcp_names = self._parent_mcp_tool_names()
        reserved_names = ({s.name for s in self._agent._tool_schemas} - parent_mcp_names) | set(intrinsic_schemas)
        mcp_collisions = set(mcp_schemas) & reserved_names
        if mcp_collisions:
            raise ValueError(
                "Task MCP tools collide with existing parent/daemon tools: "
                f"{sorted(mcp_collisions)}"
            )
        parent_mcp_requested = (tool_names & self._parent_mcp_tool_names()) - set(mcp_schemas)
        if parent_mcp_requested:
            raise ValueError(
                "Parent MCP tools must be provided as task mcp registrations, "
                f"not requested via tools: {sorted(parent_mcp_requested)}"
            )

        if preset_surface is not None:
            preset_schemas, preset_handlers = preset_surface
            # Available surface = preset capabilities ∪ task-scoped MCP tools
            # ∪ daemon-eligible intrinsics (currently email).
            available = set(preset_schemas.keys()) | set(mcp_schemas) | set(intrinsic_schemas)
            # The narrow daemon intrinsic surface and task MCP surface are
            # auto-included for this one run.
            tool_names |= set(mcp_schemas) | set(intrinsic_schemas)

            missing = tool_names - available
            if missing:
                raise ValueError(f"Unknown tools for emanation: {missing}")

            # Build merged schemas + dispatch — preset tools first, MCP fills in
            schemas: list[FunctionSchema] = []
            dispatch: dict = {}
            parent_schema_map = {s.name: s for s in self._agent._tool_schemas}
            for n in sorted(tool_names):
                if n in preset_schemas:
                    schemas.append(preset_schemas[n])
                    if n in preset_handlers:
                        dispatch[n] = preset_handlers[n]
                elif n in intrinsic_schemas:
                    schemas.append(intrinsic_schemas[n])
                    if n in intrinsic_handlers:
                        dispatch[n] = intrinsic_handlers[n]
                elif n in mcp_schemas:
                    schemas.append(mcp_schemas[n])
                    if n in mcp_handlers:
                        dispatch[n] = mcp_handlers[n]
            return schemas, dispatch

        # Default path: emanation runs on the parent's capability surface plus
        # task-scoped MCP tools from full registrations.
        tool_names |= set(mcp_schemas)

        # Validate requested tools exist
        available = ({s.name for s in self._agent._tool_schemas}
                     | set(intrinsic_schemas) | set(mcp_schemas))
        missing = tool_names - available
        if missing:
            raise ValueError(f"Unknown tools for emanation: {missing}")

        # Build schemas and dispatch
        schema_map = {s.name: s for s in self._agent._tool_schemas}
        schemas = []
        for n in sorted(tool_names):
            if n in intrinsic_schemas:
                schemas.append(intrinsic_schemas[n])
            elif n in mcp_schemas:
                schemas.append(mcp_schemas[n])
            elif n in schema_map:
                schemas.append(schema_map[n])
        dispatch = {n: self._agent._tool_handlers[n]
                    for n in tool_names if n in self._agent._tool_handlers}
        for n in tool_names:
            if n in mcp_handlers:
                dispatch[n] = mcp_handlers[n]
            if n in intrinsic_handlers:
                dispatch[n] = intrinsic_handlers[n]
        return schemas, dispatch


    def _parent_mcp_tool_names(self) -> set[str]:
        """Return parent MCP tool names tracked by the parent agent."""
        names = getattr(self._agent, "_mcp_tool_names", set())
        if not isinstance(names, set):
            return set()
        return {n for n in names if isinstance(n, str)}

    def _expand_requested_tools(self, requested: list[str]) -> set[str]:
        """Expand requested daemon tools after group aliases and blacklist."""
        from ...capabilities import _GROUPS

        tool_names: set[str] = set()
        for name in requested:
            if name in EMANATION_BLACKLIST:
                continue
            if name in _GROUPS:
                tool_names.update(_GROUPS[name])
            else:
                tool_names.add(name)
        return tool_names

    def _instantiate_preset_capabilities(
        self,
        preset_caps: dict,
        preset_llm: dict,
        required_tools: set[str] | None = None,
    ) -> tuple[dict, dict]:
        """Instantiate a preset's manifest.capabilities into a sandbox.

        Returns ``(schemas_by_name, handlers_by_name)``. Capabilities run
        their ``setup()`` against a ``_CapabilitySandbox`` so the parent's
        own tool registry is not mutated. ``provider: "inherit"`` sentinels
        in the preset's capability kwargs resolve against the *preset's*
        LLM, not the parent's — capabilities follow the body that hosts
        them.

        Raises ``ValueError`` for broken capabilities that are required by
        the current task. Broken unused capabilities are logged and skipped.
        The caller (``_handle_emanate``) converts required setup failures into
        a tool-level error and refuses the whole batch.
        """
        from ...capabilities import setup_capability, _GROUPS, _BUILTIN
        from ...presets import expand_inherit

        # Resolve provider:"inherit" sentinels against the preset's LLM
        # (not the parent's). expand_inherit mutates in place — work on a
        # deep enough copy so the original preset dict is unchanged.
        import copy
        resolved = copy.deepcopy(preset_caps)
        expand_inherit(resolved, preset_llm)

        # Expand group names (e.g. 'file' → read/write/edit/glob/grep). Groups
        # inherit the same kwargs as the group entry — same convention as
        # agent.py:790. Without this, setup_capability would reject 'file'
        # as an unknown capability.
        expanded: dict = {}
        for name, kwargs in resolved.items():
            if name in _GROUPS:
                for sub in _GROUPS[name]:
                    # Each group member gets its own kwargs copy — if a
                    # capability's setup() ever pops or mutates its kwargs
                    # in place, sibling members must not be corrupted.
                    expanded[sub] = dict(kwargs) if isinstance(kwargs, dict) else {}
            else:
                expanded[name] = kwargs

        collector = _ToolCollector(self._agent)
        required = required_tools
        for name, kwargs in expanded.items():
            if name in EMANATION_BLACKLIST:
                continue
            # Tolerate non-capability names (intrinsics like 'email', 'psyche',
            # 'system', 'soul' — kernel always-on, not composable). The TUI
            # preset wizard writes these into manifest.capabilities and the
            # main Agent.__init__ tolerates them via try/except (agent.py:91-94);
            # the daemon sandbox must replicate that tolerance or "full" user
            # presets become unusable as daemon presets. See lingtai #29.
            if name not in _BUILTIN:
                self._log(
                    "daemon_preset_capability_skipped",
                    capability=name,
                    reason="not a composable capability (intrinsic or unknown)",
                )
                continue
            if not isinstance(kwargs, dict):
                kwargs = {}
            try:
                setup_capability(collector, name, **kwargs)
            except Exception as e:
                if required is not None and name not in required:
                    self._log(
                        "daemon_preset_capability_skipped",
                        capability=name,
                        reason=f"setup failed: {e}",
                    )
                    continue
                raise ValueError(
                    f"preset capability {name!r} failed to set up: {e}"
                ) from e

        return collector.schemas, collector.handlers

    def _build_emanation_prompt(
        self,
        task: str,
        schemas: list[FunctionSchema],
        system_prompt: str | None = None,
    ) -> str:
        """Build the system prompt for an emanation."""
        lines = [
            "You are a daemon emanation (分神) — a focused subagent dispatched by an agent.",
            "You have one task. Complete it, then provide your final report as text.",
            "Your intermediate text output will be seen by the main agent — treat it as a progress report.",
            'When you are done, explicitly state "task done" and summarize what you accomplished.',
            "",
            "You work in the agent's working directory. Other subagents may be working",
            "concurrently on different tasks in the same directory. Do not modify files",
            "outside your assigned scope.",
        ]

        # Tool descriptions
        tool_lines = []
        for s in schemas:
            if s.description:
                tool_lines.append(f"### {s.name}\n{s.description}")
        if tool_lines:
            lines.append("")
            lines.append("## tools")
            lines.extend(tool_lines)

        if system_prompt:
            lines.append("")
            lines.append("## Parent-provided daemon context (oneshot)")
            lines.append(
                "These parent instructions and selected skills/MCP context "
                "apply only to this daemon run. They may narrow how you complete "
                "the task, "
                "but they do not override the daemon lifecycle, cancellation/"
                "timeout limits, available tool schema, or tool execution/"
                "approval guard."
            )
            lines.append(system_prompt)

        lines.append("")
        lines.append("Your task:")
        lines.append(task)

        return "\n".join(lines)

    def _run_emanation(self, em_id: str, run_dir, schemas, dispatch,
                       task: str,
                       cancel_event: threading.Event,
                       timeout_event: threading.Event | None = None,
                       preset_llm: dict | None = None,
                       max_turns: int | None = None,
                       mcp_clients: list[object] | None = None) -> str:
        """Run a single emanation's tool loop. Called in a worker thread.

        run_dir is the DaemonRunDir constructed in _handle_emanate. All
        filesystem effects flow through it.

        timeout_event distinguishes watchdog-fired cancellation (timeout) from
        manual reclaim. When set alongside cancel_event, the run loop calls
        mark_timeout instead of mark_cancelled. None is allowed for direct-call
        tests and the cancellation defaults to "cancelled" semantics.

        preset_llm: if provided, a dict with keys provider/model/api_key_env/
        base_url (and optionally api_key) resolved from the preset. A dedicated
        LLMService is constructed for this emanation instead of reusing
        self._agent.service.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        if preset_llm:
            # Build a dedicated LLM service for this emanation from the preset.
            from lingtai.llm.service import LLMService
            from lingtai_kernel.config_resolve import resolve_env
            api_key = resolve_env(preset_llm.get("api_key"), preset_llm.get("api_key_env"))
            service = LLMService(
                provider=preset_llm["provider"],
                model=preset_llm["model"],
                api_key=api_key,
                base_url=preset_llm.get("base_url"),
                provider_defaults=self._daemon_provider_defaults(
                    preset_llm["provider"],
                    self._llm_defaults_from_manifest(preset_llm),
                    run_dir,
                ),
            )
            effective_model = preset_llm["model"]
        else:
            # No preset: build a fresh daemon-scoped service mirroring the parent
            # service rather than reusing ``self._agent.service``. Every provider
            # keeps the parent's provider defaults; Codex additionally gets a
            # per-run cache anchor (and drops any fixed session id) so this run
            # gets its own session_id/thread_id/prompt_cache_key triple instead
            # of colliding with the parent agent's cache slot.
            from lingtai.llm.service import LLMService
            parent_service = self._agent.service
            parent_provider = str(getattr(parent_service, "provider", "")).lower()
            parent_defaults = getattr(parent_service, "_provider_defaults", {}) or {}
            parent_key_resolver = getattr(parent_service, "_key_resolver", None)
            parent_api_key = (
                parent_key_resolver(parent_provider)
                if callable(parent_key_resolver)
                else None
            )
            service = LLMService(
                provider=parent_service.provider,
                model=parent_service.model,
                api_key=parent_api_key,
                base_url=getattr(parent_service, "_base_url", None),
                key_resolver=parent_key_resolver,
                provider_defaults=self._daemon_provider_defaults(
                    parent_provider,
                    parent_defaults.get(parent_provider, {}),
                    run_dir,
                ),
                context_window=getattr(parent_service, "_context_window", 1_000_000),
            )
            effective_model = parent_service.model

        session = service.create_session(
            system_prompt=run_dir.prompt_path.read_text(encoding="utf-8"),
            tools=schemas or None,
            model=effective_model,
            thinking="default",
            tracked=False,
        )

        endpoint = getattr(service, "_base_url", None)

        intrinsic_tool_names = set(self._daemon_intrinsic_surface()[1])

        def _dispatch_daemon_tool(tc):
            handler = dispatch.get(tc.name)
            if handler is None:
                from lingtai_kernel.types import UnknownToolError
                raise UnknownToolError(tc.name)
            args = dict(tc.args or {})
            if tc.name in intrinsic_tool_names:
                args["_tc_id"] = tc.id
            return handler(args)

        def _daemon_tool_logger(event_type: str, **fields) -> None:
            tool_name = fields.get("tool_name") or fields.get("tool")
            if event_type == "tool_call_normalized" and tool_name:
                run_dir.set_current_tool(tool_name, fields.get("tool_args") or {})
            elif event_type == "tool_result" and tool_name:
                status = "error" if fields.get("status") == "error" else "ok"
                run_dir.clear_current_tool(result_status=status)
            self._log(
                f"daemon_{event_type}",
                em_id=em_id,
                run_id=getattr(run_dir, "run_id", None),
                **fields,
            )

        executor = ToolExecutor(
            dispatch_fn=_dispatch_daemon_tool,
            make_tool_result_fn=lambda name, result, **kw: service.make_tool_result(
                name, result, **kw
            ),
            guard=LoopGuard(),
            known_tools=set(dispatch),
            parallel_safe_tools=set(),
            logger_fn=_daemon_tool_logger,
            meta_fn=lambda: build_meta(self._agent),
            working_dir=self._agent._working_dir,
            tool_call_guard=getattr(self._agent, "_tool_call_guard", None),
        )

        def _accum(resp):
            if resp.usage is None:
                return
            u = resp.usage
            run_dir.append_tokens(
                input=u.input_tokens,
                output=u.output_tokens,
                thinking=u.thinking_tokens,
                cached=u.cached_tokens,
                model=effective_model,
                endpoint=endpoint,
            )

        try:
            run_dir.record_user_send(task, kind="task")
            response = session.send(task)
            _accum(response)
            turns = 0
            run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

            effective_max_turns = max_turns if max_turns is not None else self._max_turns
            while response.tool_calls and turns < effective_max_turns:
                if cancel_event.is_set():
                    return _exit_cancelled()

                # Intermediate text is already persisted in chat_history via
                # bump_turn(); do not inject daemon progress as parent requests.

                executor.guard.record_calls(len(response.tool_calls))
                tool_results, intercepted, intercept_text = executor.execute(
                    response.tool_calls,
                    api_call_id=getattr(response, "api_call_id", None),
                )
                executor.guard.clear_progress_notice()

                if intercepted:
                    # Preserve provider pairing by recording the synthesized tool
                    # results, then terminate the daemon with the intercept text.
                    run_dir.record_user_send(
                        json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                        kind="tool_results",
                    )
                    text = intercept_text or "[intercepted]"
                    run_dir.mark_done(text)
                    return text

                # Tool results are written to chat_history before sending
                run_dir.record_user_send(
                    json.dumps([str(r) for r in tool_results], ensure_ascii=False),
                    kind="tool_results",
                )
                response = session.send(tool_results)
                _accum(response)
                turns += 1
                run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

                # Inject follow-up as a separate user message — only safe when
                # the response is text-only. If it carries new tool_calls, the
                # canonical interface tail is assistant[tool_calls] and a user
                # message here would violate the pairing invariant.
                if not response.tool_calls:
                    followup = self._drain_followup(em_id)
                    if followup:
                        run_dir.record_user_send(followup, kind="followup")
                        response = session.send(followup)
                        _accum(response)
                        turns += 1
                        run_dir.bump_turn(turn=turns + 1, response_text=response.text or "")

            text = response.text or "[no output]"
            run_dir.mark_done(text)
            return text
        except Exception as e:
            run_dir.mark_failed(e)
            raise
        finally:
            self._close_task_mcp_clients(mcp_clients)

    def _find_claude_session_id(self, em_id: str) -> str | None:
        """Search ~/.claude/projects/ for the session JSONL whose customTitle matches em_id.

        Claude Code stores sessions as JSONL files under
        ``~/.claude/projects/<project-hash>/``. The first line of each session
        file is a JSON object with ``type: "custom-title"`` containing the
        ``customTitle`` and ``sessionId``.
        """
        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.is_dir():
            return None
        for jsonl_path in projects_dir.rglob("*.jsonl"):
            try:
                with open(jsonl_path, "r", encoding="utf-8") as f:
                    first_line = f.readline().strip()
                if not first_line:
                    continue
                obj = json.loads(first_line)
                if (obj.get("type") == "custom-title"
                        and obj.get("customTitle") == em_id):
                    return obj.get("sessionId")
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def _run_claude_code_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run a Claude Code CLI session as the emanation backend.

        Spawns Claude Code with ``--output-format stream-json --verbose`` so
        events arrive in real time (vs ``--output-format text``, which
        buffers everything until completion — see GH issues #99/#100).
        Parses each event line and writes:

        - ``claude_session_id`` to daemon.json on the first event that
          carries one (typically the system ``init`` event, but any event
          with ``session_id`` works as a fallback). This makes
          ``daemon(ask)`` usable from the moment ``emanate`` returns,
          rather than after the initial run completes.
        - Per-turn ``text``/``tool_use`` blocks via
          ``record_cli_output`` so ``daemon(check)`` shows live progress.
        - Tool calls via ``set_current_tool`` / ``clear_current_tool``.
        - stderr to its own pipe so diagnostic messages aren't lost in
          the stdout stream.

        Note: Claude Code's token ``usage`` fields are deliberately NOT
        forwarded to ``append_tokens``. Claude Code bills through its
        own provider account, and its cache_creation/cache_read
        semantics don't map cleanly onto the kernel's LLM-adapter
        accounting. Mixing them into ``sum_token_ledger`` would
        produce a misleading "lifetime totals" number for the parent.
        The final ``result`` event's ``usage`` is instead persisted to
        ``daemon.json.cli_tokens`` via ``record_cli_tokens`` — UI-only,
        never touching either token ledger — so the TUI ``/daemons``
        view can still surface what the CLI run cost.

        Falls back to the legacy JSONL scan if no ``session_id`` ever
        appears in the stream.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        # Required infrastructure flags come first; free-form
        # backend_options sit between them and the task prompt so the
        # task itself stays the trailing positional argument that the
        # Claude Code CLI expects.
        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            "--name", em_id,
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(task)
        self._log("daemon_claude_code_start", em_id=em_id, cmd=" ".join(cmd))

        spawn_env = _claude_code_env()
        if len(spawn_env) != len(os.environ):
            self._log("daemon_claude_code_env_stripped", em_id=em_id,
                      stripped=[k for k in _CLAUDE_CODE_STRIP_ENV if k in os.environ])

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                env=spawn_env,
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            exc = RuntimeError("'claude' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start claude CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        self._register_cli_proc(proc, group_id=run_dir.group_id)

        # Drain stderr in a background thread so diagnostic messages reach
        # the run dir even while the main thread is parsing stdout events.
        # iLink-style daemons with a chatty stderr would otherwise block
        # the pipe and stall the process.
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True, name=f"daemon-claude-stderr-{em_id}",
        )
        stderr_thread.start()

        final_result_text: str | None = None
        final_is_error: bool = False
        session_id_captured: str | None = None
        # Active tool_use blocks awaiting their tool_result. Keyed by
        # the tool_use id from the assistant message; value is the tool
        # name so we can call clear_current_tool with a status string.
        pending_tools: dict[str, str] = {}

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if session_id_captured == sid:
                return
            session_id_captured = sid
            run_dir._state["claude_session_id"] = sid
            run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log("daemon_claude_code_session",
                      em_id=em_id, session_id=sid)

        def _handle_assistant_event(event: dict) -> None:
            message = event.get("message") or {}
            content = message.get("content") or []
            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text.strip():
                        run_dir.record_cli_output(text, stream="stdout")
                elif btype == "tool_use":
                    tool_id = block.get("id") or ""
                    tool_name = block.get("name") or "unknown"
                    tool_input = block.get("input") or {}
                    if tool_id:
                        pending_tools[tool_id] = tool_name
                    try:
                        run_dir.set_current_tool(tool_name, tool_input)
                    except Exception:
                        pass
            # NOTE: Claude Code spend is intentionally NOT recorded in the
            # daemon's or parent's token ledger. Claude Code runs as an
            # external process with its own billing path; counting its
            # `usage` fields here would mix unrelated currencies (cache
            # read/write semantics differ from the kernel's LLM adapters)
            # and create a misleading "lifetime totals" number. Spend
            # remains visible to the agent via daemon(check) — the
            # `last_output` field, cli_output events, and stderr — and,
            # for UI display, the final result event's usage is persisted
            # separately to daemon.json.cli_tokens (see the result-event
            # handler below). Neither path touches sum_token_ledger.

        def _handle_user_event(event: dict) -> None:
            # User events in stream-json mode carry tool_result blocks back
            # from tool executions performed by Claude Code itself.
            message = event.get("message") or {}
            content = message.get("content") or []
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id") or ""
                status = "error" if block.get("is_error") else "ok"
                if tool_id in pending_tools:
                    pending_tools.pop(tool_id, None)
                try:
                    run_dir.clear_current_tool(status)
                except Exception:
                    pass

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if cancel_event.is_set():
                    _kill_process_group(proc)
                    return _exit_cancelled()

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Defensive: if Claude Code ever emits a non-JSON line
                    # in stream-json mode (e.g. a startup banner), don't
                    # crash the parse — surface it as raw stdout.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                # Capture session_id from the first event that has it. The
                # very first system events (hook_started, init) already
                # carry it, so this typically fires within the first few
                # lines — well before the LLM produces any reply.
                sid = event.get("session_id")
                if sid and session_id_captured != sid:
                    _store_session_id(sid)

                etype = event.get("type")
                if etype == "assistant":
                    _handle_assistant_event(event)
                elif etype == "user":
                    _handle_user_event(event)
                elif etype == "result":
                    final_result_text = event.get("result") or ""
                    final_is_error = bool(event.get("is_error"))
                    # Persist Claude Code's reported token usage for UI
                    # display only. This goes to daemon.json.cli_tokens via
                    # record_cli_tokens — NOT to append_tokens — so the
                    # parent/daemon token ledgers stay free of external CLI
                    # billing (whose cache semantics don't match the kernel
                    # adapter accounting). See the note in this method's
                    # docstring and run_dir.record_cli_tokens.
                    usage = _normalize_claude_usage(event.get("usage"))
                    if usage is not None:
                        try:
                            run_dir.record_cli_tokens(
                                input=usage["input"], output=usage["output"],
                                cached=usage["cached"],
                                thinking=usage["thinking"],
                                raw=event.get("usage"),
                            )
                        except Exception:
                            pass
                    # If there are still tool_use blocks pending without
                    # a matching tool_result (shouldn't happen on success,
                    # but be defensive), clear them so daemon.json's
                    # current_tool doesn't stay stuck.
                    while pending_tools:
                        pending_tools.popitem()
                        try:
                            run_dir.clear_current_tool("ok")
                        except Exception:
                            pass

            proc.wait()
        except Exception as e:
            _kill_process_group(proc)
            run_dir.mark_failed(e)
            raise
        finally:
            # Give the stderr drainer a moment to finish reading any
            # remaining bytes before the pipe closes on us.
            stderr_thread.join(timeout=2.0)
            # Remove from tracked procs to prevent PID recycling issues
            self._unregister_cli_proc(proc, group_id=run_dir.group_id)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or (final_result_text or "")
            attributed = self._attributed_cli_exit(
                proc, "claude", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"claude CLI exited with code {proc.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # If the result event signalled an error even though the process
        # exited 0, surface that so the caller doesn't think the task
        # succeeded.
        if final_is_error:
            exc = RuntimeError(
                f"claude CLI reported is_error=true: "
                f"{(final_result_text or stderr_tail)[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Fallback: if no event carried session_id (extremely unusual but
        # possible if Claude Code changes its stream format), fall back to
        # the legacy JSONL scan so daemon(ask) still works.
        if not session_id_captured:
            session_id = self._find_claude_session_id(em_id)
            if session_id:
                _store_session_id(session_id)

        text = (final_result_text or "").strip() or "[no output]"
        run_dir.mark_done(text)
        return text

    def _run_claude_interactive_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run an interactive Claude Code session through a PTY.

        This is the experimental ``backend="claude"`` route inspired by
        third-party ``claude -p`` replacements: run the normal interactive
        ``claude`` TUI, use SessionStart/Stop hooks as synchronization points,
        and read Claude's transcript JSONL for the daemon result.  It does not
        mutate Claude's global config and refuses credential/trust automation.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        try:
            result = run_claude_interactive(
                em_id=em_id,
                run_dir=run_dir,
                working_dir=self._agent._working_dir,
                task=task,
                cancel_event=cancel_event,
                timeout_event=timeout_event,
                backend_argv=backend_argv,
                env=_claude_code_env(),
                log_callback=self._log,
            )
        except ClaudeInteractiveError as e:
            run_dir.mark_failed(e)
            raise
        except Exception as e:
            run_dir.mark_failed(e)
            raise

        if cancel_event.is_set():
            return _exit_cancelled()

        text = (result.final_text or "").strip() or "[no output]"
        run_dir.mark_done(text)
        return text

    def _run_codex_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run a Codex CLI session as the emanation backend.

        Spawns Codex with ``--json`` so events arrive as JSONL (one event
        per stdout line), and parses them so the daemon shows live
        progress and captures a resumable session id — mirroring the
        Claude Code backend. ``--ephemeral`` is intentionally **not**
        passed: it would disable session persistence and break
        ``daemon(ask, id=em-N)``.

        Event shapes (codex-cli 0.128.0):
        - ``{"type":"thread.started","thread_id":"<uuid>"}`` — first event,
          carries the session id we'll later pass to
          ``codex exec resume <id>``.
        - ``{"type":"turn.started"}`` — marks an agent turn beginning.
        - ``{"type":"item.completed","item":{"type":"agent_message","text":"..."}}``
          — visible agent reply text.
        - ``{"type":"turn.completed","usage":{...}}`` — terminal event.
          Codex reports token usage on this event, but we deliberately do
          NOT forward it to ``append_tokens``: codex runs as an external
          process with its own billing path, and counting its tokens
          into the kernel's ledger would mix unrelated currencies. Spend
          is visible to the agent via ``daemon(check)`` but not via
          ``sum_token_ledger``.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        cmd = [
            "codex",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(task)
        self._log("daemon_codex_start", em_id=em_id, cmd=" ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            exc = RuntimeError("'codex' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start codex CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        self._register_cli_proc(proc, group_id=run_dir.group_id)

        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True, name=f"daemon-codex-stderr-{em_id}",
        )
        stderr_thread.start()

        session_id_captured: str | None = None
        agent_message_texts: list[str] = []
        turn_completed = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if session_id_captured == sid:
                return
            session_id_captured = sid
            run_dir._state["codex_session_id"] = sid
            run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log("daemon_codex_session", em_id=em_id, session_id=sid)

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if cancel_event.is_set():
                    _kill_process_group(proc)
                    return _exit_cancelled()

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Defensive: surface non-JSON lines as raw stdout
                    # instead of crashing the parser.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "thread.started":
                    tid = event.get("thread_id")
                    if tid:
                        _store_session_id(tid)
                elif etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text.strip():
                            agent_message_texts.append(text)
                            run_dir.record_cli_output(text, stream="stdout")
                elif etype == "turn.completed":
                    turn_completed = True
                    # NOTE: Codex spend is intentionally NOT recorded in
                    # the daemon's or parent's token ledger. Codex runs
                    # as an external process with its own billing path,
                    # and its `cached_input_tokens` semantics differ
                    # from the kernel's LLM adapters (codex `input_tokens`
                    # already includes the cached portion). Mixing it in
                    # would produce a misleading "lifetime totals" number.
                    # Spend is visible to the agent via daemon(check),
                    # not via sum_token_ledger.

            proc.wait()
        except Exception as e:
            _kill_process_group(proc)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            # Remove from tracked procs to prevent PID recycling issues
            self._unregister_cli_proc(proc, group_id=run_dir.group_id)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(agent_message_texts[-3:])
            attributed = self._attributed_cli_exit(
                proc, "codex", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"codex CLI exited with code {proc.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Codex doesn't emit an `is_error` flag like Claude Code; the
        # signal that the turn finished cleanly is a `turn.completed`
        # event. If we never saw one AND captured no agent messages,
        # treat that as a failure even though the process exited 0.
        if not turn_completed and not agent_message_texts:
            exc = RuntimeError(
                f"codex CLI produced no turn.completed event: "
                f"{(stderr_tail or '[no output]')[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = "\n".join(agent_message_texts).strip() or "[no output]"
        run_dir.mark_done(text)
        return text

    _NOTIFICATION_PREVIEW_MAX = 500

    def _publish_daemon_notification(
        self,
        em_id: str,
        *,
        status: str,
        text: str,
        run_dir: DaemonRunDir | None = None,
    ) -> None:
        """Publish a compact daemon terminal event via .notification/system.json.

        Fired on every terminal status (done / failed / cancelled / timeout) so
        the parent agent can dispatch a daemon and safely go idle: the kernel
        notification sync wakes it when the run ends, no polling required. Full
        daemon output belongs in the run directory and is inspectable via
        ``daemon(action="check", id=...)``.  The parent notification is only a
        wake signal with provenance, bounded preview, and the inspection path.
        It must not arrive as ordinary ``MSG_REQUEST`` text.

        Once-only delivery is the caller's responsibility via
        ``DaemonRunDir.claim_terminal_notification`` (terminal path); follow-up
        (``ask``) notifications intentionally reuse this same compact format.
        """
        preview = text or ""
        if len(preview) > self._NOTIFICATION_PREVIEW_MAX:
            preview = (
                preview[: self._NOTIFICATION_PREVIEW_MAX]
                + f"...[truncated; {len(preview)} chars total]"
            )
        parts = [
            f"Daemon {em_id} {status}.",
            f"Inspect with daemon(action=\"check\", id=\"{em_id}\").",
        ]
        recorded_error = None
        if run_dir is not None:
            snapshot = run_dir.state_snapshot()
            task = (snapshot.get("task") or "").strip()
            if task:
                if len(task) > self._NOTIFICATION_PREVIEW_MAX:
                    task = task[: self._NOTIFICATION_PREVIEW_MAX] + "..."
                parts.append(f"Task: {task}")
            parts.append(f"Run directory: {run_dir.path}")
            result_path = snapshot.get("result_path")
            if result_path:
                parts.append(f"Result file: {result_path}")
            recorded_error = snapshot.get("error")
        if recorded_error:
            err_type = recorded_error.get("type", "error")
            err_msg = (recorded_error.get("message") or "")[:self._NOTIFICATION_PREVIEW_MAX]
            parts.append(f"Error: {err_type}: {err_msg}".rstrip(": "))
        if preview:
            parts.append(f"Preview:\n{preview}")
        body = "\n".join(parts)
        try:
            self._agent._enqueue_system_notification(
                source="daemon",
                ref_id=em_id,
                body=body,
            )
        except Exception as e:
            self._log(
                "daemon_notification_error",
                em_id=em_id,
                status=status,
                error=str(e)[:200],
            )

    def _publish_followup_if_live(
        self,
        em_id: str,
        *,
        status: str,
        text: str,
        run_dir: DaemonRunDir | None = None,
    ) -> None:
        """Publish a follow-up completion notification only if the emanation
        is still tracked. A reclaim that races an in-flight CLI ask would
        otherwise produce a "follow-up failed" notification for an entry the
        agent has already torn down — surprising and unactionable. Run_dir
        writes still happen unconditionally inside the worker; this gate is
        for the parent-facing notification only.
        """
        entry = self._emanations.get(em_id)
        if entry is None or entry.get("shutdown_in_progress"):
            self._log(
                "daemon_ask_post_reclaim",
                em_id=em_id, status=status, text_length=len(text or ""),
            )
            return
        self._publish_daemon_notification(
            em_id, status=status, text=text, run_dir=run_dir,
        )

    def _on_ask_done(self, em_id: str, future) -> None:
        """Done-callback for ask workers — surface any worker-thread exception.

        Without this, an unexpected exception in the stream-parse loop
        (e.g. an unhandled stdout decode error) would land silently in the
        future and never reach the agent or the run_dir. We log the
        exception via the standard daemon log channel and best-effort
        record it into the emanation's run_dir as a cli_output line so a
        later daemon(check) shows what happened.
        """
        try:
            exc = future.exception()
        except Exception:  # noqa: BLE001 — future internals raising is itself worth logging
            exc = None
        if exc is None:
            return
        self._log(
            "daemon_ask_worker_error",
            em_id=em_id,
            exception=type(exc).__name__,
            message=str(exc)[:500],
        )
        entry = self._emanations.get(em_id)
        run_dir = entry.get("run_dir") if entry else None
        if run_dir is not None:
            try:
                run_dir.record_cli_output(
                    f"[ask worker error] {type(exc).__name__}: {str(exc)[:300]}",
                    stream="stderr",
                )
            except OSError:
                pass
        # Clear ask_in_flight if the worker raised before its finally ran
        # (very rare — finally would normally clear it). Safe to do twice.
        if entry is not None:
            try:
                with entry["followup_lock"]:
                    entry["ask_in_flight"] = False
            except Exception:  # noqa: BLE001 — entry mutation must never re-raise
                pass

    def _drain_followup(self, em_id: str) -> str | None:
        """Drain the follow-up buffer for a specific emanation."""
        entry = self._emanations.get(em_id)
        if not entry:
            return None
        with entry["followup_lock"]:
            text = entry["followup_buffer"]
            entry["followup_buffer"] = ""
        return text or None

    def _handle_emanate(self, tasks: list[dict],
                        max_turns: int | None = None,
                        timeout: float | None = None,
                        backend: str = "lingtai") -> dict:
        backend = _normalize_backend(backend)
        if not tasks:
            return {"status": "error", "message": "No tasks provided"}

        # Per-batch limit overrides — capped at the manager's ceilings.
        # Author-set ceilings (self._max_turns, self._timeout) are the upper
        # bounds; the agent picks within them. None means "use ceiling".
        if max_turns is not None:
            try:
                mt = int(max_turns)
            except (TypeError, ValueError):
                return {"status": "error",
                        "message": f"max_turns must be a positive integer (got {max_turns!r})"}
            if mt <= 0:
                return {"status": "error",
                        "message": f"max_turns must be ≥ 1 (got {mt})"}
            effective_max_turns = min(mt, self._max_turns)
        else:
            effective_max_turns = self._max_turns

        if timeout is not None:
            try:
                to = float(timeout)
            except (TypeError, ValueError):
                return {"status": "error",
                        "message": f"timeout must be a positive number (got {timeout!r})"}
            # Floor at 5s — the watchdog ticks at 1s granularity and the
            # OS scheduler may delay the watchdog thread's first run, so a
            # sub-5s timeout can fire before any emanation thread starts and
            # mark them as 'timeout' without ever running.
            if to < 5:
                return {"status": "error",
                        "message": f"timeout must be ≥ 5 seconds (got {to})"}
            effective_timeout = min(to, self._timeout)
        else:
            effective_timeout = self._timeout

        # Clear completed emanations and stale pools.
        # Keep completed CLI emanations (backend != lingtai) so that `ask`
        # can still route to `_handle_ask_cli` / `_handle_ask_codex` /
        # `_handle_ask_opencode` / `_handle_ask_cursor`
        # and `list` can show them.
        self._emanations = {
            k: v for k, v in self._emanations.items()
            if not v["future"].done() or v.get("backend") not in (None, "lingtai")
        }
        self._pools = [(p, c) for p, c in self._pools if not c.is_set()]

        # --- External CLI backends: skip preset resolution entirely ---
        if backend in (
            "claude", "claude-interactive", "claude-p", "claude-code",
            "codex", "opencode", "mimocode", "qwen-code", "oh-my-pi", "cursor",
        ):
            return self._handle_emanate_cli(
                tasks, backend=backend,
                effective_max_turns=effective_max_turns,
                effective_timeout=effective_timeout,
            )

        # Pre-flight: resolve any per-task presets BEFORE scheduling.
        # If any preset is invalid, refuse the whole batch. Presets are
        # identified by path (~/foo.json, ./foo.json, or absolute).
        from lingtai.presets import load_preset
        from lingtai_kernel.preset_connectivity import check_connectivity

        resolved_presets: list[dict | None] = []  # one entry per task — None means inherit
        for spec in tasks:
            preset_name = spec.get("preset")
            if not preset_name:
                resolved_presets.append(None)
                continue
            # Validate preset exists and is loadable
            try:
                preset = load_preset(preset_name, working_dir=self._agent._working_dir)
            except (KeyError, ValueError) as e:
                return {"status": "error",
                        "message": f"preset {preset_name!r} unloadable: {e}"}
            preset_llm = preset.get("manifest", {}).get("llm", {})
            # Connectivity check — refuse upfront rather than burning tokens later
            conn = check_connectivity(
                provider=preset_llm.get("provider"),
                base_url=preset_llm.get("base_url"),
                api_key_env=preset_llm.get("api_key_env"),
            )
            if conn["status"] != "ok":
                return {"status": "error",
                        "message": f"preset {preset_name!r}: {conn['status']} — "
                                   f"{conn.get('error', 'cannot reach LLM')}"}
            preset_caps = preset.get("manifest", {}).get("capabilities", {})
            # Instantiate preset capabilities into a sandbox up front so any
            # setup-time failure refuses the whole batch (consistent with
            # connectivity refusal). Empty caps dict → empty sandbox surface,
            # which means the emanation only gets task-scoped MCP tools —
            # that's a valid if unusual configuration.
            try:
                preset_schemas, preset_handlers = self._instantiate_preset_capabilities(
                    preset_caps,
                    preset_llm,
                    required_tools=self._expand_requested_tools(spec.get("tools", [])),
                )
            except ValueError as e:
                return {"status": "error",
                        "message": f"preset {preset_name!r}: {e}"}
            resolved_presets.append({
                "name": preset_name,
                "llm": preset_llm,
                "capabilities": preset_caps,
                "preset_schemas": preset_schemas,
                "preset_handlers": preset_handlers,
            })

        cancel_event = threading.Event()
        # Separate event so the watchdog can distinguish timeout from manual
        # reclaim. Watchdog sets BOTH on timeout; reclaim sets only cancel_event.
        # The run loop checks timeout_event first to call mark_timeout vs
        # mark_cancelled.
        timeout_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=len(tasks))
        self._pools.append((pool, cancel_event))

        ids = []
        group_id = DaemonRunDir.new_group_id()
        parent_addr = self._agent._working_dir.name
        parent_pid = os.getpid()

        for i, spec in enumerate(tasks):
            em_id = f"em-{self._next_id}"
            self._next_id += 1
            ids.append(em_id)
            resolved = resolved_presets[i]

            # Build tool surface and system prompt up front so the run_dir
            # records the prompt verbatim before any LLM call. Validation
            # (unknown tools) raises here and aborts before scheduling.
            preset_surface = None
            if resolved is not None:
                preset_surface = (
                    resolved["preset_schemas"],
                    resolved["preset_handlers"],
                )
            task_mcp_clients: list[object] = []
            try:
                task_mcp_regs, task_mcp_catalog = self._task_mcp_registrations(spec)
                mcp_schemas, mcp_handlers, task_mcp_clients = (
                    self._connect_task_mcp_registrations(task_mcp_regs)
                )
                schemas, dispatch = self._build_tool_surface(
                    spec["tools"],
                    preset_surface=preset_surface,
                    mcp_surface=(mcp_schemas, mcp_handlers),
                )
                task_system_prompt = self._task_system_prompt(spec)
                task_skill_catalog = self._task_skill_catalog(spec)
            except Exception as e:
                self._close_task_mcp_clients(task_mcp_clients)
                return {"status": "error", "message": str(e)}
            task_context = self._combine_oneshot_context(
                task_system_prompt, task_skill_catalog, task_mcp_catalog
            )
            system_prompt = self._build_emanation_prompt(
                spec["task"], schemas, system_prompt=task_context
            )

            # Effective model for this emanation (preset overrides if present)
            effective_model = (resolved["llm"]["model"]
                               if resolved else self._default_model)

            # Construct run_dir — creates folder on disk, writes daemon.json,
            # .prompt, .heartbeat, daemon_start event. If FS construction fails,
            # propagate as a tool-level error and skip scheduling for this spec.
            try:
                run_dir = DaemonRunDir(
                    parent_working_dir=self._agent._working_dir,
                    handle=em_id,
                    task=spec["task"],
                    tools=spec["tools"],
                    model=effective_model,
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    parent_addr=parent_addr,
                    parent_pid=parent_pid,
                    system_prompt=system_prompt,
                    group_id=group_id,
                    call_parameters={
                        "task": spec["task"],
                        "tools": spec.get("tools", []),
                        "skills": spec.get("skills", []),
                        "mcp": [self._redact_mcp_registration_for_prompt(r) for r in task_mcp_regs],
                        "system_prompt": task_system_prompt,
                    },
                    log_callback=self._log,
                    preset_name=resolved["name"] if resolved else None,
                    preset_provider=resolved["llm"].get("provider") if resolved else None,
                    preset_model=resolved["llm"].get("model") if resolved else None,
                )
            except OSError as e:
                self._close_task_mcp_clients(task_mcp_clients)
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            future = pool.submit(
                self._run_emanation,
                em_id, run_dir, schemas, dispatch,
                spec["task"], cancel_event, timeout_event,
                resolved["llm"] if resolved else None,
                effective_max_turns,
                task_mcp_clients,
            )
            future.add_done_callback(
                lambda f, eid=em_id, task=spec["task"]:
                    self._on_emanation_done(eid, task, f)
            )
            self._emanations[em_id] = {
                "future": future,
                "task": spec["task"],
                "start_time": time.time(),
                "cancel_event": cancel_event,
                "timeout_event": timeout_event,
                "followup_buffer": "",
                "followup_lock": threading.Lock(),
                "run_dir": run_dir,
            }

        # Start watchdog — sets timeout_event AND cancel_event when timer fires.
        # The lingtai backend spawns no CLI procs, so cli_group_id stays None;
        # the watchdog only flips the cancel/timeout events for the run loops.
        watchdog = threading.Thread(
            target=self._watchdog,
            args=(cancel_event, timeout_event, effective_timeout),
            daemon=True,
        )
        watchdog.start()
        # When every future in this batch finishes, signal cancel so the
        # watchdog returns instead of waking later to do work.
        self._arm_batch_done_cancel(
            [self._emanations[eid]["future"] for eid in ids],
            cancel_event,
        )

        self._log("daemon_emanate", ids=ids, group_id=group_id, count=len(tasks),
                  tasks=[{"task": s["task"][:80], "tools": s["tools"]} for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids,
                "group_id": group_id}

    def _handle_emanate_cli(
        self,
        tasks: list[dict],
        backend: str,
        effective_max_turns: int,
        effective_timeout: float,
    ) -> dict:
        """Dispatch emanations via an external CLI backend.

        Skips preset resolution — the CLI manages its own tools/model/provider.
        Creates a DaemonRunDir for tracking. CLI output is persisted in the
        run directory; only terminal completion/failure emits a compact
        system notification.
        """
        # Pre-flight: validate per-task backend_options BEFORE creating any
        # run_dir or scheduling work, so a single bad spec refuses the whole
        # batch with a clear message instead of leaving half-spawned daemons.
        resolved_backend_argv: list[list[str]] = []
        task_system_prompts: list[str | None] = []
        task_skill_catalogs: list[str | None] = []
        task_mcp_catalogs: list[str | None] = []
        task_mcp_registrations: list[list[dict]] = []
        for i, spec in enumerate(tasks):
            try:
                task_system_prompts.append(self._task_system_prompt(spec))
                task_skill_catalogs.append(self._task_skill_catalog(spec))
                task_mcp_regs, task_mcp_catalog = self._task_mcp_registrations(spec)
                task_mcp_registrations.append(task_mcp_regs)
                task_mcp_catalogs.append(task_mcp_catalog)
            except ValueError as e:
                return {"status": "error",
                        "message": f"tasks[{i}]: {e}"}
            raw_opts = spec.get("backend_options")
            if raw_opts is None:
                resolved_backend_argv.append([])
                continue
            try:
                argv = _backend_options_to_argv(raw_opts)
                _validate_claude_backend_argv(backend, argv)
                resolved_backend_argv.append(argv)
            except ValueError as e:
                return {"status": "error",
                        "message": f"tasks[{i}].backend_options: {e}"}

        cancel_event = threading.Event()
        timeout_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=len(tasks))
        self._pools.append((pool, cancel_event))

        ids = []
        group_id = DaemonRunDir.new_group_id()
        parent_addr = self._agent._working_dir.name
        parent_pid = os.getpid()

        for i, spec in enumerate(tasks):
            em_id = f"em-{self._next_id}"
            self._next_id += 1
            ids.append(em_id)
            backend_argv = resolved_backend_argv[i]
            backend_options = spec.get("backend_options") or None

            task_system_prompt = task_system_prompts[i]
            task_skill_catalog = task_skill_catalogs[i]
            task_mcp_catalog = task_mcp_catalogs[i]
            task_mcp_regs = task_mcp_registrations[i]
            task_context = self._combine_oneshot_context(
                task_system_prompt, task_skill_catalog, task_mcp_catalog
            )
            system_prompt = f"[{backend} backend — task delegated to external CLI]"
            if task_context:
                system_prompt += (
                    "\n\nParent-provided daemon context (oneshot):\n"
                    + task_context
                )
            cli_task = self._compose_cli_task(spec["task"], task_context)
            try:
                run_dir = DaemonRunDir(
                    parent_working_dir=self._agent._working_dir,
                    handle=em_id,
                    task=spec["task"],
                    tools=spec.get("tools", []),
                    model=backend,
                    max_turns=effective_max_turns,
                    timeout_s=effective_timeout,
                    parent_addr=parent_addr,
                    parent_pid=parent_pid,
                    system_prompt=system_prompt,
                    group_id=group_id,
                    call_parameters={
                        "task": spec["task"],
                        "tools": spec.get("tools", []),
                        "skills": spec.get("skills", []),
                        "mcp": [self._redact_mcp_registration_for_prompt(r) for r in task_mcp_regs],
                        "system_prompt": task_system_prompt,
                        "backend_options": backend_options,
                    },
                    log_callback=self._log,
                    backend=backend,
                )
            except OSError as e:
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            # Persist the resolved options into daemon.json for observability.
            # The raw object (what the agent passed) goes alongside the
            # converted argv tokens so the run is fully reconstructible from
            # the run dir.
            if backend_options is not None or backend_argv:
                run_dir._state["backend_options"] = backend_options
                run_dir._state["backend_argv"] = list(backend_argv)
                run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log("daemon_backend_options",
                      em_id=em_id, backend=backend,
                      argv=list(backend_argv))

            if backend == "codex":
                run_fn = self._run_codex_emanation
            elif backend == "opencode":
                run_fn = self._run_opencode_emanation
            elif backend == "mimocode":
                run_fn = self._run_mimocode_emanation
            elif backend == "qwen-code":
                run_fn = self._run_qwen_code_emanation
            elif backend == "oh-my-pi":
                run_fn = self._run_oh_my_pi_emanation
            elif backend == "cursor":
                run_fn = self._run_cursor_emanation
            elif backend in ("claude", "claude-interactive"):
                run_fn = self._run_claude_interactive_emanation
            else:
                # ``claude-p`` is the new explicit name for the existing
                # print-mode backend; ``claude-code`` remains a compatibility
                # alias for older callers and stored daemon entries.
                run_fn = self._run_claude_code_emanation
            future = pool.submit(
                run_fn,
                em_id, run_dir, cli_task,
                cancel_event, timeout_event,
                backend_argv,
            )
            future.add_done_callback(
                lambda f, eid=em_id, task=spec["task"]:
                    self._on_emanation_done(eid, task, f)
            )
            self._emanations[em_id] = {
                "future": future,
                "task": spec["task"],
                "start_time": time.time(),
                "cancel_event": cancel_event,
                "timeout_event": timeout_event,
                "followup_buffer": "",
                "followup_lock": threading.Lock(),
                "run_dir": run_dir,
                "backend": backend,
                # Tracks whether a CLI `ask` follow-up is currently being
                # streamed in the background. Set/cleared by the ask worker
                # under `followup_lock`; checked by `_handle_ask_cli` /
                # `_handle_ask_codex` to refuse a second concurrent ask
                # against the same session (the `claude --resume` /
                # `codex exec resume` CLIs serialize per-session and a second
                # spawn would either error or interleave).
                "ask_in_flight": False,
                "ask_future": None,
            }

        # Start watchdog — scoped to this batch's CLI procs (group_id) so an
        # earlier batch's timeout can never kill this one's subprocesses.
        watchdog = threading.Thread(
            target=self._watchdog,
            args=(cancel_event, timeout_event, effective_timeout),
            kwargs={"cli_group_id": group_id},
            daemon=True,
        )
        watchdog.start()
        # When every future in this batch finishes, signal cancel so the
        # watchdog returns instead of waking later to do work.
        self._arm_batch_done_cancel(
            [self._emanations[eid]["future"] for eid in ids],
            cancel_event,
        )

        self._log("daemon_emanate", ids=ids, group_id=group_id, count=len(tasks), backend=backend,
                  tasks=[{"task": s["task"][:80], "tools": s.get("tools", [])}
                         for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids,
                "group_id": group_id, "backend": backend}

    @staticmethod
    def _truncate_list_string(value: object, limit: int = 500) -> object:
        if not isinstance(value, str):
            return value
        if len(value) <= limit:
            return value
        return value[:limit] + "…[truncated]"

    @staticmethod
    def _list_search_blob(info: dict) -> str:
        try:
            return json.dumps(info, ensure_ascii=False, sort_keys=True).lower()
        except (TypeError, ValueError):
            return str(info).lower()

    def _daemon_prompt_preview(self, run_path: Path, limit: int = 500) -> tuple[str | None, int | None]:
        prompt_path = run_path / ".prompt"
        try:
            size = prompt_path.stat().st_size
            with open(prompt_path, encoding="utf-8") as f:
                text = f.read(limit + 1)
        except (OSError, UnicodeDecodeError):
            return None, None
        return self._truncate_list_string(text, limit), size

    def _daemon_list_entry_from_state(
        self,
        state: dict,
        run_path: Path,
        *,
        active_status: str | None = None,
        active_elapsed: int | None = None,
        active_error: BaseException | None = None,
    ) -> dict:
        status = active_status or state.get("state") or "unknown"
        call_params = state.get("call_parameters")
        if not isinstance(call_params, dict):
            call_params = {}
        prompt_preview, prompt_chars = self._daemon_prompt_preview(run_path)
        visible_call_params = {
            "task": self._truncate_list_string(call_params.get("task", state.get("task"))),
            "tools": call_params.get("tools", state.get("tools", [])),
            "skills": call_params.get("skills", []),
            "mcp": call_params.get("mcp", []),
            "system_prompt_preview": self._truncate_list_string(call_params.get("system_prompt")),
        }
        visible_call_params = {k: v for k, v in visible_call_params.items() if v not in (None, [], "")}
        info = {
            "id": state.get("handle"),
            "task": self._truncate_list_string(state.get("task", ""), 120),
            "status": status,
            "data_version": state.get("data_version"),
            "migration": state.get("migration"),
            "elapsed_s": active_elapsed if active_elapsed is not None else state.get("elapsed_s"),
            "run_id": state.get("run_id"),
            "group_id": state.get("group_id"),
            "backend": state.get("backend"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "path": str(run_path),
            "result_preview": state.get("result_preview"),
            "result_path": state.get("result_path"),
            "call_parameters": visible_call_params,
        }
        if prompt_preview is not None:
            info["system_prompt_preview"] = prompt_preview
            info["system_prompt_bytes"] = prompt_chars
            info["system_prompt_path"] = str(run_path / ".prompt")
        if active_error is not None:
            info["error"] = str(active_error)
        elif state.get("error"):
            info["error"] = state.get("error")
        return {k: v for k, v in info.items() if v is not None}

    @staticmethod
    def _utc_iso_from_timestamp(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _started_at_from_run_id(run_id: str) -> str | None:
        match = re.match(r"^em-\d+-(\d{8}-\d{6})-[0-9a-fA-F]+$", run_id)
        if not match:
            return None
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _handle_from_run_id(run_id: str) -> str | None:
        match = re.match(r"^(em-\d+)-", run_id)
        return match.group(1) if match else None

    @staticmethod
    def _atomic_write_daemon_json(path: Path, state: dict) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _looks_like_daemon_run_dir(run_path: Path) -> bool:
        return (
            run_path.is_dir()
            and (
                run_path.name.startswith("em-")
                or (run_path / ".prompt").exists()
                or (run_path / "result.txt").exists()
                or (run_path / "logs" / "events.jsonl").exists()
            )
        )

    def _read_daemon_events_tail(self, run_path: Path, max_lines: int = 80) -> list[dict]:
        events_path = run_path / "logs" / "events.jsonl"
        try:
            size = events_path.stat().st_size
            with open(events_path, "rb") as f:
                f.seek(max(0, size - 65536))
                raw = f.read()
            text = raw.decode("utf-8", errors="replace")
            lines = text.splitlines()[-max_lines:]
        except OSError:
            return []
        events: list[dict] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _infer_task_from_prompt(self, run_path: Path) -> str | None:
        prompt_path = run_path / ".prompt"
        try:
            with open(prompt_path, encoding="utf-8") as f:
                text = f.read(20000)
        except (OSError, UnicodeDecodeError):
            return None
        markers = ["\nYour task:\n", "\nTask:\n"]
        best = None
        best_idx = -1
        for marker in markers:
            idx = text.rfind(marker)
            if idx > best_idx:
                best = marker
                best_idx = idx
        if best is None or best_idx < 0:
            return None
        task = text[best_idx + len(best):].strip()
        if not task:
            return None
        return str(self._truncate_list_string(task, 2000))

    def _infer_terminal_state_from_events(self, events: list[dict]) -> tuple[str | None, str | None, object | None]:
        for event in reversed(events):
            name = event.get("event")
            if name == "daemon_done":
                return "done", event.get("ts"), None
            if name == "daemon_error":
                error = {
                    "type": event.get("exception") or "DaemonError",
                    "message": event.get("message") or "daemon failed",
                }
                return "failed", event.get("ts"), error
            if name == "daemon_cancelled":
                return "cancelled", event.get("ts"), None
            if name == "daemon_timeout":
                return "timeout", event.get("ts"), None
        return None, None, None

    def _result_preview_from_file(self, run_path: Path) -> tuple[str | None, str | None]:
        result_path = run_path / "result.txt"
        try:
            with open(result_path, encoding="utf-8") as f:
                text = f.read(201)
        except (OSError, UnicodeDecodeError):
            return None, None
        preview = text[:200]
        return preview, str(result_path)

    def _build_reconstructed_daemon_state(
        self,
        run_path: Path,
        existing_state: dict | None,
        *,
        reason: str,
    ) -> dict:
        old = dict(existing_state or {})
        run_id = str(old.get("run_id") or run_path.name)
        handle = str(old.get("handle") or self._handle_from_run_id(run_id) or run_id.split("-")[0] or run_id)
        events = self._read_daemon_events_tail(run_path)
        inferred_state, inferred_finished_at, inferred_error = self._infer_terminal_state_from_events(events)
        result_preview, result_path = self._result_preview_from_file(run_path)
        task = old.get("task")
        call_params = old.get("call_parameters") if isinstance(old.get("call_parameters"), dict) else {}
        if not isinstance(task, str) or not task:
            task = call_params.get("task") if isinstance(call_params.get("task"), str) else None
        if not task:
            task = self._infer_task_from_prompt(run_path) or ""
        tools = old.get("tools")
        if not isinstance(tools, list):
            tools = call_params.get("tools") if isinstance(call_params.get("tools"), list) else []
        daemon_state = old.get("state") if isinstance(old.get("state"), str) else None
        if inferred_state and daemon_state in (None, "", "running", "active", "unknown"):
            daemon_state = inferred_state
        if result_path and daemon_state in (None, "", "running", "active", "unknown"):
            daemon_state = "done"
        if not daemon_state:
            daemon_state = "unknown"
        started_at = old.get("started_at") if isinstance(old.get("started_at"), str) else None
        if not started_at:
            started_at = self._started_at_from_run_id(run_id)
        if not started_at:
            try:
                started_at = self._utc_iso_from_timestamp(run_path.stat().st_mtime)
            except OSError:
                started_at = DaemonRunDir._now_iso()
        finished_at = old.get("finished_at") if isinstance(old.get("finished_at"), str) else None
        if not finished_at and daemon_state in {"done", "failed", "cancelled", "timeout"}:
            finished_at = inferred_finished_at
            if not finished_at:
                terminal_path = Path(result_path) if result_path else (run_path / "logs" / "events.jsonl")
                try:
                    finished_at = self._utc_iso_from_timestamp(terminal_path.stat().st_mtime)
                except OSError:
                    finished_at = DaemonRunDir._now_iso()
        if not isinstance(call_params, dict):
            call_params = {}
        if task and not call_params.get("task"):
            call_params["task"] = task
        if tools and not call_params.get("tools"):
            call_params["tools"] = tools
        state = {
            "data_version": DaemonRunDir.DATA_VERSION,
            "handle": handle,
            "run_id": run_id,
            "group_id": old.get("group_id"),
            "parent_addr": old.get("parent_addr"),
            "parent_pid": old.get("parent_pid"),
            "task": task,
            "tools": tools,
            "call_parameters": call_params,
            "model": old.get("model") or old.get("preset_model") or "unknown",
            "max_turns": old.get("max_turns"),
            "timeout_s": old.get("timeout_s"),
            "state": daemon_state,
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_s": old.get("elapsed_s") if old.get("elapsed_s") is not None else 0.0,
            "turn": old.get("turn") if old.get("turn") is not None else 0,
            "current_tool": old.get("current_tool"),
            "tool_call_count": old.get("tool_call_count") if old.get("tool_call_count") is not None else 0,
            "tokens": old.get("tokens") if isinstance(old.get("tokens"), dict) else {"input": 0, "output": 0, "thinking": 0, "cached": 0},
            "result_preview": old.get("result_preview") or result_preview,
            "result_path": old.get("result_path") or result_path,
            "last_output": old.get("last_output"),
            "last_output_at": old.get("last_output_at"),
            "error": old.get("error") or inferred_error,
            "preset_name": old.get("preset_name"),
            "preset_provider": old.get("preset_provider"),
            "preset_model": old.get("preset_model"),
            "backend": old.get("backend") or "unknown",
            "claude_session_id": old.get("claude_session_id"),
            "codex_session_id": old.get("codex_session_id"),
            "opencode_session_id": old.get("opencode_session_id"),
            "mimocode_session_id": old.get("mimocode_session_id"),
            "oh_my_pi_session_id": old.get("oh_my_pi_session_id"),
            "cursor_session_id": old.get("cursor_session_id"),
            "migration": {
                "reason": reason,
                "rebuilt_at": DaemonRunDir._now_iso(),
                "source": "daemon_list_best_effort",
            },
        }
        # Preserve fields added by specific backends or future versions (for
        # example backend_options/backend_argv/session ids) while still
        # normalizing the fields list relies on above.  data_version itself is
        # deliberately overwritten to the current version.
        for key, value in old.items():
            if key != "data_version" and key not in state:
                state[key] = value
        return state

    @staticmethod
    def _has_current_daemon_data_version(state: dict) -> bool:
        version = state.get("data_version")
        return (
            isinstance(version, int)
            and not isinstance(version, bool)
            and version == DaemonRunDir.DATA_VERSION
        )

    def _load_or_rebuild_daemon_state(self, run_path: Path) -> dict | None:
        daemon_json_path = run_path / "daemon.json"
        existing: dict | None = None
        reason: str | None = None
        try:
            loaded = json.loads(daemon_json_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
            else:
                reason = "daemon_json_not_object"
        except FileNotFoundError:
            reason = "daemon_json_missing"
        except (json.JSONDecodeError, UnicodeDecodeError):
            reason = "daemon_json_invalid"
        except OSError:
            return None
        if reason is None and existing is not None:
            if self._has_current_daemon_data_version(existing):
                return existing
            reason = "daemon_json_data_version_mismatch"
        rebuilt = self._build_reconstructed_daemon_state(run_path, existing, reason=reason or "daemon_json_rebuild")
        try:
            self._atomic_write_daemon_json(daemon_json_path, rebuilt)
        except OSError:
            pass
        return rebuilt

    def _iter_daemon_history_states(self, skip_run_ids: set[str] | None = None) -> list[tuple[Path, dict]]:
        daemons_dir = self._agent._working_dir / "daemons"
        if not daemons_dir.is_dir():
            return []
        skip_run_ids = skip_run_ids or set()
        rows: list[tuple[Path, dict]] = []
        for run_path in daemons_dir.iterdir():
            # Active runs are represented by their live DaemonRunDir object above.
            # Do not lazy-rebuild their daemon.json here: an active writer thread
            # may be updating it concurrently, and the live state is fresher.
            if run_path.name in skip_run_ids:
                continue
            if not self._looks_like_daemon_run_dir(run_path):
                continue
            state = self._load_or_rebuild_daemon_state(run_path)
            if isinstance(state, dict):
                rows.append((run_path, state))
        return rows

    def _handle_list(
        self,
        contains: str | None = "",
        status_filter: str | None = "all",
        include_done: bool = True,
        limit: int | None = None,
    ) -> dict:
        try:
            limit_int = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            return {"status": "error", "message": f"last must be a positive integer (got {limit!r})"}
        if limit_int is not None and limit_int < 1:
            return {"status": "error", "message": f"last must be ≥ 1 (got {limit_int})"}
        if limit_int is not None:
            limit_int = min(limit_int, self._CHECK_LAST_MAX)

        query = (contains or "").strip().lower()
        wanted_status = (status_filter or "all").strip().lower()
        include_done = include_done is not False

        emanations: list[dict] = []
        running = 0
        active_run_ids: set[str] = set()

        for em_id, entry in self._emanations.items():
            elapsed = time.time() - entry["start_time"]
            future = entry["future"]
            exc = None
            if future.done():
                exc = future.exception()
                status = "failed" if exc else "done"
            else:
                status = "running"
                running += 1
            run_dir = entry.get("run_dir")
            if run_dir is not None:
                state = run_dir.state_snapshot()
                state.setdefault("handle", em_id)
                active_run_ids.add(run_dir.run_id)
                info = self._daemon_list_entry_from_state(
                    state, run_dir.path, active_status=status,
                    active_elapsed=round(elapsed), active_error=exc,
                )
            else:
                info = {
                    "id": em_id,
                    "task": self._truncate_list_string(entry.get("task", ""), 120),
                    "status": status,
                    "elapsed_s": round(elapsed),
                }
                if exc:
                    info["error"] = str(exc)
            emanations.append(info)

        if include_done:
            for run_path, state in self._iter_daemon_history_states(skip_run_ids=active_run_ids):
                info = self._daemon_list_entry_from_state(state, run_path)
                if info.get("status") == "running":
                    running += 1
                emanations.append(info)

        total_before_filter = len(emanations)
        if wanted_status and wanted_status != "all":
            emanations = [e for e in emanations if str(e.get("status", "")).lower() == wanted_status]
        if query:
            emanations = [e for e in emanations if query in self._list_search_blob(e)]

        def _sort_key(item: dict) -> str:
            return str(item.get("started_at") or item.get("run_id") or "")
        emanations.sort(key=_sort_key, reverse=True)
        total_matches = len(emanations)
        if limit_int is not None:
            emanations = emanations[:limit_int]
        return {
            "emanations": emanations,
            "running": running,
            "max_emanations": self._max_emanations,
            "history_included": include_done,
            "index": "daemon_run_dirs",
            "total_before_filter": total_before_filter,
            "total_matches": total_matches,
            "showing": len(emanations),
        }

    def _handle_ask(self, em_id: str, message: str) -> dict:
        entry = self._emanations.get(em_id)
        if not entry:
            return {"status": "error", "message": f"Unknown emanation: {em_id}"}

        # CLI backends with resumable sessions:
        #   - claude / claude-interactive: interactive `claude --resume ...`
        #   - claude-p / claude-code:      `claude --resume ... --print`
        #   - codex:                       `codex exec resume <codex_session_id>`
        #   - opencode:                    `opencode run --session <opencode_session_id> ...`
        #   - mimocode:                    `mimo run --session <mimocode_session_id> ...`
        #   - oh-my-pi:                    `omp --mode json --session <oh_my_pi_session_id> ...`
        #   - cursor:                      `agent -p --resume <cursor_session_id> ...`
        # Qwen Code headless mode does not expose a stable resume contract here.
        # All stream progress into the daemon run directory so
        # `daemon(check)` shows live progress.
        backend = entry.get("backend")
        if backend in ("claude", "claude-interactive"):
            return self._handle_ask_claude_interactive(em_id, entry, message)
        if backend in ("claude-p", "claude-code"):
            return self._handle_ask_cli(em_id, entry, message)
        if backend == "codex":
            return self._handle_ask_codex(em_id, entry, message)
        if backend == "opencode":
            return self._handle_ask_opencode(em_id, entry, message)
        if backend == "mimocode":
            return self._handle_ask_mimocode(em_id, entry, message)
        if backend == "oh-my-pi":
            return self._handle_ask_oh_my_pi(em_id, entry, message)
        if backend == "qwen-code":
            return {"status": "error", "id": em_id,
                    "message": "qwen-code daemon backend does not support daemon(action='ask') yet; start a new qwen-code emanation instead."}
        if backend == "cursor":
            return self._handle_ask_cursor(em_id, entry, message)

        if entry["future"].done():
            return {"status": "error", "message": "not running"}
        with entry["followup_lock"]:
            if entry["followup_buffer"]:
                entry["followup_buffer"] += "\n\n" + message
            else:
                entry["followup_buffer"] = message
        self._log("daemon_ask", em_id=em_id, message_length=len(message))
        return {"status": "sent", "id": em_id}

    def _handle_ask_claude_interactive(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch an interactive Claude ``--resume`` follow-up asynchronously."""
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("claude_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No claude session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        try:
            run_dir.record_cli_output(
                f"[interactive ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_claude_interactive_stream,
            em_id, entry, message, session_id, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future
        return {"status": "sent", "id": em_id, "async": True,
                "message": "interactive ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_claude_interactive_stream(
        self,
        em_id: str,
        entry: dict,
        message: str,
        session_id: str,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker for interactive Claude ``--resume`` follow-ups."""
        ask_cancel = threading.Event()
        ask_timeout = threading.Event()
        parent_cancel = entry.get("cancel_event")
        monitor_done = threading.Event()

        def _timeout() -> None:
            ask_timeout.set()
            ask_cancel.set()

        def _mirror_parent_cancel() -> None:
            if parent_cancel is None:
                return
            while not monitor_done.is_set():
                if parent_cancel.is_set():
                    ask_cancel.set()
                    return
                monitor_done.wait(0.05)

        timer = threading.Timer(self._timeout, _timeout)
        timer.daemon = True
        timer.start()
        monitor = threading.Thread(
            target=_mirror_parent_cancel,
            daemon=True,
            name=f"daemon-claude-interactive-ask-cancel-{em_id}",
        )
        monitor.start()
        try:
            try:
                result = run_claude_interactive(
                    em_id=em_id,
                    run_dir=run_dir,
                    working_dir=self._agent._working_dir,
                    task=message,
                    cancel_event=ask_cancel,
                    timeout_event=ask_timeout,
                    resume_session_id=session_id,
                    env=_claude_code_env(),
                    log_callback=self._log,
                )
            except Exception as e:
                err = f"interactive claude ask failed: {e}"
                self._publish_followup_if_live(
                    em_id, status="follow-up failed", text=err, run_dir=run_dir,
                )
                return {"status": "error", "id": em_id, "message": err}
            if ask_timeout.is_set():
                err = f"interactive claude ask timed out after {self._timeout}s"
                self._publish_followup_if_live(
                    em_id, status="follow-up failed", text=err, run_dir=run_dir,
                )
                return {"status": "error", "id": em_id, "message": err}
            output = (result.final_text or "").strip()
            if output:
                self._publish_followup_if_live(
                    em_id, status="follow-up completed", text=output, run_dir=run_dir,
                )
            return {"status": "sent", "id": em_id, "output": output}
        finally:
            timer.cancel()
            monitor_done.set()
            monitor.join(timeout=0.2)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

    def _handle_ask_cli(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Claude Code `--resume` follow-up off the caller's turn.

        Returns immediately after spawning the subprocess; the stream-json
        parse runs in ``self._ask_pool``. Progress + final reply still land
        in ``run_dir`` (``cli_output`` events, ``last_output``, and a
        ``follow-up completed`` notification on success), so ``daemon(check)``
        observes the ask just as it did when this method was synchronous.

        Refuses a second concurrent ask against the same emanation with
        ``status="busy"`` — ``claude --resume`` serializes per-session and
        a second spawn would either error or interleave reply text.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("claude_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No claude session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        # Concurrent-ask guard. Checked + set under followup_lock so two
        # parent tool calls racing on the same em_id can't both spawn.
        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "claude",
            "--resume", session_id,
            "--print",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
            message,
        ]
        self._log("daemon_claude_code_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                env=_claude_code_env(),
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'claude' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start claude CLI: {e}"}
        # Ask follow-ups are not part of any batch; track globally only so
        # reclaim-all still kills them, but no batch watchdog owns them.
        self._register_cli_proc(proc, group_id=None)

        # Surface that an ask just started so `daemon(check)` shows it
        # immediately, even before any stream-json event arrives.
        # record_cli_output already routes its filesystem writes through
        # _safe (which catches OSError); the outer guard here is only for
        # the unlikely case the call site itself raises (e.g. attribute
        # access on a torn-down run_dir). Narrowed to OSError so real bugs
        # propagate.
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_claude_code_stream, em_id, entry, proc, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_claude_code_stream(
        self,
        em_id: str,
        entry: dict,
        proc: subprocess.Popen,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream a Claude Code `--resume` subprocess.

        Same stream-json parse as ``_run_claude_code_emanation``. Always
        clears ``ask_in_flight`` and detaches ``proc`` from ``_cli_procs``.
        Return value is captured by the future for tests/debugging; the
        agent observes the result through the run_dir + notification.
        """
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-claude-ask-stderr-{em_id}",
        )
        stderr_thread.start()

        final_result_text: str | None = None
        final_is_error = False
        timed_out = False

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + self._timeout
            # _iter_stdout_with_deadline returns on EOF *or* deadline;
            # we distinguish the two by checking the clock afterwards.
            # This is the core fix for the silent-subprocess hang — the
            # old `for raw_line in proc.stdout` blocked the worker thread
            # indefinitely if the resumed CLI never wrote a newline.
            for raw_line in _iter_stdout_with_deadline(
                proc, deadline,
                thread_name=f"daemon-claude-ask-stdout-{em_id}",
            ):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "assistant":
                    message_obj = event.get("message") or {}
                    for block in (message_obj.get("content") or []):
                        if block.get("type") == "text":
                            text = block.get("text") or ""
                            if text.strip():
                                run_dir.record_cli_output(text, stream="stdout")
                elif etype == "result":
                    final_result_text = event.get("result") or ""
                    final_is_error = bool(event.get("is_error"))
                    # Accumulate the follow-up's token usage into
                    # daemon.json.cli_tokens for UI display — same UI-only,
                    # never-ledger policy as the initial emanation run.
                    usage = _normalize_claude_usage(event.get("usage"))
                    if usage is not None:
                        try:
                            run_dir.record_cli_tokens(
                                input=usage["input"], output=usage["output"],
                                cached=usage["cached"],
                                thinking=usage["thinking"],
                                raw=event.get("usage"),
                            )
                        except Exception:
                            pass

            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
            else:
                # Reader hit EOF before the deadline. The CLI usually exits
                # within milliseconds of closing stdout, but bound the wait
                # so a misbehaving child can't strand us here.
                try:
                    proc.wait(timeout=max(1.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group(proc)
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=None)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"claude --resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if proc.returncode != 0:
            detail = stderr_tail or (final_result_text or "")
            err = f"claude CLI exited {proc.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_is_error:
            err = (f"claude CLI reported is_error=true: "
                   f"{(final_result_text or stderr_tail)[-500:]}")
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        output = (final_result_text or "").strip()
        if output:
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    def _handle_ask_codex(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Codex ``exec resume`` follow-up off the caller's turn.

        Mirrors ``_handle_ask_cli``: spawn, register the proc, hand the
        JSONL stream parse to ``self._ask_pool``, return immediately.
        Concurrent-ask guard is the same — ``codex exec resume`` is
        single-writer per session.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("codex_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No codex session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            message,
        ]
        self._log("daemon_codex_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'codex' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start codex CLI: {e}"}
        # Ask follow-ups are not part of any batch (see claude-code ask).
        self._register_cli_proc(proc, group_id=None)

        # See _handle_ask_cli for the rationale on the narrowed except.
        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_codex_stream, em_id, entry, proc, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_codex_stream(
        self,
        em_id: str,
        entry: dict,
        proc: subprocess.Popen,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream a ``codex exec resume`` subprocess.

        Same JSONL event vocabulary as ``_run_codex_emanation``:
        ``item.completed/agent_message`` for reply text, ``turn.completed``
        for terminal acknowledgement. Always clears ``ask_in_flight`` and
        detaches ``proc`` from ``_cli_procs``.
        """
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-codex-ask-stderr-{em_id}",
        )
        stderr_thread.start()

        agent_message_texts: list[str] = []
        turn_completed = False
        timed_out = False

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + self._timeout
            # See _run_ask_claude_code_stream for the rationale on
            # _iter_stdout_with_deadline — fixes the silent-CLI hang.
            for raw_line in _iter_stdout_with_deadline(
                proc, deadline,
                thread_name=f"daemon-codex-ask-stdout-{em_id}",
            ):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                etype = event.get("type")
                if etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text.strip():
                            agent_message_texts.append(text)
                            run_dir.record_cli_output(text, stream="stdout")
                elif etype == "turn.completed":
                    turn_completed = True

            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
            else:
                try:
                    proc.wait(timeout=max(1.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group(proc)
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=None)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"codex exec resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(agent_message_texts[-3:])
            err = f"codex CLI exited {proc.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if not turn_completed and not agent_message_texts:
            err = (f"codex exec resume produced no turn.completed event: "
                   f"{(stderr_tail or '[no output]')[-500:]}")
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        output = "\n".join(agent_message_texts).strip()
        if output:
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    # ------------------------------------------------------------------
    # OpenCode backend (opencode-ai CLI, `opencode run --format json`)
    # ------------------------------------------------------------------

    # OpenCode emits one JSON object per stdout line under ``--format json``.
    # The event vocabulary is less standardized than claude-code / codex —
    # field names vary by event family and version — so the parser is
    # intentionally defensive: it pulls text from any of several common
    # shapes and captures the session id from whichever event carries it
    # first. Unknown / non-JSON lines are still surfaced as cli_output so
    # nothing is lost.
    _OPENCODE_SESSION_FIELDS = (
        "session_id", "sessionID", "sessionId", "session",
        "thread_id", "threadId",
    )

    def _build_opencode_prompt(self, task: str) -> str:
        """Compose the initial prompt sent to ``opencode run``.

        OpenCode is being used as a one-shot daemon worker, not as an
        interactive session, so we wrap the user task with a short
        operating contract: write detailed work product to files in the
        parent working directory, and end with a concise final answer
        the parent agent can read at a glance.
        """
        return (
            "You are running as a LingTai daemon — a disposable subagent "
            "spawned by a parent LingTai agent to perform one task and "
            "report back.\n\n"
            "Operating contract:\n"
            "1. Do the task in the current working directory.\n"
            "2. If the answer is long, structured, or includes code, "
            "write the detailed output to a file (e.g. report.md, "
            "result.json) and reference it in your final answer.\n"
            "3. End with a concise final answer (a few short paragraphs "
            "or bullet points) summarising what you did and where to "
            "look for the full result.\n"
            "4. Do not ask the operator for clarification — make the "
            "best reasonable assumption and proceed.\n\n"
            f"Task:\n{task}"
        )

    @staticmethod
    def _opencode_extract_session_id(event: dict) -> str | None:
        """Pull a session-id-shaped string out of an opencode JSON event.

        OpenCode's event field naming is unstable across versions: a
        session-created style event may use ``session_id``, ``sessionID``,
        ``sessionId``, or a nested ``session.id``. Be defensive over all
        of them. Returns the first non-empty string found, or None.
        """
        for key in DaemonManager._OPENCODE_SESSION_FIELDS:
            val = event.get(key)
            if isinstance(val, str) and val:
                return val
            if isinstance(val, dict):
                inner = val.get("id") or val.get("session_id") or val.get("sessionID")
                if isinstance(inner, str) and inner:
                    return inner
        # A bare top-level ``id`` is commonly an event/message id. Only treat
        # it as a session id when the event type is explicitly session-shaped.
        etype = event.get("type")
        if isinstance(etype, str) and "session" in etype.lower():
            val = event.get("id")
            if isinstance(val, str) and val:
                return val
        # Some opencode builds emit a ``data`` envelope on session events.
        data = event.get("data")
        if isinstance(data, dict):
            return DaemonManager._opencode_extract_session_id(data)
        return None

    @staticmethod
    def _opencode_extract_text(event: dict) -> str:
        """Best-effort text extraction from an opencode JSON event.

        Tries a handful of common shapes (top-level ``text`` / ``content``
        / ``message`` / ``delta``, content-block lists similar to
        Anthropic's, and Codex-style ``item.text``) and returns the first
        non-empty string. Returns "" when no text is present (events
        that are purely structural, e.g. tool calls, are skipped).
        """
        # Top-level scalar text fields.
        for key in ("text", "content", "message", "delta", "answer", "output", "result"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return val
        # Content-block list (Anthropic-style).
        msg = event.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        t = block.get("text")
                        if isinstance(t, str) and t.strip():
                            parts.append(t)
                if parts:
                    return "\n".join(parts)
            elif isinstance(content, str) and content.strip():
                return content
        # Codex-style item.
        item = event.get("item")
        if isinstance(item, dict):
            t = item.get("text")
            if isinstance(t, str) and t.strip():
                return t
        return ""

    def _run_opencode_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        *,
        executable: str = "opencode",
        backend_name: str = "opencode",
        session_state_key: str = "opencode_session_id",
        cmd_prefix: list[str] | None = None,
    ) -> str:
        """Run an OpenCode-family CLI session as the emanation backend.

        Spawns ``<executable> <cmd_prefix...> <backend_argv...> <prompt>`` and
        parses one JSON event per stdout line (``cmd_prefix`` defaults to
        ``["run", "--format", "json"]`` for OpenCode/MiMo; Oh-My-Pi passes
        ``["--mode", "json", "--approval-mode", "yolo"]``). Non-JSON lines are recorded
        as ``cli_output`` so nothing is silently dropped. The first event that
        carries a session-id-shaped field is stored in daemon.json under
        ``session_state_key`` (``opencode_session_id`` by default) — used later
        by ``daemon(action='ask')`` to resume the session.

        OpenCode-family event field naming is less standardized than
        claude-code or codex, so the parser is intentionally permissive.
        See ``_opencode_extract_text`` / ``_opencode_extract_session_id``
        for the shapes accepted.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        prompt = self._build_opencode_prompt(task)

        # Required infrastructure flags come first; free-form
        # backend_options sit between them and the prompt positional so the
        # prompt stays the trailing argument the CLI expects.
        prefix = cmd_prefix if cmd_prefix is not None else ["run", "--format", "json"]
        cmd = [executable, *prefix]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(prompt)
        self._log(f"daemon_{backend_name}_start", em_id=em_id,
                  cmd_head=" ".join(cmd[:1 + len(prefix)]))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            exc = RuntimeError(f"'{executable}' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start {backend_name} CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        self._register_cli_proc(proc, group_id=run_dir.group_id)

        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-{backend_name}-stderr-{em_id}",
        )
        stderr_thread.start()

        session_id_captured: str | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if not sid:
                return
            # The session id is established by the first session-shaped header.
            # Later OpenCode-family/Oh-My-Pi events may carry their own event ids;
            # do not let those overwrite a working resume id.
            if session_id_captured:
                return
            session_id_captured = sid
            run_dir._state[session_state_key] = sid
            run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log(f"daemon_{backend_name}_session", em_id=em_id, session_id=sid)

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if cancel_event.is_set():
                    _kill_process_group(proc)
                    return _exit_cancelled()

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON line — record verbatim so the agent can
                    # still see banner / progress text that opencode
                    # didn't structure as an event.
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                sid = self._opencode_extract_session_id(event)
                if sid:
                    _store_session_id(sid)

                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")

                # Capture a definitive final answer if the event signals
                # completion. OpenCode's "final" event names vary; we
                # accept any event whose ``type`` ends in a terminal-ish
                # token. Last-text-wins so a later result overrides
                # intermediate streaming.
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    if low.endswith((".completed", ".done", ".finished",
                                     "result", "final")):
                        if text:
                            final_text = text

            proc.wait()
        except Exception as e:
            _kill_process_group(proc)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=run_dir.group_id)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            attributed = self._attributed_cli_exit(
                proc, backend_name, detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"{backend_name} CLI exited with code {proc.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        # Choose the best final text: explicit terminal event > last text
        # chunk > stderr tail > no-output sentinel. ``any_event`` lets us
        # distinguish "process exited 0 but never spoke" from a real
        # silent success (which shouldn't happen, but be defensive).
        if final_text is not None:
            text = final_text.strip()
        elif text_chunks:
            text = text_chunks[-1].strip()
        elif stderr_tail:
            text = f"[no JSON events; stderr tail follows]\n{stderr_tail[-500:]}"
        else:
            text = "[no output]"
        if not any_event and not stderr_tail:
            text = "[no output]"

        run_dir.mark_done(text)
        return text

    def _run_mimocode_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run a MiMo Code CLI session as the emanation backend.

        MiMo Code's npm package ``@mimo-ai/cli`` exposes the ``mimo``
        executable and an OpenCode-derived ``run --format json`` command, so
        the existing defensive OpenCode JSONL parser is reused with a distinct
        session-id field in daemon.json.
        """
        return self._run_opencode_emanation(
            em_id, run_dir, task, cancel_event, timeout_event, backend_argv,
            executable="mimo",
            backend_name="mimocode",
            session_state_key="mimocode_session_id",
        )

    def _run_oh_my_pi_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run an Oh-My-Pi (``omp``) CLI session as the emanation backend.

        Oh-My-Pi's npm package ``@oh-my-pi/pi-coding-agent`` exposes the
        ``omp`` executable. ``--mode json`` makes it a non-interactive JSON
        event-stream printer (it first emits a ``type:session`` header whose
        ``id`` is the resumable session id, then one agent event per JSONL
        line); ``--approval-mode yolo`` lets the daemon proceed without interactive
        approval prompts. The OpenCode-family JSON parser is reused — its
        ``_opencode_extract_session_id`` already recognizes a ``type:session``
        header with a bare top-level ``id`` — with a distinct session-id field
        in daemon.json so ``daemon(action='ask')`` can resume via ``--session``.
        """
        return self._run_opencode_emanation(
            em_id, run_dir, task, cancel_event, timeout_event, backend_argv,
            executable="omp",
            backend_name="oh-my-pi",
            session_state_key="oh_my_pi_session_id",
            cmd_prefix=["--mode", "json", "--approval-mode", "yolo"],
        )

    def _build_qwen_code_prompt(self, task: str) -> str:
        """Compose the prompt sent to Qwen Code headless mode."""
        return self._build_opencode_prompt(task)

    def _run_qwen_code_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run a Qwen Code CLI session as the emanation backend.

        Qwen Code documents headless mode as ``qwen -p <prompt>``. LingTai
        additionally owns ``--yolo`` so the daemon can proceed without
        interactive approval prompts. Qwen Code does not expose a stable
        machine-readable streaming/resume contract here, so stdout/stderr are
        recorded verbatim and ``daemon(action='ask')`` is intentionally
        unsupported for this backend.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        prompt = self._build_qwen_code_prompt(task)
        cmd = ["qwen", "--yolo"]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.extend(["-p", prompt])
        self._log("daemon_qwen_code_start", em_id=em_id, cmd_head=" ".join(cmd[:5]))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,
            )
        except FileNotFoundError:
            exc = RuntimeError("'qwen' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start qwen-code CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        self._register_cli_proc(proc, group_id=run_dir.group_id)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-qwen-code-stderr-{em_id}",
        )
        stderr_thread.start()

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if cancel_event.is_set():
                    _kill_process_group(proc)
                    return _exit_cancelled()
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                stdout_lines.append(line)
                try:
                    run_dir.record_cli_output(line, stream="stdout")
                except Exception:
                    pass
            proc.wait()
        except Exception as e:
            _kill_process_group(proc)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=run_dir.group_id)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
        output = "\n".join(stdout_lines).strip()

        if proc.returncode != 0:
            detail = stderr_tail or output
            attributed = self._attributed_cli_exit(
                proc, "qwen-code", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"qwen-code CLI exited with code {proc.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        text = output or (f"[no stdout; stderr tail follows]\n{stderr_tail[-500:]}" if stderr_tail else "[no output]")
        run_dir.mark_done(text)
        return text

    def _handle_ask_opencode(
        self, em_id: str, entry: dict, message: str,
        *,
        executable: str = "opencode",
        backend_name: str = "opencode",
        session_state_key: str = "opencode_session_id",
        build_resume_cmd: Callable[[str, str, str], list[str]] | None = None,
    ) -> dict:
        """Dispatch an OpenCode-family session-resume follow-up off the caller's turn.

        Mirrors ``_handle_ask_cli`` / ``_handle_ask_codex``: spawn the resume
        subprocess (``opencode run --session <id> ...`` by default), hand the
        JSON-stream parse to ``self._ask_pool``, return immediately. The
        concurrent-ask guard refuses overlapping asks per-emanation because
        resume is single-writer per session.

        ``build_resume_cmd(executable, session_id, message)`` overrides the
        argv for backends whose resume shape differs (e.g. Oh-My-Pi's
        ``omp --mode json --approval-mode yolo --session <id> <message>``).
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get(session_state_key)
        if not session_id:
            return {"status": "error",
                    "message": f"No {backend_name} session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        if build_resume_cmd is not None:
            cmd = build_resume_cmd(executable, session_id, message)
        else:
            cmd = [
                executable,
                "run",
                "--session", session_id,
                "--format", "json",
                message,
            ]
        self._log(f"daemon_{backend_name}_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,  # own process group for reliable cleanup
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"'{executable}' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start {backend_name} CLI: {e}"}
        # Ask follow-ups are not part of any batch (see claude-code ask).
        self._register_cli_proc(proc, group_id=None)

        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_opencode_stream, em_id, entry, proc, run_dir, backend_name,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _handle_ask_mimocode(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a MiMo Code ``mimo run --session`` follow-up."""
        return self._handle_ask_opencode(
            em_id, entry, message,
            executable="mimo",
            backend_name="mimocode",
            session_state_key="mimocode_session_id",
        )

    @staticmethod
    def _oh_my_pi_resume_cmd(executable: str, session_id: str, message: str) -> list[str]:
        """Build the Oh-My-Pi resume argv: ``omp --mode json --approval-mode yolo
        --session <id> <message>``."""
        return [
            executable,
            "--mode", "json",
            "--approval-mode", "yolo",
            "--session", session_id,
            message,
        ]

    def _handle_ask_oh_my_pi(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch an Oh-My-Pi ``omp --mode json --session`` follow-up."""
        return self._handle_ask_opencode(
            em_id, entry, message,
            executable="omp",
            backend_name="oh-my-pi",
            session_state_key="oh_my_pi_session_id",
            build_resume_cmd=self._oh_my_pi_resume_cmd,
        )

    def _run_ask_opencode_stream(
        self,
        em_id: str,
        entry: dict,
        proc: subprocess.Popen,
        run_dir: DaemonRunDir,
        backend_name: str = "opencode",
    ) -> dict:
        """Background worker: stream an ``opencode run --session`` subprocess.

        Same defensive JSON-line parse as ``_run_opencode_emanation``:
        non-JSON lines are recorded verbatim, text is pulled from any
        plausible field, terminal-shaped events override intermediate
        text. Always clears ``ask_in_flight`` and detaches ``proc`` from
        ``_cli_procs`` on exit.
        """
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-{backend_name}-ask-stderr-{em_id}",
        )
        stderr_thread.start()

        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False
        timed_out = False

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + self._timeout
            # See _run_ask_claude_code_stream for the rationale on
            # _iter_stdout_with_deadline — fixes the silent-CLI hang.
            for raw_line in _iter_stdout_with_deadline(
                proc, deadline,
                thread_name=f"daemon-{backend_name}-ask-stdout-{em_id}",
            ):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    if low.endswith((".completed", ".done", ".finished",
                                     "result", "final")):
                        if text:
                            final_text = text

            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
            else:
                try:
                    proc.wait(timeout=max(1.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group(proc)
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=None)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"{backend_name} run timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            err = f"{backend_name} CLI exited {proc.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_text is not None:
            output = final_text.strip()
        elif text_chunks:
            output = text_chunks[-1].strip()
        else:
            output = ""

        if not any_event and not output:
            output = "[no output]"

        if output and output != "[no output]":
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}


    # ------------------------------------------------------------------
    # Cursor backend (Cursor Agent CLI, `agent -p --output-format stream-json`)
    # ------------------------------------------------------------------

    # Cursor's headless CLI is exposed as the `agent` executable. In print mode
    # (`-p` / `--print`) it can emit the same single-result JSON shape and
    # stream-json shape documented by Cursor's CLI reference.  We parse it with
    # the same defensive helpers used by OpenCode because both are JSONL CLI
    # backends whose event vocabularies may evolve between releases. Cursor's
    # documented final result event includes `result` and `session_id` fields;
    # the shared helpers cover both.

    def _build_cursor_prompt(self, task: str) -> str:
        """Compose the initial prompt sent to Cursor Agent CLI."""
        return self._build_opencode_prompt(task)

    def _run_cursor_emanation(
        self,
        em_id: str,
        run_dir: DaemonRunDir,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
    ) -> str:
        """Run a Cursor Agent CLI session as the emanation backend.

        Spawns ``agent -p --force --output-format stream-json <prompt>``.
        ``-p`` puts Cursor in non-interactive print mode; ``--force`` allows
        file modifications in that mode (matching the daemon's coding-agent
        expectation); ``stream-json`` gives one JSON object per stdout line.
        The first event carrying a session-id-shaped field is stored in
        daemon.json under ``cursor_session_id`` for ``daemon(action='ask')``.
        """
        def _exit_cancelled() -> str:
            if timeout_event is not None and timeout_event.is_set():
                run_dir.mark_timeout()
            else:
                run_dir.mark_cancelled()
            return "[cancelled]"

        if cancel_event.is_set():
            return _exit_cancelled()

        prompt = self._build_cursor_prompt(task)
        cmd = [
            "agent",
            "-p",
            "--force",
            "--output-format", "stream-json",
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(prompt)
        self._log("daemon_cursor_start", em_id=em_id, cmd_head=" ".join(cmd[:5]))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,
            )
        except FileNotFoundError:
            exc = RuntimeError("'agent' Cursor CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start Cursor CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        self._register_cli_proc(proc, group_id=run_dir.group_id)

        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-cursor-stderr-{em_id}",
        )
        stderr_thread.start()

        session_id_captured: str | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if not sid or session_id_captured == sid:
                return
            session_id_captured = sid
            run_dir._state["cursor_session_id"] = sid
            run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log("daemon_cursor_session", em_id=em_id, session_id=sid)

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                if cancel_event.is_set():
                    _kill_process_group(proc)
                    return _exit_cancelled()

                line = raw_line.rstrip("\n")
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                sid = self._opencode_extract_session_id(event)
                if sid:
                    _store_session_id(sid)

                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")

                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    subtype = str(event.get("subtype") or "").lower()
                    is_error_event = bool(event.get("is_error")) or subtype == "error"
                    is_result_event = low == "result" or low.endswith(
                        (".completed", ".done", ".finished", ".result", ".final")
                    )
                    if is_result_event:
                        final_is_error = is_error_event
                        if text:
                            final_text = text

            proc.wait()
        except Exception as e:
            _kill_process_group(proc)
            run_dir.mark_failed(e)
            raise
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=run_dir.group_id)

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            attributed = self._attributed_cli_exit(
                proc, "Cursor", detail[-500:], run_dir,
            )
            exc = RuntimeError(
                attributed
                or f"Cursor CLI exited with code {proc.returncode}: "
                f"{detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        if final_is_error:
            detail = final_text or stderr_tail or "\n".join(text_chunks[-3:])
            exc = RuntimeError(
                f"Cursor CLI reported error result: {detail[-500:]}"
            )
            run_dir.mark_failed(exc)
            raise exc

        if final_text is not None:
            text = final_text.strip()
        elif text_chunks:
            text = text_chunks[-1].strip()
        elif stderr_tail:
            text = f"[no JSON events; stderr tail follows]\n{stderr_tail[-500:]}"
        else:
            text = "[no output]"
        if not any_event and not stderr_tail:
            text = "[no output]"

        run_dir.mark_done(text)
        return text

    def _handle_ask_cursor(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch a Cursor Agent CLI ``--resume`` follow-up off the caller's turn."""
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("cursor_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No cursor session ID found for {em_id}. "
                               "The emanation may still be initializing — "
                               "wait a moment and retry."}

        with entry["followup_lock"]:
            if entry.get("ask_in_flight"):
                return {"status": "busy", "id": em_id,
                        "message": f"a previous ask on {em_id} is still "
                                   "running; wait for it or use "
                                   f"daemon(action='check', id='{em_id}')"}
            entry["ask_in_flight"] = True

        cmd = [
            "agent",
            "-p",
            "--force",
            "--resume", session_id,
            "--output-format", "stream-json",
            message,
        ]
        self._log("daemon_cursor_ask", em_id=em_id,
                  session_id=session_id, message_length=len(message))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self._agent._working_dir),
                start_new_session=True,
            )
        except FileNotFoundError:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": "'agent' Cursor CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start Cursor CLI: {e}"}
        # Ask follow-ups are not part of any batch (see claude-code ask).
        self._register_cli_proc(proc, group_id=None)

        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_cursor_stream, em_id, entry, proc, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_cursor_stream(
        self,
        em_id: str,
        entry: dict,
        proc: subprocess.Popen,
        run_dir: DaemonRunDir,
    ) -> dict:
        """Background worker: stream an ``agent -p --resume`` subprocess."""
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                stderr_lines.append(stripped)
                try:
                    run_dir.record_cli_output(stripped, stream="stderr")
                except Exception:
                    pass

        stderr_thread = threading.Thread(
            target=_drain_stderr, daemon=True,
            name=f"daemon-cursor-ask-stderr-{em_id}",
        )
        stderr_thread.start()

        text_chunks: list[str] = []
        final_text: str | None = None
        final_is_error = False
        any_event = False
        timed_out = False

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + self._timeout
            for raw_line in _iter_stdout_with_deadline(
                proc, deadline,
                thread_name=f"daemon-cursor-ask-stdout-{em_id}",
            ):
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    run_dir.record_cli_output(line, stream="stdout")
                    continue
                if not isinstance(event, dict):
                    run_dir.record_cli_output(line, stream="stdout")
                    continue

                any_event = True
                text = self._opencode_extract_text(event)
                if text:
                    text_chunks.append(text)
                    run_dir.record_cli_output(text, stream="stdout")
                etype = event.get("type") or ""
                if isinstance(etype, str) and etype:
                    low = etype.lower()
                    subtype = str(event.get("subtype") or "").lower()
                    is_error_event = bool(event.get("is_error")) or subtype == "error"
                    is_result_event = low == "result" or low.endswith(
                        (".completed", ".done", ".finished", ".result", ".final")
                    )
                    if is_result_event:
                        final_is_error = is_error_event
                        if text:
                            final_text = text

            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
            else:
                try:
                    proc.wait(timeout=max(1.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_process_group(proc)
        finally:
            stderr_thread.join(timeout=2.0)
            self._unregister_cli_proc(proc, group_id=None)
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"Cursor CLI resume timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            err = f"Cursor CLI exited {proc.returncode}: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_is_error:
            detail = final_text or stderr_tail or "\n".join(text_chunks[-3:])
            err = f"Cursor CLI reported error result: {detail[-500:]}"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if final_text is not None:
            output = final_text.strip()
        elif text_chunks:
            output = text_chunks[-1].strip()
        else:
            output = ""

        if not any_event and not output:
            output = "[no output]"

        if output and output != "[no output]":
            self._publish_followup_if_live(
                em_id, status="follow-up completed", text=output, run_dir=run_dir,
            )
        return {"status": "sent", "id": em_id, "output": output}

    # Hard cap on `last` to bound memory in case events.jsonl has grown large
    # (long-running emanations under the new 3600s timeout default can write
    # thousands of events). Beyond this an agent should read the file directly.
    _CHECK_LAST_MAX = 1000

    def _handle_check(self, em_id: str, last=20, truncate=500) -> dict:
        """Read-only progress tail for one emanation.

        Returns a snapshot of daemon.json plus the last N events from
        events.jsonl, with string fields truncated. Pure read — no
        coordination with the run thread (atomic writes + append-only JSONL
        guarantee a consistent view).
        """
        # Validate and coerce — the LLM may pass non-numeric strings;
        # reject cleanly rather than letting int() raise to the dispatcher.
        try:
            last = int(last)
        except (TypeError, ValueError):
            return {"status": "error",
                    "message": f"last must be a positive integer (got {last!r})"}
        try:
            truncate = int(truncate)
        except (TypeError, ValueError):
            return {"status": "error",
                    "message": f"truncate must be a non-negative integer (got {truncate!r})"}
        if last < 1:
            return {"status": "error", "message": f"last must be ≥ 1 (got {last})"}
        if truncate < 0:
            return {"status": "error", "message": f"truncate must be ≥ 0 (got {truncate})"}
        # Cap last to prevent self-DoS — readlines() loads the whole file
        # before slicing, so an unbounded last would read all of events.jsonl.
        last = min(last, self._CHECK_LAST_MAX)

        entry = self._emanations.get(em_id)
        if not entry:
            return {"status": "error", "message": f"Unknown emanation: {em_id}"}
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        # daemon.json — atomic-replaced, may transiently miss but never partial
        try:
            state = json.loads(run_dir.daemon_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return {"status": "error", "message": f"daemon.json read failed: {e}"}

        # events.jsonl — append-only, missing means no events yet
        events: list[dict] = []
        events_total = 0
        if run_dir.events_path.is_file():
            try:
                with open(run_dir.events_path, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()
            except OSError as e:
                return {"status": "error", "message": f"events.jsonl read failed: {e}"}
            events_total = len(raw_lines)
            tail = raw_lines[-last:] if last > 0 else []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if truncate > 0:
                    ev = {k: (v[:truncate] + "…[truncated]"
                              if isinstance(v, str) and len(v) > truncate else v)
                          for k, v in ev.items()}
                events.append(ev)

        return {
            "id": em_id,
            "run_id": state.get("run_id"),
            "state": state.get("state"),
            "backend": state.get("backend"),
            "path": str(run_dir.path),
            "turn": state.get("turn"),
            "current_tool": state.get("current_tool"),
            "elapsed_s": state.get("elapsed_s"),
            "finished_at": state.get("finished_at"),
            "tokens": state.get("tokens", {}),
            "result_preview": state.get("result_preview"),
            "result_path": state.get("result_path"),
            "last_output": state.get("last_output"),
            "last_output_at": state.get("last_output_at"),
            "error": state.get("error"),
            "events": events,
            "events_total": events_total,
            "events_returned": len(events),
        }

    def shutdown_for_agent_stop(
        self, reason: str = "agent_stop", wait_timeout: float = 5.0
    ) -> dict:
        """Shut down daemon-owned runtime resources during agent teardown.

        Refresh/suspend/stop must not release the parent agent heartbeat/lock
        while daemon executor workers or external CLI subprocess groups can
        still keep the old Python interpreter alive.  This lifecycle hook is
        intentionally best-effort and non-raising: callers in the agent stop
        path must continue toward teardown even if one child process is already
        gone or a pool has raced to completion.
        """
        return self._shutdown_runtime_resources(
            reason=reason, wait_timeout=wait_timeout
        )

    def _shutdown_runtime_resources(
        self, *, reason: str, wait_timeout: float = 0.0
    ) -> dict:
        futures = [
            future for e in self._emanations.values()
            if (future := e.get("future")) is not None
        ]
        ask_futures = [
            future for e in self._emanations.values()
            if (future := e.get("ask_future")) is not None
        ]
        wait_futures = futures + ask_futures
        cancelled = sum(1 for future in wait_futures if not future.done())
        errors: list[str] = []

        # Mark entries before killing child processes. CLI ask workers can wake
        # up immediately after the kill but before _emanations is cleared; the
        # parent-facing follow-up notification gate must treat that window as
        # post-reclaim too.
        for entry in self._emanations.values():
            entry["shutdown_in_progress"] = True

        # Kill all tracked CLI process groups first — this terminates child
        # shells/tools that cancel_event alone cannot reach (GH #122).
        # Snapshot under lock, kill outside to avoid holding lock during wait.
        procs_to_kill = self._drain_all_cli_procs(reason=reason)
        for proc in procs_to_kill:
            try:
                _kill_process_group(proc)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"kill pid {getattr(proc, 'pid', '?')}: {e}")

        pools = list(self._pools)
        self._pools.clear()
        for pool, cancel in pools:
            try:
                cancel.set()
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"cancel pool: {e}")
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"shutdown pool: {e}")

        # Tear down the dedicated CLI-ask pool too — its workers are already
        # losing their subprocesses to the kill above, but futures may still
        # be sitting in the queue. Rebuild a fresh pool so explicit reclaim
        # and stop/start reuse leave the manager in a valid state.
        try:
            self._ask_pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:  # pragma: no cover - defensive teardown
            errors.append(f"shutdown ask pool: {e}")
        self._ask_pool = ThreadPoolExecutor(
            max_workers=max(1, self._max_emanations),
            thread_name_prefix="daemon-cli-ask",
        )

        # During parent stop/refresh, keep heartbeat/lock alive for a bounded
        # grace period while killed CLI workers and cooperative daemon loops
        # unwind. Explicit daemon(action="reclaim") keeps the old non-blocking
        # behavior by passing wait_timeout=0.
        futures_remaining = sum(1 for future in wait_futures if not future.done())
        if wait_timeout > 0 and futures_remaining:
            try:
                wait(wait_futures, timeout=wait_timeout)
            except Exception as e:  # pragma: no cover - defensive teardown
                errors.append(f"wait futures: {e}")
            futures_remaining = sum(
                1 for future in wait_futures if not future.done()
            )

        self._emanations.clear()
        self._next_id = 1  # handles can be re-used; folder names disambiguate

        report = {
            "status": "shutdown",
            "reason": reason,
            "cancelled": cancelled,
            "cli_processes_killed": len(procs_to_kill),
            "pools_shutdown": len(pools),
            "ask_futures_shutdown": len(ask_futures),
            "futures_remaining": futures_remaining,
            "errors": errors,
        }
        self._log("daemon_lifecycle_shutdown", **report)
        return report

    def _handle_reclaim(self) -> dict:
        report = self.shutdown_for_agent_stop(reason="reclaim", wait_timeout=0.0)
        cancelled = report.get("cancelled", 0)
        self._log("daemon_reclaim", cancelled_count=cancelled)
        return {"status": "reclaimed", "cancelled": cancelled}

    def _on_emanation_done(self, em_id: str, task_summary: str, future) -> None:
        elapsed = 0.0
        entry = self._emanations.get(em_id)
        if entry:
            elapsed = time.time() - entry["start_time"]
        run_dir = entry.get("run_dir") if entry else None

        # Derive a fallback status from the future result. The future returns
        # text on cooperative exit (including the short ``[cancelled]`` sentinel
        # a timed-out/reclaimed run returns) and raises on a hard failure.
        status = "done"
        try:
            text = future.result()
            self._log("daemon_result", em_id=em_id, status="done",
                      text_length=len(text), elapsed_ms=round(elapsed * 1000))
        except Exception as e:
            status = "failed"
            text = f"Failed: {e}"
            self._log("daemon_error", em_id=em_id,
                      exception=type(e).__name__, exception_message=str(e))

        # The run directory's recorded state is authoritative for the terminal
        # status: a cancelled/timed-out run returns the short ``[cancelled]``
        # sentinel through the future, which would otherwise be misclassified as
        # a tiny "done" and swallowed by the short-result gate below. Prefer it
        # so every terminal status (done / failed / cancelled / timeout) is
        # labelled and reported correctly, and the parent always learns the
        # daemon terminated even when it failed silently.
        if run_dir is not None:
            try:
                recorded = run_dir.state_snapshot().get("state")
            except Exception:
                recorded = None
            if recorded in ("done", "failed", "cancelled", "timeout"):
                status = recorded

        # Suppress notifications only for short *successful* results to prevent
        # notification storms. Every non-success terminal state (failed,
        # cancelled, timeout) always notifies so the parent can safely go idle
        # after dispatch and still learn the run ended.
        if status == "done" and len(text) < self._notify_threshold:
            self._log("daemon_result", em_id=em_id, status="suppressed_short",
                      text_length=len(text))
            return

        # Deliver exactly once per run: the done-callback can fire more than
        # once for the same em_id (racing reclaim, re-entrant callbacks). The
        # run directory owns the once-only claim, decoupled from the system
        # channel's ref_id dedup so an earlier follow-up (``ask``) event sharing
        # this run's ref_id cannot suppress the terminal notification.
        if run_dir is not None and not run_dir.claim_terminal_notification():
            self._log("daemon_terminal_notify_skipped_duplicate",
                      em_id=em_id, status=status)
            return
        self._publish_daemon_notification(
            em_id, status=status, text=text, run_dir=run_dir,
        )

    # ------------------------------------------------------------------
    # CLI process-group tracking helpers
    #
    # Every external-CLI backend registers its Popen here on spawn and
    # unregisters it on exit, instead of poking _cli_procs directly. Ownership
    # metadata (the batch ``group_id``) lets a batch's timeout watchdog kill
    # only its own subprocesses (_kill_cli_group), while reclaim-all still
    # drains everything (_drain_all_cli_procs).
    # ------------------------------------------------------------------

    def _register_cli_proc(self, proc: subprocess.Popen,
                           group_id: str | None = None) -> None:
        """Track *proc* globally and (if batched) under its ``group_id``."""
        with self._cli_lock:
            self._cli_procs.append(proc)
            if group_id is not None:
                self._cli_proc_groups.setdefault(group_id, set()).add(proc)
            # CPython may recycle a previous proc's id() for this fresh object.
            # Drop any stale termination reason left under that id (e.g. a kill
            # stamped on a proc that then exited 0 before SIGTERM landed) so it
            # cannot be mis-attributed to this new subprocess. See GH #455.
            self._cli_term_reasons.pop(id(proc), None)

    def _unregister_cli_proc(self, proc: subprocess.Popen,
                             group_id: str | None = None) -> None:
        """Detach *proc* from global and group tracking. Idempotent.

        The recorded termination reason (if any) is intentionally NOT cleared
        here: ``_unregister_cli_proc`` runs in the read-loop's ``finally`` block,
        immediately before the returncode is classified, so the reason must
        survive until ``_take_cli_term_reason`` consumes it.
        """
        with self._cli_lock:
            try:
                self._cli_procs.remove(proc)
            except ValueError:
                pass  # already removed by reclaim/watchdog
            if group_id is not None:
                bucket = self._cli_proc_groups.get(group_id)
                if bucket is not None:
                    bucket.discard(proc)
                    if not bucket:
                        del self._cli_proc_groups[group_id]

    def _note_cli_term_reason(self, proc: subprocess.Popen, reason: str) -> None:
        """Record the LingTai-initiated termination *reason* for *proc*.

        Called at the out-of-loop kill sites (reclaim/agent_stop/refresh and
        batch timeout) just before SIGTERM. First reason wins so a follow-up
        teardown kill cannot overwrite the original causal reason (e.g. a
        timeout that is then swept by reclaim stays "timeout"). See GH #455.
        """
        with self._cli_lock:
            self._cli_term_reasons.setdefault(id(proc), reason)

    def _take_cli_term_reason(self, proc: subprocess.Popen) -> str | None:
        """Pop and return the recorded termination reason for *proc*, if any."""
        with self._cli_lock:
            return self._cli_term_reasons.pop(id(proc), None)

    def _kill_cli_group(self, group_id: str, reason: str = "timeout") -> None:
        """Kill only the CLI process groups owned by *group_id*.

        Snapshots the group's procs under the lock, detaches them from both
        the group index and the global list, then kills outside the lock so we
        never hold ``_cli_lock`` across a multi-second ``proc.wait``. Procs from
        other batches (and ungrouped ``ask`` procs) are left untouched.

        *reason* (default "timeout", the only current caller) is stamped on each
        proc before SIGTERM so the read loop can attribute the signal exit.
        """
        with self._cli_lock:
            bucket = self._cli_proc_groups.pop(group_id, None)
            procs_to_kill = list(bucket) if bucket else []
            for proc in procs_to_kill:
                self._cli_term_reasons.setdefault(id(proc), reason)
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass
        for proc in procs_to_kill:
            _kill_process_group(proc)

    def _drain_all_cli_procs(self, reason: str | None = None) -> list[subprocess.Popen]:
        """Clear all CLI tracking and return the procs to kill (reclaim path).

        When *reason* is given (agent_stop / parent refresh / reclaim) it is
        stamped on each drained proc before the caller sends SIGTERM, so the
        read loop attributes the resulting -15/143 exit to that local cause
        instead of reporting an opaque CLI failure (GH #455).
        """
        with self._cli_lock:
            procs_to_kill = list(self._cli_procs)
            if reason is not None:
                for proc in procs_to_kill:
                    self._cli_term_reasons.setdefault(id(proc), reason)
            self._cli_procs.clear()
            self._cli_proc_groups.clear()
        return procs_to_kill

    @staticmethod
    def _signal_exit_name(returncode: int | None) -> str | None:
        """Map a Popen returncode to a signal name, or None if not a signal.

        Covers both subprocess conventions: negative (``-15``) when Python
        reaps the child directly, and ``128 + signum`` (``143``) when the exit
        propagates through a shell.
        """
        if returncode in (-15, 143):
            return "SIGTERM"
        if returncode in (-9, 137):
            return "SIGKILL"
        return None

    def _attributed_cli_exit(
        self,
        proc: subprocess.Popen,
        backend_name: str,
        detail: str,
        run_dir: "DaemonRunDir | None" = None,
    ) -> str | None:
        """Attribute a signal-terminated CLI exit to its local cause.

        Returns a human-readable message naming the LingTai-initiated reason
        (e.g. ``agent_stop`` / ``reclaim`` / ``timeout``) when this manager
        recorded one before sending the signal, and records the reason on
        *run_dir* for forensic inspection. The raw exit code is always kept in
        the message. Returns ``None`` when the exit is not a signal we attribute
        or no local reason was recorded — the caller then keeps its existing
        opaque message so external/unknown SIGTERMs are not mislabeled as
        deliberate cancellations. See GH #455.
        """
        reason = self._take_cli_term_reason(proc)
        signal_name = self._signal_exit_name(getattr(proc, "returncode", None))
        if signal_name is None or reason is None:
            return None
        if run_dir is not None:
            try:
                run_dir.record_cli_termination(
                    reason=reason,
                    signal_name=signal_name,
                    returncode=proc.returncode,
                )
            except Exception:
                pass
        msg = (
            f"{backend_name} CLI terminated by LingTai ({reason}, "
            f"{signal_name}, code {proc.returncode})"
        )
        return f"{msg}: {detail}" if detail else msg

    def _arm_batch_done_cancel(self, futures: list,
                               cancel_event: threading.Event) -> None:
        """Set *cancel_event* once every future in *futures* is done.

        This stops a completed batch's watchdog from sleeping out its full
        timeout and then waking to kill/scan after all work already finished.
        A done-callback runs per future; the last one to complete trips the
        event. Setting cancel after all futures are done is harmless to the run
        loops (they have already returned) — only the idle watchdog observes it.

        ``timeout_event`` is intentionally left unset: this is normal
        completion, not a timeout, so terminal run state stays "done"/"failed".
        """
        if not futures:
            cancel_event.set()
            return
        remaining = {"n": len(futures)}
        lock = threading.Lock()

        def _done(_f):
            with lock:
                remaining["n"] -= 1
                if remaining["n"] > 0:
                    return
            cancel_event.set()

        for future in futures:
            future.add_done_callback(_done)

    def _watchdog(self, cancel_event: threading.Event,
                  timeout_event: threading.Event, timeout: float,
                  cli_group_id: str | None = None) -> None:
        """Kill emanations that exceed the timeout.

        Sets timeout_event BEFORE cancel_event so the run loop can observe
        the timeout flag at its next checkpoint and call mark_timeout instead
        of mark_cancelled.

        Also directly kills the CLI process groups *belonging to this batch*
        (``cli_group_id``) so that long child tool/CLI commands are terminated
        even if the run loop is blocked on stdout (GH #121) — without touching
        a newer, unrelated batch's procs (GH overlapping-batch kill). When
        ``cli_group_id`` is None (e.g. the in-process lingtai backend, which
        spawns no CLI procs) no kill scan runs.

        Returns early without firing if ``cancel_event`` is already set — once
        a batch's futures are all done, the dispatch path signals cancel so a
        completed batch's watchdog cannot wake later and do work.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event.is_set():
                return
            time.sleep(1.0)
        if cancel_event.is_set():
            return
        timeout_event.set()
        cancel_event.set()
        # Kill CLI process groups directly — the run loop may be blocked
        # reading stdout from a long child command and cannot check
        # cancel_event until that command finishes. Scoped to this batch only.
        if cli_group_id is not None:
            self._kill_cli_group(cli_group_id)

    def _log(self, event_type: str, **fields) -> None:
        """Log through the parent agent's logging system."""
        if hasattr(self._agent, '_log'):
            self._agent._log(event_type, **fields)


def setup(agent: "Agent", max_emanations: int = 100,
          max_turns: int = DEFAULT_MAX_TURNS, timeout: float = 3600.0,
          notify_threshold: int = 20) -> DaemonManager:
    """Set up the daemon capability on an agent."""
    lang = agent._config.language
    mgr = DaemonManager(agent, max_emanations=max_emanations,
                        max_turns=max_turns, timeout=timeout,
                        notify_threshold=notify_threshold)
    schema = get_schema(lang)
    agent.add_tool("daemon", schema=schema, handler=mgr.handle,
                   description=get_description(lang))
    return mgr
