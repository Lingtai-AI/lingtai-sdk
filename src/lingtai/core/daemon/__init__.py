"""Daemon capability (神識) — dispatch ephemeral subagents (分神).

Gives an agent the ability to split its consciousness into focused worker
fragments that operate in parallel on the same working directory.  Each
emanation is a disposable ChatSession with a curated tool surface — not an
agent.  Results are persisted in daemon run directories; completion is surfaced via a compact system notification.

Usage:
    Agent(capabilities=["daemon"])
    Agent(capabilities={"daemon": {"max_emanations": 10}})
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t

if TYPE_CHECKING:
    from ...agent import Agent

from lingtai_kernel.llm.base import FunctionSchema
from .run_dir import DaemonRunDir

PROVIDERS = {"providers": [], "default": "builtin"}


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
EMANATION_BLACKLIST = {"daemon", "avatar", "psyche", "skills", "knowledge"}

# Env vars that override Claude Code's normal first-party OAuth credentials.
# LingTai loads ``.env`` from ``~/.lingtai-tui/`` early, so auth intended for
# another LLM adapter can leak into spawned ``claude`` subprocesses.
# ``ANTHROPIC_*`` keys force API billing (GH #107); a stale
# ``CLAUDE_CODE_OAUTH_TOKEN`` can also beat a refreshed
# ``~/.claude/.credentials.json`` and surface as a false weekly-limit error
# (GH Lingtai-AI/lingtai#189). Strip these for claude-code spawns only; other
# backends (codex, lingtai, opencode) are unaffected.
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
                        "preset": {
                            "type": "string",
                            "description": t(lang, "daemon.tasks.preset"),
                        },
                        "backend_options": {
                            "type": "object",
                            "description": t(lang, "daemon.tasks.backend_options"),
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
            "max_turns": {
                "type": "integer",
                "minimum": 1,
                "description": t(lang, "daemon.max_turns"),
            },
            "timeout": {
                "type": "number",
                "minimum": 5,
                "description": t(lang, "daemon.timeout"),
            },
            "backend": {
                "type": "string",
                "enum": ["lingtai", "claude-code", "codex", "opencode"],
                "description": (
                    "Execution backend: 'lingtai' (default — parallel LLM reasoning, uses your current model), "
                    "'claude-code' (coding tasks, code review, file manipulation via Claude Code CLI), "
                    "'codex' (coding tasks via OpenAI Codex CLI), "
                    "'opencode' (multi-provider open-source agent via the opencode-ai CLI). "
                    "CLI backends use external tools with no LLM overhead from the parent."
                ),
            },
        },
        "required": ["action"],
    }


class DaemonManager:
    """Manages subagent (emanation) lifecycle."""

    # Minimum text length to trigger a parent notification.
    # Short results (e.g. "[cancelled]") are suppressed to avoid notification storms.
    _NOTIFY_MIN_LEN = 20

    def __init__(self, agent: "Agent", max_emanations: int = 10,
                 max_turns: int = 200, timeout: float = 3600.0,
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
        self._cli_procs: list[subprocess.Popen] = []
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

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        backend = args.get("backend", "lingtai")
        if action == "emanate":
            return self._handle_emanate(
                args.get("tasks", []),
                max_turns=args.get("max_turns"),
                timeout=args.get("timeout"),
                backend=backend,
            )
        elif action == "list":
            return self._handle_list()
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

    def _build_tool_surface(
        self,
        requested: list[str],
        preset_surface: tuple[dict, dict] | None = None,
    ) -> tuple[list[FunctionSchema], dict]:
        """Build filtered tool schemas and dispatch map for an emanation.

        When ``preset_surface`` is provided (preset-driven emanation), the
        capability tools come from the preset's pre-instantiated sandbox
        (``preset_surface = (schemas_by_name, handlers_by_name)``), unioned
        with the parent's MCP tools (those don't bind to an LLM, so they
        carry over). When ``preset_surface`` is None, the parent's currently
        registered tool surface is used (today's behavior).
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

        if preset_surface is not None:
            preset_schemas, preset_handlers = preset_surface
            # Available surface = preset capabilities ∪ parent's MCP tools
            capability_names = {cap_name for cap_name, _ in self._agent._capabilities}
            all_registered = {s.name for s in self._agent._tool_schemas}
            mcp_names = all_registered - capability_names - EMANATION_BLACKLIST
            available = set(preset_schemas.keys()) | mcp_names
            # MCP tools auto-included (parent-bound, LLM-agnostic)
            tool_names |= mcp_names

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
                elif n in parent_schema_map:
                    # MCP tool from parent
                    schemas.append(parent_schema_map[n])
                    if n in self._agent._tool_handlers:
                        dispatch[n] = self._agent._tool_handlers[n]
            return schemas, dispatch

        # Default path: emanation runs on parent's tool surface
        capability_names = {cap_name for cap_name, _ in self._agent._capabilities}
        all_registered = {s.name for s in self._agent._tool_schemas}
        mcp_names = all_registered - capability_names - EMANATION_BLACKLIST
        tool_names |= mcp_names

        # Validate requested tools exist
        available = {s.name for s in self._agent._tool_schemas}
        missing = tool_names - available
        if missing:
            raise ValueError(f"Unknown tools for emanation: {missing}")

        # Build schemas and dispatch
        schema_map = {s.name: s for s in self._agent._tool_schemas}
        schemas = [schema_map[n] for n in sorted(tool_names) if n in schema_map]
        dispatch = {n: self._agent._tool_handlers[n]
                    for n in tool_names if n in self._agent._tool_handlers}
        return schemas, dispatch

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

    def _build_emanation_prompt(self, task: str, schemas: list[FunctionSchema]) -> str:
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

        lines.append("")
        lines.append("Your task:")
        lines.append(task)

        return "\n".join(lines)

    def _run_emanation(self, em_id: str, run_dir, schemas, dispatch,
                       task: str,
                       cancel_event: threading.Event,
                       timeout_event: threading.Event | None = None,
                       preset_llm: dict | None = None,
                       max_turns: int | None = None) -> str:
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
            )
            effective_model = preset_llm["model"]
        else:
            service = self._agent.service
            effective_model = self._default_model

        session = service.create_session(
            system_prompt=run_dir.prompt_path.read_text(encoding="utf-8"),
            tools=schemas or None,
            model=effective_model,
            thinking="default",
            tracked=False,
        )

        endpoint = getattr(service, "_base_url", None)

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

                tool_results = []
                for tc in response.tool_calls:
                    handler = dispatch.get(tc.name)
                    if handler is None:
                        run_dir.set_current_tool(tc.name, tc.args or {})
                        result = {"status": "error", "message": f"Unknown tool: {tc.name}"}
                        run_dir.clear_current_tool(result_status="error")
                    else:
                        run_dir.set_current_tool(tc.name, tc.args or {})
                        try:
                            result = handler(tc.args or {})
                            status = "error" if isinstance(result, dict) and result.get("status") == "error" else "ok"
                            run_dir.clear_current_tool(result_status=status)
                        except Exception as e:
                            result = {"status": "error", "message": str(e)}
                            run_dir.clear_current_tool(result_status="error")
                    tool_results.append(
                        service.make_tool_result(
                            tc.name, result, tool_call_id=tc.id,
                        )
                    )

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
        with self._cli_lock:
            self._cli_procs.append(proc)

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
            # `last_output` field, cli_output events, and stderr — but
            # not in sum_token_ledger.

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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or (final_result_text or "")
            exc = RuntimeError(
                f"claude CLI exited with code {proc.returncode}: "
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
        with self._cli_lock:
            self._cli_procs.append(proc)

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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(agent_message_texts[-3:])
            exc = RuntimeError(
                f"codex CLI exited with code {proc.returncode}: "
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
        """Publish a compact daemon completion event via .notification/system.json.

        Full daemon output belongs in the run directory and is inspectable via
        ``daemon(action="check", id=...)``.  The parent notification is only a
        wake signal with provenance, bounded preview, and the inspection path.
        It must not arrive as ordinary ``MSG_REQUEST`` text.
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
        if run_dir is not None:
            parts.append(f"Run directory: {run_dir.path}")
            result_path = run_dir.state_snapshot().get("result_path")
            if result_path:
                parts.append(f"Result file: {result_path}")
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
        if em_id not in self._emanations:
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
        # `_handle_ask_opencode`
        # and `list` can show them.
        self._emanations = {
            k: v for k, v in self._emanations.items()
            if not v["future"].done() or v.get("backend") not in (None, "lingtai")
        }
        self._pools = [(p, c) for p, c in self._pools if not c.is_set()]

        # --- Claude Code / Codex / OpenCode backend: skip preset resolution entirely ---
        if backend in ("claude-code", "codex", "opencode"):
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
            # which means the emanation only gets MCP tools — that's a valid
            # if unusual configuration.
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
            try:
                schemas, dispatch = self._build_tool_surface(
                    spec["tools"], preset_surface=preset_surface,
                )
            except ValueError as e:
                return {"status": "error", "message": str(e)}
            system_prompt = self._build_emanation_prompt(spec["task"], schemas)

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
                    log_callback=self._log,
                    preset_name=resolved["name"] if resolved else None,
                    preset_provider=resolved["llm"].get("provider") if resolved else None,
                    preset_model=resolved["llm"].get("model") if resolved else None,
                )
            except OSError as e:
                return {"status": "error",
                        "message": f"Failed to create daemon folder: {e}"}

            future = pool.submit(
                self._run_emanation,
                em_id, run_dir, schemas, dispatch,
                spec["task"], cancel_event, timeout_event,
                resolved["llm"] if resolved else None,
                effective_max_turns,
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

        # Start watchdog — sets timeout_event AND cancel_event when timer fires
        watchdog = threading.Thread(
            target=self._watchdog,
            args=(cancel_event, timeout_event, effective_timeout),
            daemon=True,
        )
        watchdog.start()

        self._log("daemon_emanate", ids=ids, count=len(tasks),
                  tasks=[{"task": s["task"][:80], "tools": s["tools"]} for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids}

    def _handle_emanate_cli(
        self,
        tasks: list[dict],
        backend: str,
        effective_max_turns: int,
        effective_timeout: float,
    ) -> dict:
        """Dispatch emanations via an external CLI backend (claude-code, codex, opencode).

        Skips preset resolution — the CLI manages its own tools/model/provider.
        Creates a DaemonRunDir for tracking. CLI output is persisted in the
        run directory; only terminal completion/failure emits a compact
        system notification.
        """
        # Pre-flight: validate per-task backend_options BEFORE creating any
        # run_dir or scheduling work, so a single bad spec refuses the whole
        # batch with a clear message instead of leaving half-spawned daemons.
        resolved_backend_argv: list[list[str]] = []
        for i, spec in enumerate(tasks):
            raw_opts = spec.get("backend_options")
            if raw_opts is None:
                resolved_backend_argv.append([])
                continue
            try:
                resolved_backend_argv.append(_backend_options_to_argv(raw_opts))
            except ValueError as e:
                return {"status": "error",
                        "message": f"tasks[{i}].backend_options: {e}"}

        cancel_event = threading.Event()
        timeout_event = threading.Event()
        pool = ThreadPoolExecutor(max_workers=len(tasks))
        self._pools.append((pool, cancel_event))

        ids = []
        parent_addr = self._agent._working_dir.name
        parent_pid = os.getpid()

        for i, spec in enumerate(tasks):
            em_id = f"em-{self._next_id}"
            self._next_id += 1
            ids.append(em_id)
            backend_argv = resolved_backend_argv[i]
            backend_options = spec.get("backend_options") or None

            system_prompt = f"[{backend} backend — task delegated to external CLI]"
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
            else:
                run_fn = self._run_claude_code_emanation
            future = pool.submit(
                run_fn,
                em_id, run_dir, spec["task"],
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

        # Start watchdog
        watchdog = threading.Thread(
            target=self._watchdog,
            args=(cancel_event, timeout_event, effective_timeout),
            daemon=True,
        )
        watchdog.start()

        self._log("daemon_emanate", ids=ids, count=len(tasks), backend=backend,
                  tasks=[{"task": s["task"][:80], "tools": s.get("tools", [])}
                         for s in tasks])

        return {"status": "dispatched", "count": len(tasks), "ids": ids,
                "backend": backend}

    def _handle_list(self) -> dict:
        emanations = []
        running = 0
        for em_id, entry in self._emanations.items():
            elapsed = time.time() - entry["start_time"]
            future = entry["future"]
            if future.done():
                exc = future.exception()
                if exc:
                    status = "failed"
                else:
                    status = "done"
            else:
                status = "running"
                running += 1
                exc = None
            info = {"id": em_id, "task": entry["task"][:80],
                    "status": status, "elapsed_s": round(elapsed)}
            if status == "failed" and exc:
                info["error"] = str(exc)
            run_dir = entry.get("run_dir")
            if run_dir is not None:
                info["run_id"] = run_dir.run_id
                info["path"] = str(run_dir.path)
            emanations.append(info)
        return {
            "emanations": emanations,
            "running": running,
            "max_emanations": self._max_emanations,
        }

    def _handle_ask(self, em_id: str, message: str) -> dict:
        entry = self._emanations.get(em_id)
        if not entry:
            return {"status": "error", "message": f"Unknown emanation: {em_id}"}

        # CLI backends with resumable sessions:
        #   - claude-code: `claude --resume <claude_session_id>`
        #   - codex:       `codex exec resume <codex_session_id>`
        #   - opencode:    `opencode run --session <opencode_session_id> ...`
        # All stream JSON events through the resumed turn so
        # `daemon(check)` shows live progress.
        backend = entry.get("backend")
        if backend == "claude-code":
            return self._handle_ask_cli(em_id, entry, message)
        if backend == "codex":
            return self._handle_ask_codex(em_id, entry, message)
        if backend == "opencode":
            return self._handle_ask_opencode(em_id, entry, message)

        if entry["future"].done():
            return {"status": "error", "message": "not running"}
        with entry["followup_lock"]:
            if entry["followup_buffer"]:
                entry["followup_buffer"] += "\n\n" + message
            else:
                entry["followup_buffer"] = message
        self._log("daemon_ask", em_id=em_id, message_length=len(message))
        return {"status": "sent", "id": em_id}

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
        with self._cli_lock:
            self._cli_procs.append(proc)

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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog
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
        with self._cli_lock:
            self._cli_procs.append(proc)

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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog
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
        for key in ("text", "content", "message", "delta", "answer", "output"):
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
    ) -> str:
        """Run an OpenCode CLI session as the emanation backend.

        Spawns ``opencode run --format json <prompt>`` and parses one
        JSON event per stdout line. Non-JSON lines are recorded as
        ``cli_output`` so nothing is silently dropped. The first event
        that carries a session-id-shaped field is stored in daemon.json
        under ``opencode_session_id`` — used later by
        ``daemon(action='ask')`` to resume the session via
        ``opencode run --session <id> ...``.

        OpenCode's event field naming is less standardized than
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
        # prompt stays the trailing argument opencode expects.
        cmd = [
            "opencode",
            "run",
            "--format", "json",
        ]
        if backend_argv:
            cmd.extend(backend_argv)
        cmd.append(prompt)
        self._log("daemon_opencode_start", em_id=em_id, cmd_head=" ".join(cmd[:4]))

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
            exc = RuntimeError("'opencode' CLI not found on PATH")
            run_dir.mark_failed(exc)
            raise exc
        except OSError as e:
            exc = RuntimeError(f"Failed to start opencode CLI: {e}")
            run_dir.mark_failed(exc)
            raise exc
        with self._cli_lock:
            self._cli_procs.append(proc)

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
            name=f"daemon-opencode-stderr-{em_id}",
        )
        stderr_thread.start()

        session_id_captured: str | None = None
        text_chunks: list[str] = []
        final_text: str | None = None
        any_event = False

        def _store_session_id(sid: str) -> None:
            nonlocal session_id_captured
            if not sid or session_id_captured == sid:
                return
            session_id_captured = sid
            run_dir._state["opencode_session_id"] = sid
            run_dir._atomic_write_json(run_dir.daemon_json_path, run_dir._state)
            self._log("daemon_opencode_session", em_id=em_id, session_id=sid)

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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            exc = RuntimeError(
                f"opencode CLI exited with code {proc.returncode}: "
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

    def _handle_ask_opencode(self, em_id: str, entry: dict, message: str) -> dict:
        """Dispatch an OpenCode session-resume follow-up off the caller's turn.

        Mirrors ``_handle_ask_cli`` / ``_handle_ask_codex``: spawn the
        ``opencode run --session <id>`` subprocess, hand the JSON-stream
        parse to ``self._ask_pool``, return immediately. The concurrent-ask
        guard refuses overlapping asks per-emanation because opencode
        resume is single-writer per session.
        """
        run_dir = entry.get("run_dir")
        if run_dir is None:
            return {"status": "error", "message": f"emanation {em_id} has no run_dir"}

        session_id = run_dir._state.get("opencode_session_id")
        if not session_id:
            return {"status": "error",
                    "message": f"No opencode session ID found for {em_id}. "
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
            "opencode",
            "run",
            "--session", session_id,
            "--format", "json",
            message,
        ]
        self._log("daemon_opencode_ask", em_id=em_id,
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
                    "message": "'opencode' CLI not found on PATH"}
        except OSError as e:
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False
            return {"status": "error",
                    "message": f"Failed to start opencode CLI: {e}"}
        with self._cli_lock:
            self._cli_procs.append(proc)

        try:
            run_dir.record_cli_output(
                f"[ask dispatched] {message[:200]}", stream="stdout",
            )
        except OSError:
            pass

        ask_future = self._ask_pool.submit(
            self._run_ask_opencode_stream, em_id, entry, proc, run_dir,
        )
        ask_future.add_done_callback(
            lambda f, eid=em_id: self._on_ask_done(eid, f)
        )
        entry["ask_future"] = ask_future

        return {"status": "sent", "id": em_id, "async": True,
                "message": "ask dispatched; check daemon(action='check', "
                           f"id='{em_id}') for progress and final reply"}

    def _run_ask_opencode_stream(
        self,
        em_id: str,
        entry: dict,
        proc: subprocess.Popen,
        run_dir: DaemonRunDir,
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
            name=f"daemon-opencode-ask-stderr-{em_id}",
        )
        stderr_thread.start()

        text_chunks: list[str] = []
        final_text: str | None = None
        any_event = False
        timed_out = False

        try:
            assert proc.stdout is not None
            deadline = time.monotonic() + self._timeout
            # See _run_ask_claude_code_stream for the rationale on
            # _iter_stdout_with_deadline — fixes the silent-CLI hang.
            for raw_line in _iter_stdout_with_deadline(
                proc, deadline,
                thread_name=f"daemon-opencode-ask-stdout-{em_id}",
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
            with self._cli_lock:
                try:
                    self._cli_procs.remove(proc)
                except ValueError:
                    pass  # already removed by reclaim/watchdog
            with entry["followup_lock"]:
                entry["ask_in_flight"] = False

        stderr_tail = "\n".join(stderr_lines[-20:]) if stderr_lines else ""

        if timed_out:
            err = f"opencode run timed out after {self._timeout}s"
            self._publish_followup_if_live(
                em_id, status="follow-up failed", text=err, run_dir=run_dir,
            )
            return {"status": "error", "id": em_id, "message": err}

        if proc.returncode != 0:
            detail = stderr_tail or "\n".join(text_chunks[-3:])
            err = f"opencode CLI exited {proc.returncode}: {detail[-500:]}"
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

    def _handle_reclaim(self) -> dict:
        cancelled = sum(1 for e in self._emanations.values()
                        if not e["future"].done())
        # Kill all tracked CLI process groups first — this terminates child
        # shells/tools that cancel_event alone cannot reach (GH #122).
        # Snapshot under lock, kill outside to avoid holding lock during wait.
        with self._cli_lock:
            procs_to_kill = list(self._cli_procs)
            self._cli_procs.clear()
        for proc in procs_to_kill:
            _kill_process_group(proc)
        for pool, cancel in self._pools:
            cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
        self._pools.clear()
        # Tear down the dedicated CLI-ask pool too — its workers are already
        # losing their subprocesses to the kill above, but futures may still
        # be sitting in the queue. Rebuild a fresh pool for subsequent asks.
        self._ask_pool.shutdown(wait=False, cancel_futures=True)
        self._ask_pool = ThreadPoolExecutor(
            max_workers=max(1, self._max_emanations),
            thread_name_prefix="daemon-cli-ask",
        )
        self._emanations.clear()
        self._next_id = 1  # handles can be re-used; folder names disambiguate
        self._log("daemon_reclaim", cancelled_count=cancelled)
        return {"status": "reclaimed", "cancelled": cancelled}

    def _on_emanation_done(self, em_id: str, task_summary: str, future) -> None:
        elapsed = 0.0
        entry = self._emanations.get(em_id)
        if entry:
            elapsed = time.time() - entry["start_time"]
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

        # Suppress notifications for short successful results to prevent
        # notification storms. Failures always notify.
        if status == "done" and len(text) < self._notify_threshold:
            self._log("daemon_result", em_id=em_id, status="suppressed_short",
                      text_length=len(text))
            return

        run_dir = entry.get("run_dir") if entry else None
        self._publish_daemon_notification(
            em_id, status=status, text=text, run_dir=run_dir
        )

    def _watchdog(self, cancel_event: threading.Event,
                  timeout_event: threading.Event, timeout: float) -> None:
        """Kill emanations that exceed the timeout.

        Sets timeout_event BEFORE cancel_event so the run loop can observe
        the timeout flag at its next checkpoint and call mark_timeout instead
        of mark_cancelled.

        Also directly kills all tracked CLI process groups so that long
        child tool/CLI commands are terminated even if the run loop is
        blocked on stdout (GH #121).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event.is_set():
                return
            time.sleep(1.0)
        timeout_event.set()
        cancel_event.set()
        # Kill CLI process groups directly — the run loop may be blocked
        # reading stdout from a long child command and cannot check
        # cancel_event until that command finishes.
        # Snapshot under lock, kill outside to avoid holding lock during wait.
        with self._cli_lock:
            procs_to_kill = list(self._cli_procs)
            self._cli_procs.clear()
        for proc in procs_to_kill:
            _kill_process_group(proc)

    def _log(self, event_type: str, **fields) -> None:
        """Log through the parent agent's logging system."""
        if hasattr(self._agent, '_log'):
            self._agent._log(event_type, **fields)


def setup(agent: "Agent", max_emanations: int = 10,
          max_turns: int = 200, timeout: float = 3600.0,
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
