"""Interactive Claude Code daemon backend.

This module drives the *interactive* ``claude`` TUI through a real PTY and
uses Claude Code's hook + transcript surfaces to turn one daemon task into a
programmatic result.  It intentionally does not call ``claude --print``: that
legacy/official print-mode route remains in ``DaemonManager`` as the
``claude-p``/``claude-code`` backend.

The bridge is deliberately conservative:
- no direct editing of ``~/.claude.json`` or global Claude configuration;
- no login/MFA/token/cookie handling;
- no web/mobile UI automation;
- no auto-acceptance of trust/onboarding prompts for arbitrary directories.

For headless operation it creates a LingTai-managed ephemeral workspace under
``~/.lingtai-claude/runs/<daemon-run-id>/worktree`` (or the
``LINGTAI_CLAUDE_MANAGED_ROOT`` test override).  Only inside that managed
workspace may the bridge auto-select Claude Code's workspace trust prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import pty
import queue
import re
import select
import shlex
import subprocess
import threading
import time
from typing import Callable

from .run_dir import DaemonRunDir
from .runtime import kill_process_group


@dataclass(slots=True)
class ClaudeInteractiveResult:
    """Terminal result of one interactive Claude daemon turn."""

    final_text: str
    session_id: str | None = None
    transcript_path: str | None = None
    raw_pty_log_path: str | None = None


@dataclass(slots=True)
class _HookEvent:
    event: str
    payload: dict


_TERMINAL_RESPONSES: tuple[tuple[bytes, bytes], ...] = (
    (b"\x1b[c", b"\x1b[?1;2c"),
    (b"\x1b[0c", b"\x1b[?1;2c"),
    (b"\x1b[>c", b"\x1b[>0;0;0c"),
    (b"\x1b[>0c", b"\x1b[>0;0;0c"),
    (b"\x1b[6n", b"\x1b[1;1R"),
    (b"\x1b[>q", b"\x1bP>|LingTai Claude Interactive\x1b\\"),
    (b"\x1b[>0q", b"\x1bP>|LingTai Claude Interactive\x1b\\"),
    (b"\x1b[18t", b"\x1b[8;40;120t"),
)

_AUTH_OR_ONBOARDING_PROMPTS = (
    "login to claude",
    "not logged in",
    "please log in",
    "press enter to continue",
)

_TRUST_PROMPTS = (
    "do you trust",
    "trust this folder",
    "project you created",
)

_MANAGED_SYSTEM_PROMPT = """
You are Claude Code running as a LingTai daemon backend inside a
LingTai-managed ephemeral workspace.  Operational boundaries:

- Your current working directory is a LingTai-created managed workspace for
  this daemon run.  Treat it as the only trusted workspace for this session.
- Do not modify global Claude, shell, git, package-manager, or operating-system
  configuration.
- Do not handle, request, echo, store, or transform credentials, cookies,
  session tokens, API keys, MFA codes, recovery codes, SSH keys, or browser
  profiles.  If a task needs them, stop and report the blocker.
- Do not intentionally leave the managed workspace unless the user task
  explicitly asks for read-only inspection of another path.  Never make writes
  outside the managed workspace.
- Prefer producing a concise final summary that includes files changed, tests or
  checks run, and any blockers.
