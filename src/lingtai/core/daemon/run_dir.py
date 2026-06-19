"""Per-emanation filesystem run directory.

Each daemon emanation gets one DaemonRunDir, which owns every filesystem
effect for that run: folder layout, daemon.json atomic writes, JSONL appends,
heartbeat touches, terminal state markers. The DaemonManager calls into a
DaemonRunDir at every hook (start, per-turn, per-tool-dispatch, terminal)
without itself touching the filesystem.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from lingtai_kernel.token_ledger import append_token_entry


class DaemonRunDir:
    """Filesystem-backed mini-avatar log surface for one daemon emanation.

    Folder layout:
        <parent>/daemons/em-<N>-<YYYYMMDD-HHMMSS>-<hash6>/
            daemon.json                  # identity card + live status
            .prompt                      # system prompt verbatim
            .heartbeat                   # mtime-touched on activity
            history/chat_history.jsonl   # session transcript
            logs/token_ledger.jsonl      # per-call tokens, daemon-scoped
            logs/events.jsonl            # tool_call, tool_result, cli_output, daemon_*
            result.txt                   # full terminal result when available
    """

    DATA_VERSION = 1

    def __init__(
        self,
        *,
        parent_working_dir: Path,
        handle: str,
        task: str,
        tools: list[str],
        model: str,
        max_turns: int,
        timeout_s: float,
        parent_addr: str,
        parent_pid: int,
        system_prompt: str,
        log_callback=None,
        preset_name: str | None = None,
        preset_provider: str | None = None,
        preset_model: str | None = None,
        backend: str = "lingtai",
        group_id: str | None = None,
        call_parameters: dict | None = None,
    ):
        self._handle = handle
        self._parent_token_ledger = parent_working_dir / "logs" / "token_ledger.jsonl"
        # Optional callback for swallowed OSError visibility — invoked as
        # log_callback("daemon_fs_error", op=<op_name>, error=<str(exc)>).
        # When None, _safe stays silent (preserves prior behavior for tests).
        self._log_callback = log_callback
        self._started_monotonic = time.monotonic()
        started_at_iso = self._now_iso()

        # run_id format: em-<N>-<YYYYMMDD-HHMMSS>-<6 hex>
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        hash6 = secrets.token_hex(3)
        self._run_id = f"{handle}-{timestamp}-{hash6}"

        self._path = parent_working_dir / "daemons" / self._run_id

        # Identity-card construction is strict — failures here propagate up to
        # _handle_emanate which converts them into a tool-level error response.
        self._path.mkdir(parents=True, exist_ok=False)
        (self._path / "history").mkdir()
        (self._path / "logs").mkdir()

        self._state = {
            "data_version": self.DATA_VERSION,
            "handle": handle,
            "run_id": self._run_id,
            "group_id": group_id,
            "parent_addr": parent_addr,
            "parent_pid": parent_pid,
            "task": task,
            "tools": list(tools),
            "call_parameters": dict(call_parameters or {}),
            "model": model,
            "max_turns": max_turns,
            "timeout_s": timeout_s,
            "state": "running",
            "started_at": started_at_iso,
            "finished_at": None,
            "elapsed_s": 0.0,
            "turn": 0,
            "current_tool": None,
            "tool_call_count": 0,
            "tokens": {"input": 0, "output": 0, "thinking": 0, "cached": 0},
            "result_preview": None,
            "result_path": None,
            "last_output": None,
            "last_output_at": None,
            "error": None,
            "preset_name": preset_name,
            "preset_provider": preset_provider,
            "preset_model": preset_model,
            "backend": backend,
            "claude_session_id": None,
        }

        self._atomic_write_json(self.daemon_json_path, self._state)
        self.prompt_path.write_text(system_prompt, encoding="utf-8")
        self.heartbeat_path.touch()
        self._append_jsonl(self.events_path,
                           {"event": "daemon_start", "ts": self._now_iso()})

    # ------------------------------------------------------------------
    # Path properties
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def handle(self) -> str:
        return self._handle

    @property
    def group_id(self) -> str | None:
        """The daemon batch group id this run belongs to (None if ungrouped)."""
        return self._state.get("group_id")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def daemon_json_path(self) -> Path:
        return self._path / "daemon.json"

    @property
    def prompt_path(self) -> Path:
        return self._path / ".prompt"

    @property
    def heartbeat_path(self) -> Path:
        return self._path / ".heartbeat"

    @property
    def chat_path(self) -> Path:
        return self._path / "history" / "chat_history.jsonl"

    @property
    def events_path(self) -> Path:
        return self._path / "logs" / "events.jsonl"

    @property
    def token_ledger_path(self) -> Path:
        return self._path / "logs" / "token_ledger.jsonl"

    @property
    def result_path(self) -> Path:
        return self._path / "result.txt"

    def state_snapshot(self) -> dict:
        """Return a shallow copy of the current daemon.json state."""
        return dict(self._state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def new_group_id() -> str:
        """Return a daemon batch group id shared by one emanate call."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"dg-{timestamp}-{secrets.token_hex(3)}"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _now_secs(self) -> float:
        return round(time.monotonic() - self._started_monotonic, 3)

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        """Write JSON to a tempfile then os.replace — readers never see partial state."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _append_jsonl(self, path: Path, entry: dict) -> None:
        """Append one JSON line. Single-writer per file — POSIX O_APPEND atomic for sub-PIPE_BUF lines."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _safe(self, op: str, fn) -> None:
        """Run `fn`; swallow OSError (best-effort policy for mutation writes).

        If a log_callback was provided at construction, the swallowed error is
        forwarded so the parent agent can record it without breaking the run.
        """
        try:
            fn()
        except OSError as e:
            if self._log_callback is not None:
                try:
                    self._log_callback(
                        "daemon_fs_error",
                        em_id=self._handle,
                        run_id=self._run_id,
                        op=op,
                        error=str(e),
                    )
                except Exception:
                    # Logging itself must never break the run — secondary
                    # failure is silent by design.
                    pass

    # ------------------------------------------------------------------
    # Per-turn hooks
    # ------------------------------------------------------------------

    def record_user_send(self, text: str, kind: str) -> None:
        """Append a user-role entry to chat_history.jsonl before session.send.

        kind ∈ {"task", "tool_results", "followup"}. Tool result payloads are
        written verbatim — no truncation. Chat history is forensic; we want
        full fidelity. Single-writer per file (only the run thread).
        """
        def _write():
            self._append_jsonl(
                self.chat_path,
                {
                    "role": "user",
                    "text": text,
                    "kind": kind,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("record_user_send", _write)

    def bump_turn(self, turn: int, response_text: str) -> None:
        """Mark the end of an LLM round.

        Updates daemon.json (turn, elapsed_s, current_tool=null) atomically,
        appends an assistant entry to chat_history, touches heartbeat.
        """
        def _write():
            self._state["turn"] = turn
            self._state["current_tool"] = None
            self._state["elapsed_s"] = self._now_secs()
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.chat_path,
                {
                    "role": "assistant",
                    "text": response_text,
                    "turn": turn,
                    "ts": self._now_iso(),
                },
            )
            self.heartbeat_path.touch()
        self._safe("bump_turn", _write)

    # ------------------------------------------------------------------
    # Tool dispatch hooks
    # ------------------------------------------------------------------

    _ARGS_PREVIEW_MAX = 500

    def set_current_tool(self, name: str, args: dict) -> None:
        """Mark a tool dispatch starting.

        Increments tool_call_count, sets current_tool, logs tool_call event,
        touches heartbeat. Tracked tool name (current_tool) is what the parent
        sees on a `cat daemon.json` poll.
        """
        def _write():
            self._state["current_tool"] = name
            self._state["tool_call_count"] += 1
            self._atomic_write_json(self.daemon_json_path, self._state)
            args_preview = json.dumps(args, ensure_ascii=False)
            if len(args_preview) > self._ARGS_PREVIEW_MAX:
                suffix = "...[truncated]"
                args_preview = args_preview[: self._ARGS_PREVIEW_MAX - len(suffix)] + suffix
            self._append_jsonl(
                self.events_path,
                {
                    "event": "tool_call",
                    "name": name,
                    "args_preview": args_preview,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
            self.heartbeat_path.touch()
        self._safe("set_current_tool", _write)

    def clear_current_tool(self, result_status: str) -> None:
        """Mark a tool dispatch finished.

        Clears current_tool in daemon.json, logs tool_result event.
        result_status is "ok" on normal returns or "error" when the handler
        raised or returned {"status": "error", ...}.
        """
        def _write():
            tool_name = self._state["current_tool"]
            self._state["current_tool"] = None
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "tool_result",
                    "name": tool_name,
                    "status": result_status,
                    "turn": self._state["turn"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("clear_current_tool", _write)


    # ------------------------------------------------------------------
    # External CLI backend hooks
    # ------------------------------------------------------------------

    _CLI_OUTPUT_EVENT_MAX = 4000
    _LAST_OUTPUT_MAX = 1000

    def record_cli_output(self, text: str, *, stream: str = "stdout") -> None:
        """Record one stdout/stderr progress line from an external CLI backend.

        CLI backends (Claude Code / Codex) do not run through the LingTai
        ChatSession tool loop, so ``turn`` and ``current_tool`` stay mostly
        static while the child process works.  This hook makes their progress
        visible to ``daemon(check)`` by appending bounded ``cli_output`` events,
        updating a small ``last_output`` field in daemon.json, and touching the
        heartbeat.  The final full output is still captured by ``mark_done``.
        """
        text = text.rstrip("\n")
        if not text:
            return
        if stream not in ("stdout", "stderr", "combined"):
            stream = "stdout"
        event_text = text
        truncated = False
        if len(event_text) > self._CLI_OUTPUT_EVENT_MAX:
            event_text = event_text[:self._CLI_OUTPUT_EVENT_MAX] + "...[truncated]"
            truncated = True
        last_output = text
        if len(last_output) > self._LAST_OUTPUT_MAX:
            last_output = last_output[-self._LAST_OUTPUT_MAX:]

        def _write():
            ts = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["last_output"] = last_output
            self._state["last_output_at"] = ts
            self._atomic_write_json(self.daemon_json_path, self._state)
            entry = {
                "event": "cli_output",
                "stream": stream,
                "text": event_text,
                "elapsed_s": self._state["elapsed_s"],
                "ts": ts,
            }
            if truncated:
                entry["truncated"] = True
            self._append_jsonl(self.events_path, entry)
            self.heartbeat_path.touch()
        self._safe("record_cli_output", _write)

    # ------------------------------------------------------------------
    # Token accounting — dual ledger writes
    # ------------------------------------------------------------------

    def append_tokens(self, *, input: int, output: int,
                     thinking: int, cached: int,
                     model: str | None = None,
                     endpoint: str | None = None) -> None:
        """Record per-call token usage to both ledgers.

        Daemon's own logs/token_ledger.jsonl gets an untagged entry (the
        location is already attribution enough). Parent's logs/token_ledger.jsonl
        gets a tagged entry with source/em_id/run_id so future analytics can
        decompose, while existing sum_token_ledger callers continue to count
        daemon spend in the parent's lifetime totals (they only read the
        numeric fields).

        ``model`` and ``endpoint`` (if provided) are written as first-class
        attribution fields on both ledgers — the daemon may use a different
        model/provider than the parent, so per-entry tagging is required for
        multi-provider cost analytics.

        Skips both writes if all four values are zero — avoids ledger noise
        from LLM calls that returned no usage.

        Each write is independently fault-tolerant — if the parent's ledger
        write fails, the daemon's local ledger is still authoritative.
        """
        if not (input or output or thinking or cached):
            return

        # Update running totals in daemon.json
        def _update_state():
            self._state["tokens"]["input"] += input
            self._state["tokens"]["output"] += output
            self._state["tokens"]["thinking"] += thinking
            self._state["tokens"]["cached"] += cached
            self._atomic_write_json(self.daemon_json_path, self._state)
        self._safe("append_tokens.state", _update_state)

        # Daemon's own ledger — tagged source=daemon for uniformity with
        # parent's ledger and main/soul writes (every entry self-describes).
        self._safe(
            "append_tokens.daemon_ledger",
            lambda: append_token_entry(
                self.token_ledger_path,
                input=input, output=output,
                thinking=thinking, cached=cached,
                model=model, endpoint=endpoint,
                extra={"source": "daemon", "em_id": self._handle,
                       "run_id": self._run_id},
            ),
        )

        # Parent's ledger — same tags so daemon spend is identifiable in
        # the parent's lifetime totals.
        self._safe(
            "append_tokens.parent_ledger",
            lambda: append_token_entry(
                self._parent_token_ledger,
                input=input, output=output,
                thinking=thinking, cached=cached,
                model=model, endpoint=endpoint,
                extra={"source": "daemon", "em_id": self._handle,
                       "run_id": self._run_id},
            ),
        )

    # ------------------------------------------------------------------
    # Terminal markers
    # ------------------------------------------------------------------

    _RESULT_PREVIEW_MAX = 200

    def mark_done(self, text: str) -> None:
        """Normal completion. Sets state=done, finished_at, result_preview.

        The complete terminal text is written to ``result.txt`` for deliberate
        inspection; daemon.json keeps only a bounded preview so list/check stay
        compact.  A failure to write result.txt must not prevent the terminal
        state transition.
        """
        text = text or ""

        def _write():
            result_path = None
            try:
                self.result_path.write_text(text, encoding="utf-8")
                result_path = str(self.result_path)
            except OSError as e:
                if self._log_callback is not None:
                    try:
                        self._log_callback(
                            "daemon_fs_error",
                            em_id=self._handle,
                            run_id=self._run_id,
                            op="mark_done.result",
                            error=str(e),
                        )
                    except Exception:
                        pass
            self._state["state"] = "done"
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            preview = text
            if len(preview) > self._RESULT_PREVIEW_MAX:
                preview = preview[:self._RESULT_PREVIEW_MAX]
            self._state["result_preview"] = preview
            self._state["result_path"] = result_path
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "daemon_done",
                    "elapsed_s": self._state["elapsed_s"],
                    "result_path": result_path,
                    "ts": self._now_iso(),
                },
            )
            self.heartbeat_path.touch()
        self._safe("mark_done", _write)

    def mark_failed(self, exc: BaseException) -> None:
        """Exception in run loop. Sets state=failed, error.{type, message}.

        Defensive: a user-defined exception's `__str__` may itself raise
        (TypeError, AttributeError, ...). _safe only catches OSError, so we
        materialize the message string before entering the closure.
        """
        try:
            msg = str(exc)
        except Exception:
            msg = f"<unrenderable {type(exc).__name__}>"

        def _write():
            self._state["state"] = "failed"
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            self._state["error"] = {
                "type": type(exc).__name__,
                "message": msg,
            }
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": "daemon_error",
                    "exception": type(exc).__name__,
                    "message": msg,
                    "elapsed_s": self._state["elapsed_s"],
                    "ts": self._now_iso(),
                },
            )
        self._safe("mark_failed", _write)

    def mark_cancelled(self) -> None:
        """Cancel event observed. Sets state=cancelled."""
        self._mark_terminal("cancelled", "daemon_cancelled")

    def mark_timeout(self) -> None:
        """Watchdog timeout. Sets state=timeout."""
        self._mark_terminal("timeout", "daemon_timeout")

    def _mark_terminal(self, state: str, event: str) -> None:
        def _write():
            self._state["state"] = state
            self._state["finished_at"] = self._now_iso()
            self._state["elapsed_s"] = self._now_secs()
            self._state["current_tool"] = None
            self._atomic_write_json(self.daemon_json_path, self._state)
            self._append_jsonl(
                self.events_path,
                {
                    "event": event,
                    "elapsed_s": self._state["elapsed_s"],
                    "ts": self._now_iso(),
                },
            )
        self._safe(f"mark_{state}", _write)
