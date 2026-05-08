"""Daemon capability (神識) — dispatch ephemeral subagents (分神).

Gives an agent the ability to split its consciousness into focused worker
fragments that operate in parallel on the same working directory.  Each
emanation is a disposable ChatSession with a curated tool surface — not an
agent.  Results return as [daemon:em-N] notifications in the parent's inbox.

Usage:
    Agent(capabilities=["daemon"])
    Agent(capabilities={"daemon": {"max_emanations": 10}})
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from ...i18n import t

if TYPE_CHECKING:
    from ...agent import Agent

from lingtai_kernel.llm.base import FunctionSchema
from lingtai_kernel.message import MSG_REQUEST, _make_message

from .run_dir import DaemonRunDir

PROVIDERS = {"providers": [], "default": "builtin"}

# Tools emanations can never use (no recursion, no spawning, no identity mutation)
EMANATION_BLACKLIST = {"daemon", "avatar", "psyche", "library"}


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
                        "preset": {"type": "string"},
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
                 notify_threshold: int = 20, max_result_chars: int = 2000):
        self._agent = agent
        self._max_emanations = max_emanations
        self._max_turns = max_turns
        self._timeout = timeout
        self._default_model = agent.service.model
        self._notify_threshold = notify_threshold
        self._max_result_chars = max_result_chars

        # Emanation registry: em_id → entry dict
        self._emanations: dict[str, dict] = {}
        self._next_id = 1
        # Pool tracking for reclaim
        self._pools: list[tuple[ThreadPoolExecutor, threading.Event]] = []

    def handle(self, args: dict) -> dict:
        action = args.get("action")
        if action == "emanate":
            return self._handle_emanate(
                args.get("tasks", []),
                max_turns=args.get("max_turns"),
                timeout=args.get("timeout"),
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

    def _instantiate_preset_capabilities(
        self,
        preset_caps: dict,
        preset_llm: dict,
    ) -> tuple[dict, dict]:
        """Instantiate a preset's manifest.capabilities into a sandbox.

        Returns ``(schemas_by_name, handlers_by_name)``. Capabilities run
        their ``setup()`` against a ``_CapabilitySandbox`` so the parent's
        own tool registry is not mutated. ``provider: "inherit"`` sentinels
        in the preset's capability kwargs resolve against the *preset's*
        LLM, not the parent's — capabilities follow the body that hosts
        them.

        Raises ``ValueError`` for unknown or broken capabilities. The caller
        (``_handle_emanate``) converts that into a tool-level error and
        refuses the whole batch.
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
            from ...config_resolve import resolve_env
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

                # Intermediate text → notify parent
                if response.text:
                    self._notify_parent(em_id, response.text)

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

    def _notify_parent(self, em_id: str, text: str) -> None:
        """Send a [daemon] notification to parent's inbox."""
        notification = f"[daemon:{em_id}]\n\n{text}"
        msg = _make_message(MSG_REQUEST, "daemon", notification)
        self._agent.inbox.put(msg)

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
                        timeout: float | None = None) -> dict:
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

        # Clear completed emanations and stale pools
        self._emanations = {k: v for k, v in self._emanations.items()
                            if not v["future"].done()}
        self._pools = [(p, c) for p, c in self._pools if not c.is_set()]

        # Pre-flight: resolve any per-task presets BEFORE scheduling.
        # If any preset is invalid, refuse the whole batch. Presets are
        # identified by path (~/foo.json, ./foo.json, or absolute).
        from lingtai.presets import load_preset
        from lingtai.preset_connectivity import check_connectivity

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
                    preset_caps, preset_llm,
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
        if entry["future"].done():
            return {"status": "error", "message": "not running"}
        with entry["followup_lock"]:
            if entry["followup_buffer"]:
                entry["followup_buffer"] += "\n\n" + message
            else:
                entry["followup_buffer"] = message
        self._log("daemon_ask", em_id=em_id, message_length=len(message))
        return {"status": "sent", "id": em_id}

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
            "turn": state.get("turn"),
            "current_tool": state.get("current_tool"),
            "elapsed_s": state.get("elapsed_s"),
            "tokens": state.get("tokens", {}),
            "events": events,
            "events_total": events_total,
            "events_returned": len(events),
        }

    def _handle_reclaim(self) -> dict:
        cancelled = sum(1 for e in self._emanations.values()
                        if not e["future"].done())
        for pool, cancel in self._pools:
            cancel.set()
            pool.shutdown(wait=False, cancel_futures=True)
        self._pools.clear()
        self._emanations.clear()
        self._next_id = 1  # handles can be re-used; folder names disambiguate
        self._log("daemon_reclaim", cancelled_count=cancelled)
        return {"status": "reclaimed", "cancelled": cancelled}

    def _on_emanation_done(self, em_id: str, task_summary: str, future) -> None:
        elapsed = 0.0
        entry = self._emanations.get(em_id)
        if entry:
            elapsed = time.time() - entry["start_time"]
        try:
            text = future.result()
            self._log("daemon_result", em_id=em_id, status="done",
                      text_length=len(text), elapsed_ms=round(elapsed * 1000))
        except Exception as e:
            text = f"Failed: {e}"
            self._log("daemon_error", em_id=em_id,
                      exception=type(e).__name__, exception_message=str(e))

        # Truncate long results
        if len(text) > self._max_result_chars:
            text = text[:self._max_result_chars] + f"\n[truncated — {len(text)} chars total]"

        # Suppress notifications for short results to prevent notification storms
        if len(text) < self._notify_threshold:
            self._log("daemon_result", em_id=em_id, status="suppressed_short",
                      text_length=len(text))
        else:
            self._notify_parent(em_id, text)

    def _watchdog(self, cancel_event: threading.Event,
                  timeout_event: threading.Event, timeout: float) -> None:
        """Kill emanations that exceed the timeout.

        Sets timeout_event BEFORE cancel_event so the run loop can observe
        the timeout flag at its next checkpoint and call mark_timeout instead
        of mark_cancelled.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel_event.is_set():
                return
            time.sleep(1.0)
        timeout_event.set()
        cancel_event.set()

    def _log(self, event_type: str, **fields) -> None:
        """Log through the parent agent's logging system."""
        if hasattr(self._agent, '_log'):
            self._agent._log(event_type, **fields)


def setup(agent: "Agent", max_emanations: int = 10,
          max_turns: int = 200, timeout: float = 3600.0,
          notify_threshold: int = 20, max_result_chars: int = 2000) -> DaemonManager:
    """Set up the daemon capability on an agent."""
    lang = agent._config.language
    mgr = DaemonManager(agent, max_emanations=max_emanations,
                        max_turns=max_turns, timeout=timeout,
                        notify_threshold=notify_threshold,
                        max_result_chars=max_result_chars)
    schema = get_schema(lang)
    agent.add_tool("daemon", schema=schema, handler=mgr.handle,
                   description=get_description(lang))
    return mgr