""".strip()


class ClaudeInteractiveError(RuntimeError):
    """Raised when the interactive Claude bridge cannot finish a turn."""


class ClaudeInteractiveBridge:
    """Drive one interactive Claude Code turn through a PTY.

    The public entry point is :meth:`run`.  The bridge creates per-run hook
    plumbing inside the daemon run directory, starts ``claude`` under a PTY,
    answers the small set of terminal probes observed from Ink/Claude Code,
    waits for ``SessionStart`` to paste the prompt, then waits for ``Stop`` to
    parse Claude's transcript JSONL.
    """

    def __init__(
        self,
        *,
        em_id: str,
        run_dir: DaemonRunDir,
        working_dir: Path,
        task: str,
        cancel_event: threading.Event,
        timeout_event: threading.Event | None = None,
        backend_argv: list[str] | None = None,
        resume_session_id: str | None = None,
        env: dict[str, str] | None = None,
        log_callback: Callable[..., None] | None = None,
    ) -> None:
        self.em_id = em_id
        self.run_dir = run_dir
        self.working_dir = working_dir
        self.task = task
        self.cancel_event = cancel_event
        self.timeout_event = timeout_event
        self.backend_argv, self._managed_worktree_from = self._split_internal_backend_argv(
            list(backend_argv or [])
        )
        self.resume_session_id = resume_session_id
        self.env = dict(env or os.environ)
        self.log_callback = log_callback or (lambda *args, **kwargs: None)

        managed_root = Path(
            self.env.get("LINGTAI_CLAUDE_MANAGED_ROOT")
            or Path.home() / ".lingtai-claude"
        ).expanduser()
        self.managed_runs_root = managed_root / "runs"
        self.managed_run_root = self.managed_runs_root / run_dir.run_id
        self.managed_worktree_path = self.managed_run_root / "worktree"
        self.prompt_dir = self.managed_run_root / "prompt"
        self.system_prompt_path = self.prompt_dir / "lingtai-system-prompt.md"

        self.harness_dir = self.managed_run_root / "harness"
        self.fifo_path = self.harness_dir / "hooks.fifo"
        self.hook_script_path = self.harness_dir / "hook-relay.sh"
        self.raw_pty_log_path = self.harness_dir / "pty.ansi.log"

        self.claude_cwd = self.managed_worktree_path
        self._auto_trust_workspace = False
        self._trust_prompt_answered = False

        self._hook_events: queue.Queue[_HookEvent] = queue.Queue()
        self._hook_done = threading.Event()
        self._pty_tail = b""
        self._prompt_sent = False
        self._stop_payload: dict | None = None
        self._transcript_path: str | None = None
        self._session_id: str | None = None
        self._last_assistant_message: str | None = None
        self._prompt_warning: str | None = None

    # ------------------------------------------------------------------
    # Harness setup
    # ------------------------------------------------------------------

    @staticmethod
    def _split_internal_backend_argv(argv: list[str]) -> tuple[list[str], Path | None]:
        """Split LingTai-owned backend flags from Claude CLI passthrough flags."""
        passthrough: list[str] = []
        managed_worktree_from: Path | None = None
        i = 0
        while i < len(argv):
            token = argv[i]
            if token == "--managed-worktree-from":
                if i + 1 >= len(argv):
                    raise ClaudeInteractiveError(
                        "--managed-worktree-from requires a source repository path"
                    )
                managed_worktree_from = Path(argv[i + 1]).expanduser()
                i += 2
                continue
            if token.startswith("--managed-worktree-from="):
                managed_worktree_from = Path(token.split("=", 1)[1]).expanduser()
                i += 1
                continue
            passthrough.append(token)
            i += 1
        return passthrough, managed_worktree_from

    def _log(self, event: str, **fields) -> None:
        try:
            self.log_callback(event, **fields)
        except TypeError:
            # Some tests pass simple callables; logging must never break the
            # daemon run itself.
            pass

    def _write_state(self, **fields) -> None:
        self.run_dir._state.update(fields)
        self.run_dir._atomic_write_json(
            self.run_dir.daemon_json_path, self.run_dir._state,
        )

    def _build_settings_json(self) -> str:
        script = shlex.quote(str(self.hook_script_path))

        def hook(event: str) -> list[dict]:
            return [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": f"{script} {event}"}],
            }]

        return json.dumps({
            "hooks": {
                "SessionStart": hook("SessionStart"),
                "Stop": hook("Stop"),
            },
        }, separators=(",", ":"))

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _git_root(self, path: Path) -> Path | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        root = proc.stdout.strip()
        return Path(root) if root else None

    def _prepare_managed_workspace(self) -> None:
        runs_root = Path(os.path.realpath(self.managed_runs_root))
        self.managed_runs_root.mkdir(parents=True, exist_ok=True)
        self.managed_run_root.mkdir(parents=True, exist_ok=True)
        self.prompt_dir.mkdir(parents=True, exist_ok=True)

        if self.managed_run_root.is_symlink() or self.managed_worktree_path.is_symlink():
            raise ClaudeInteractiveError(
                "Refusing managed Claude workspace with symlinked run/worktree path"
            )

        source_path = self._managed_worktree_from
        source_git_root = self._git_root(source_path) if source_path is not None else None
        if source_path is not None and source_git_root is None:
            raise ClaudeInteractiveError(
                "--managed-worktree-from must point inside a git repository"
            )
        if not self.managed_worktree_path.exists():
            if source_git_root is not None:
                try:
                    subprocess.run(
                        [
                            "git", "-C", str(source_git_root),
                            "worktree", "add", "--detach",
                            str(self.managed_worktree_path), "HEAD",
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=60,
                        check=True,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                    detail = getattr(exc, "stderr", "") or str(exc)
                    raise ClaudeInteractiveError(
                        f"Failed to create managed Claude git worktree: {detail}"
                    ) from exc
            else:
                self.managed_worktree_path.mkdir(parents=True, exist_ok=False)

        if self.managed_worktree_path.is_symlink() or not self.managed_worktree_path.is_dir():
            raise ClaudeInteractiveError(
                "Managed Claude workspace worktree is not a real directory"
            )

        worktree_real = Path(os.path.realpath(self.managed_worktree_path))
        if not self._is_relative_to(worktree_real, runs_root):
            raise ClaudeInteractiveError(
                "Managed Claude workspace escaped the LingTai-managed runs root"
            )

        self.system_prompt_path.write_text(_MANAGED_SYSTEM_PROMPT + "\n", encoding="utf-8")
        self.claude_cwd = self.managed_worktree_path
        self._auto_trust_workspace = True
        self._write_state(
            claude_interactive_managed_root=str(self.managed_run_root),
            claude_interactive_managed_worktree=str(self.managed_worktree_path),
            claude_interactive_managed_source=str(source_git_root) if source_git_root else None,
            claude_interactive_managed_source_request=(
                str(source_path) if source_path is not None else None
            ),
            claude_interactive_system_prompt=str(self.system_prompt_path),
            claude_interactive_auto_trust="managed-workspace-only",
        )

    def _prepare_harness(self) -> str:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        if self.fifo_path.exists():
            self.fifo_path.unlink()
        os.mkfifo(self.fifo_path)
        self.hook_script_path.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "event=\"$1\"\n"
            "fifo=\"${LINGTAI_CLAUDE_INTERACTIVE_FIFO:?missing fifo}\"\n"
            "payload=\"$(cat)\"\n"
            "printf '%s\\t%s\\n' \"$event\" \"$payload\" >> \"$fifo\"\n",
            encoding="utf-8",
        )
        self.hook_script_path.chmod(0o700)
        self.raw_pty_log_path.write_bytes(b"")
        settings_json = self._build_settings_json()
        self._write_state(
            claude_interactive_hook_fifo=str(self.fifo_path),
            claude_interactive_raw_pty_log=str(self.raw_pty_log_path),
        )
        return settings_json

    # ------------------------------------------------------------------
    # Hook and transcript parsing
    # ------------------------------------------------------------------

    def _hook_reader(self) -> None:
        fd: int | None = None
        try:
            fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
            buf = b""
            while not self._hook_done.is_set():
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(fd, 8192)
                if not chunk:
                    time.sleep(0.02)
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        event_b, payload_b = line.split(b"\t", 1)
                        payload = json.loads(payload_b.decode("utf-8"))
                        event = event_b.decode("utf-8", errors="replace")
                    except Exception as exc:  # pragma: no cover - defensive
                        self._log("daemon_claude_interactive_bad_hook",
                                  em_id=self.em_id, error=str(exc))
                        continue
                    self._hook_events.put(_HookEvent(event=event, payload=payload))
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _remember_hook_payload(self, payload: dict) -> None:
        sid = payload.get("session_id") or payload.get("sessionId")
        if sid and sid != self._session_id:
            self._session_id = str(sid)
            self._write_state(claude_session_id=self._session_id)
            self._log("daemon_claude_interactive_session",
                      em_id=self.em_id, session_id=self._session_id)

        transcript = payload.get("transcript_path") or payload.get("transcriptPath")
        if transcript:
            self._transcript_path = str(transcript)
            self._write_state(claude_interactive_transcript_path=self._transcript_path)

        last_msg = payload.get("last_assistant_message") or payload.get("lastAssistantMessage")
        if isinstance(last_msg, str) and last_msg.strip():
            self._last_assistant_message = last_msg.strip()

    def _handle_hook_events(self, master_fd: int) -> None:
        while True:
            try:
                item = self._hook_events.get_nowait()
            except queue.Empty:
                return
            self._remember_hook_payload(item.payload)
            if item.event == "SessionStart" and not self._prompt_sent:
                self.run_dir.record_cli_output(
                    "[claude interactive SessionStart]", stream="stdout",
                )
                self._send_prompt(master_fd)
            elif item.event == "Stop":
                self.run_dir.record_cli_output(
                    "[claude interactive Stop]", stream="stdout",
                )
                self._stop_payload = item.payload

    def _parse_transcript_once(self, path: Path) -> tuple[str | None, str | None]:
        """Parse one Claude transcript JSONL snapshot.

        Returns ``(final_text, session_id)``.  Parsing is deliberately tolerant:
        Claude Code's transcript format is not a stable API, so unknown rows are
        ignored and only the common assistant/user/tool fields are projected into
        daemon progress events.
        """
        final_texts: list[str] = []
        session_id: str | None = None
        pending_tools: dict[str, str] = {}
        seen_texts: set[str] = set()

        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                session_id = (
                    obj.get("session_id") or obj.get("sessionId") or session_id
                )
                if obj.get("type") == "custom-title":
                    session_id = obj.get("sessionId") or session_id
                    continue

                message = obj.get("message") or obj
                content = message.get("content") or []
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                if not isinstance(content, list):
                    continue

                if obj.get("type") == "assistant" or message.get("role") == "assistant":
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                final_texts.append(text)
                                if text not in seen_texts:
                                    seen_texts.add(text)
                                    self.run_dir.record_cli_output(text, stream="stdout")
                        elif btype == "tool_use":
                            tool_id = block.get("id") or ""
                            tool_name = block.get("name") or "unknown"
                            tool_input = block.get("input") or {}
                            if tool_id:
                                pending_tools[tool_id] = tool_name
                            self.run_dir.set_current_tool(tool_name, tool_input)
                elif obj.get("type") == "user" or message.get("role") == "user":
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        tool_id = block.get("tool_use_id") or ""
                        status = "error" if block.get("is_error") else "ok"
                        if tool_id in pending_tools:
                            pending_tools.pop(tool_id, None)
                        self.run_dir.clear_current_tool(status)

        while pending_tools:
            pending_tools.popitem()
            self.run_dir.clear_current_tool("ok")

        return ("\n\n".join(final_texts).strip() or None, session_id)

    def _parse_transcript_with_retry(self) -> tuple[str | None, str | None]:
        if not self._transcript_path:
            return (self._last_assistant_message, self._session_id)
        path = Path(self._transcript_path).expanduser()
        final_text: str | None = None
        session_id: str | None = self._session_id
        last_error: Exception | None = None
        for _ in range(20):
            try:
                if path.exists() and path.stat().st_size > 0:
                    final_text, parsed_session = self._parse_transcript_once(path)
                    session_id = parsed_session or session_id
                    if final_text:
                        break
            except Exception as exc:  # transcript may still be flushing
                last_error = exc
            time.sleep(0.05)
        if last_error and not final_text:
            self._log("daemon_claude_interactive_transcript_error",
                      em_id=self.em_id, error=str(last_error))
        return (final_text or self._last_assistant_message, session_id)

    # ------------------------------------------------------------------
    # PTY driving
    # ------------------------------------------------------------------

    def _respond_to_terminal_probes(self, master_fd: int, data: bytes) -> None:
        self._pty_tail = (self._pty_tail + data)[-512:]
        for probe, response in _TERMINAL_RESPONSES:
            if probe in self._pty_tail:
                try:
                    os.write(master_fd, response)
                except OSError:
                    return
                self._pty_tail = self._pty_tail.replace(probe, b"")

    def _normalized_prompt_text(self, data: bytes) -> tuple[str, str]:
        text = data.decode("utf-8", errors="ignore").lower()
        # The TUI interleaves ANSI cursor movement/control bytes between words,
        # so normalize to a loose printable stream before matching prompts.
        normalized = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|P.*?\x1b\\)", " ", text)
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
        return text, normalized

    @staticmethod
    def _contains_marker(text: str, normalized: str, markers: tuple[str, ...]) -> bool:
        return any(marker in text for marker in markers) or any(
            marker in normalized for marker in markers
        )

    def _handle_auth_or_trust_prompt(self, master_fd: int, data: bytes) -> None:
        if self._prompt_warning:
            return
        text, normalized = self._normalized_prompt_text(data)

        if self._contains_marker(text, normalized, _TRUST_PROMPTS):
            if self._auto_trust_workspace:
                if self._trust_prompt_answered:
                    # The TUI can repaint the same prompt before consuming our
                    # answer. Do not treat a repeated frame as an arbitrary
                    # workspace trust request; let the process advance or time
                    # out normally.
                    return
                try:
                    os.write(master_fd, b"1\r")
                except OSError:
                    return
                self._trust_prompt_answered = True
                msg = (
                    "Claude interactive backend auto-selected workspace trust "
                    "inside a LingTai-managed Claude worktree."
                )
                self.run_dir.record_cli_output(msg, stream="stderr")
                self._write_state(claude_interactive_managed_trust_answered=True)
                return
            self._prompt_warning = (
                "Claude interactive backend appears to be waiting for a workspace "
                "trust prompt outside a verified LingTai-managed worktree. "
                "LingTai will not auto-accept arbitrary workspace trust; run "
                "Claude manually in that workspace or use a managed workspace."
            )
            self.run_dir.record_cli_output(self._prompt_warning, stream="stderr")
            return

        if self._contains_marker(text, normalized, _AUTH_OR_ONBOARDING_PROMPTS):
            self._prompt_warning = (
                "Claude interactive backend appears to be waiting for a "
                "login/onboarding prompt; LingTai will not auto-accept or handle "
                "credentials."
            )
            self.run_dir.record_cli_output(self._prompt_warning, stream="stderr")

    def _send_prompt(self, master_fd: int) -> None:
        payload = self.task.encode("utf-8")
        # Bracketed paste avoids treating prompt content as terminal control
        # input in most readline/Ink text areas.  Fall back naturally if Claude
        # ignores bracketed paste markers.
        framed = b"\x1b[200~" + payload + b"\x1b[201~\r"
        os.write(master_fd, framed)
        self._prompt_sent = True
        self._write_state(claude_interactive_prompt_sent=True)

    def _command(self, settings_json: str) -> list[str]:
        cmd = ["claude"]
        if self.resume_session_id:
            cmd.extend(["--resume", self.resume_session_id])
        cmd.extend(["--settings", settings_json])
        cmd.extend(["--append-system-prompt-file", str(self.system_prompt_path)])
        if self.backend_argv:
            cmd.extend(self.backend_argv)
        return cmd

    def run(self) -> ClaudeInteractiveResult:
        self._prepare_managed_workspace()
        settings_json = self._prepare_harness()
        hook_thread = threading.Thread(
            target=self._hook_reader,
            daemon=True,
            name=f"daemon-claude-interactive-hooks-{self.em_id}",
        )
        hook_thread.start()

        env = dict(self.env)
        env["LINGTAI_CLAUDE_INTERACTIVE_FIFO"] = str(self.fifo_path)
        cmd = self._command(settings_json)
        self._log(
            "daemon_claude_interactive_start",
            em_id=self.em_id,
            cmd=" ".join(cmd),
            cwd=str(self.claude_cwd),
        )
        self._write_state(claude_interactive_command=cmd, claude_interactive_cwd=str(self.claude_cwd))

        master_fd: int | None = None
        proc: subprocess.Popen | None = None
        try:
            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    cwd=str(self.claude_cwd),
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                os.close(slave_fd)
        except FileNotFoundError as exc:
            raise ClaudeInteractiveError("'claude' CLI not found on PATH") from exc
        except OSError as exc:
            raise ClaudeInteractiveError(f"Failed to start interactive claude CLI: {exc}") from exc

        stop_seen_at: float | None = None
        exited_seen_at: float | None = None
        try:
            assert master_fd is not None
            with self.raw_pty_log_path.open("ab") as raw_log:
                while True:
                    if self.cancel_event.is_set():
                        kill_process_group(proc, term_timeout=2.0, kill_timeout=1.0)
                        break

                    self._handle_hook_events(master_fd)
                    if self._stop_payload is not None and stop_seen_at is None:
                        stop_seen_at = time.monotonic()

                    ready, _, _ = select.select([master_fd], [], [], 0.05)
                    if ready:
                        try:
                            data = os.read(master_fd, 8192)
                        except OSError:
                            data = b""
                        if data:
                            raw_log.write(data)
                            raw_log.flush()
                            self._respond_to_terminal_probes(master_fd, data)
                            self._handle_auth_or_trust_prompt(master_fd, data)
                            if self._prompt_warning:
                                kill_process_group(proc, term_timeout=2.0, kill_timeout=1.0)
                                break

                    # After Stop, Claude's turn is complete.  Give the process a
                    # small grace period to exit naturally, then terminate the
                    # TUI so the daemon can finish deterministically.
                    if stop_seen_at is not None:
                        if proc.poll() is not None:
                            break
                        if time.monotonic() - stop_seen_at > 0.25:
                            kill_process_group(proc, term_timeout=2.0, kill_timeout=1.0)
                            break

                    # A fast fake (and occasionally a fast real failure) can
                    # exit before the hook-reader thread has drained the FIFO.
                    # Keep the loop alive briefly after process exit so queued
                    # SessionStart/Stop lines can be processed before we decide
                    # that no Stop hook was observed.
                    if proc.poll() is not None:
                        if exited_seen_at is None:
                            exited_seen_at = time.monotonic()
                        if self._stop_payload is not None:
                            break
                        if time.monotonic() - exited_seen_at > 1.0:
                            break

            if proc.poll() is None:
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    kill_process_group(proc, term_timeout=2.0, kill_timeout=1.0)
        finally:
            self._hook_done.set()
            # Wake the nonblocking FIFO reader if it is sitting in select/read.
            try:
                fd = os.open(self.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass
            hook_thread.join(timeout=1.0)
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass

        if self.cancel_event.is_set():
            return ClaudeInteractiveResult(
                final_text="[cancelled]",
                session_id=self._session_id,
                transcript_path=self._transcript_path,
                raw_pty_log_path=str(self.raw_pty_log_path),
            )

        rc = proc.returncode if proc is not None else None
        # When Stop fired we may have intentionally terminated the still-open
        # interactive TUI.  Treat that as successful; the transcript is the
        # source of truth for the turn result.
        if self._stop_payload is None and self._prompt_warning:
            raise ClaudeInteractiveError(self._prompt_warning)
        if self._stop_payload is None and rc not in (0, None):
            raise ClaudeInteractiveError(
                f"interactive claude CLI exited with code {rc}; "
                f"see {self.raw_pty_log_path}"
            )
        if self._stop_payload is None:
            detail = f"see {self.raw_pty_log_path}"
            raise ClaudeInteractiveError(
                "interactive claude CLI exited before a Stop hook was observed; "
                f"{detail}"
            )

        final_text, transcript_session = self._parse_transcript_with_retry()
        if transcript_session and transcript_session != self._session_id:
            self._session_id = transcript_session
            self._write_state(claude_session_id=self._session_id)

        text = (final_text or "").strip() or "[no output]"
        return ClaudeInteractiveResult(
            final_text=text,
            session_id=self._session_id,
            transcript_path=self._transcript_path,
            raw_pty_log_path=str(self.raw_pty_log_path),
        )


def run_claude_interactive(
    *,
    em_id: str,
    run_dir: DaemonRunDir,
    working_dir: Path,
    task: str,
    cancel_event: threading.Event,
    timeout_event: threading.Event | None = None,
    backend_argv: list[str] | None = None,
    resume_session_id: str | None = None,
    env: dict[str, str] | None = None,
    log_callback: Callable[..., None] | None = None,
) -> ClaudeInteractiveResult:
    """Convenience wrapper used by ``DaemonManager`` and tests."""

    return ClaudeInteractiveBridge(
        em_id=em_id,
        run_dir=run_dir,
        working_dir=working_dir,
        task=task,
        cancel_event=cancel_event,
        timeout_event=timeout_event,
        backend_argv=backend_argv,
        resume_session_id=resume_session_id,
        env=env,
        log_callback=log_callback,
    ).run()
