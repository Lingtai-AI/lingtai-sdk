"""Bash capability — shell command execution with file-based policy.

Adds the ability to run shell commands. This is a capability (not intrinsic)
because not every agent should have shell access — it's a powerful
capability that should be explicitly opted into.

Usage:
    agent.add_capability("bash", policy_file="path/to/policy.json")
    agent.add_capability("bash", yolo=True)  # no restrictions
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ...i18n import t

if TYPE_CHECKING:
    from lingtai_kernel.base_agent import BaseAgent

PROVIDERS = {"providers": [], "default": "builtin"}

_DEFAULT_POLICY_FILE = Path(__file__).parent / "bash_policy.json"

def get_description(lang: str = "en") -> str:
    return t(lang, "bash.description")


def get_schema(lang: str = "en") -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["run", "poll", "cancel"],
                "description": t(lang, "bash.action"),
                "default": "run",
            },
            "command": {
                "type": "string",
                "description": t(lang, "bash.command"),
            },
            "timeout": {
                "type": "number",
                "description": t(lang, "bash.timeout"),
                "default": 30,
            },
            "working_dir": {
                "type": "string",
                "description": t(lang, "bash.working_dir"),
            },
            "async": {
                "type": "boolean",
                "description": t(lang, "bash.async"),
                "default": False,
            },
            "job_id": {
                "type": "string",
                "description": t(lang, "bash.job_id"),
            },
        },
        "required": [],  # command required only for action=run; job_id for poll/cancel
    }



class BashPolicy:
    """Command execution policy — allow/deny lists with pipe awareness.

    Two modes, determined by the policy file content:
    - **Denylist mode** (only ``deny`` key): everything allowed except denied commands.
    - **Allowlist mode** (``allow`` key present): only listed commands allowed,
      everything else blocked. ``deny`` key is ignored in this mode.

    The mode is implicit — if ``allow`` is present, it's allowlist mode.
    """

    def __init__(self, allow: list[str] | None = None, deny: list[str] | None = None):
        self._allow = set(allow) if allow else None
        # deny is only used in denylist mode (when allow is absent)
        self._deny = set(deny) if deny and not allow else None

    @classmethod
    def from_file(cls, path: str) -> "BashPolicy":
        """Load policy from a JSON file with allow/deny lists."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"Policy file not found: {path}")
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(allow=data.get("allow"), deny=data.get("deny"))

    @classmethod
    def yolo(cls) -> "BashPolicy":
        """Create a policy that allows everything."""
        return cls()

    def describe(self) -> str:
        """Return a human-readable summary of the policy rules."""
        if self._allow is None and self._deny is None:
            return ""
        if self._allow is not None:
            return (
                f"ALLOWLIST MODE: Only these commands are permitted (all others blocked): "
                f"{', '.join(sorted(self._allow))}"
            )
        return (
            f"DENYLIST MODE: All commands are allowed except: "
            f"{', '.join(sorted(self._deny))}"
        )

    def is_allowed(self, command: str) -> bool:
        """Check if a command string is allowed by this policy.

        Parses pipes, chains, and subshells to check every command.
        """
        if self._allow is None and self._deny is None:
            return True
        commands = self._extract_commands(command)
        return all(self._check_single(cmd) for cmd in commands)

    def _check_single(self, cmd: str) -> bool:
        """Check a single command name against policy.

        Allowlist mode: command must be in allow set.
        Denylist mode: command must not be in deny set.
        """
        if self._allow is not None:
            return cmd in self._allow
        if self._deny is not None:
            return cmd not in self._deny
        return True

    @staticmethod
    def _extract_commands(command: str) -> list[str]:
        """Extract all command names from a potentially chained command string.

        Handles: |, &&, ||, ;, newlines, $(), backticks, env-var prefixes.
        Returns the first actual command word of each sub-command.
        """
        flat = command
        # Expand $(...) subshells into the command chain
        flat = re.sub(r'\$\([^)]*\)', lambda m: '; ' + m.group()[2:-1] + ' ;', flat)
        # Expand backtick subshells
        flat = re.sub(r'`[^`]*`', lambda m: '; ' + m.group()[1:-1] + ' ;', flat)
        # Split on pipe/chain operators AND newlines
        parts = re.split(r'\|{1,2}|&&|;|\n', flat)
        commands = []
        for part in parts:
            tokens = part.strip().split()
            # Skip env-var assignments (FOO=bar cmd ...) to find the real command
            while tokens and re.fullmatch(r'[A-Za-z_]\w*=\S*', tokens[0]):
                tokens = tokens[1:]
            if tokens:
                commands.append(tokens[0])
        return commands


class BashManager:
    """Manages shell command execution for an agent."""

    def __init__(
        self,
        policy: BashPolicy,
        working_dir: str,
        max_output: int = 50_000,
    ):
        self._policy = policy
        self._working_dir = working_dir
        self._max_output = max_output
        self._jobs_dir: Path | None = None

    def _ensure_jobs_dir(self) -> Path:
        """Create and return the jobs directory (lazy init)."""
        if self._jobs_dir is None:
            self._jobs_dir = Path(self._working_dir) / "system" / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        return self._jobs_dir

    def _validate_working_dir(self, cwd: str) -> dict | None:
        """Validate cwd is under the agent sandbox. Returns error dict or None."""
        try:
            resolved = str(Path(cwd).resolve())
            sandbox = str(Path(self._working_dir).resolve())
            if not (resolved == sandbox or resolved.startswith(sandbox + "/")):
                return {
                    "status": "error",
                    "message": (
                        f"working_dir must be under agent working directory: "
                        f"{self._working_dir}. To operate on an external path, "
                        f"use an allowed working_dir and put `cd {resolved} && ...` "
                        f"inside the command."
                    ),
                }
        except (ValueError, OSError):
            return {"status": "error", "message": "Invalid working_dir path"}
        return None

    def _validate_command(self, command: str) -> dict | None:
        """Validate command is non-empty and allowed by policy. Returns error dict or None."""
        if not command.strip():
            return {"status": "error", "message": "command is required"}
        if not self._policy.is_allowed(command):
            denied = BashPolicy._extract_commands(command)
            return {
                "status": "error",
                "message": f"Command not allowed by policy. "
                f"Denied command(s): {', '.join(denied)}",
            }
        return None

    @staticmethod
    def _validate_job_id(job_id: str) -> dict | None:
        """Validate job_id is safe (no path traversal). Returns error dict or None."""
        if not job_id:
            return {"status": "error", "message": "job_id is required"}
        # Reject path traversal attempts
        if "/" in job_id or "\\" in job_id or ".." in job_id:
            return {"status": "error", "message": f"Invalid job_id: {job_id}"}
        return None

    def handle(self, args: dict) -> dict:
        action = args.get("action", "run")

        if action == "poll":
            return self._handle_poll(args)
        if action == "cancel":
            return self._handle_cancel(args)
        # action == "run"
        return self._handle_run(args)

    def _handle_run(self, args: dict) -> dict:
        command = args.get("command", "")
        err = self._validate_command(command)
        if err:
            return err

        cwd = args.get("working_dir", self._working_dir)
        err = self._validate_working_dir(cwd)
        if err:
            return err

        is_async = args.get("async", False)
        if is_async:
            return self._run_async(command, cwd)
        return self._run_sync(command, cwd, args.get("timeout", 30))

    def _run_sync(self, command: str, cwd: str, timeout: float) -> dict:
        """Synchronous execution — original behavior, unchanged."""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            stdout = result.stdout
            stderr = result.stderr
            if len(stdout) > self._max_output:
                stdout = stdout[: self._max_output] + f"\n... (truncated, {len(result.stdout)} chars total)"
            if len(stderr) > self._max_output:
                stderr = stderr[: self._max_output] + f"\n... (truncated, {len(result.stderr)} chars total)"

            return {
                "status": "ok",
                "exit_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"status": "error", "message": f"Command failed: {e}"}

    def _run_async(self, command: str, cwd: str) -> dict:
        """Start command in background, return job_id immediately."""
        jobs_dir = self._ensure_jobs_dir()
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        job_dir = jobs_dir / job_id
        job_dir.mkdir()

        (job_dir / "command").write_text(command)
        (job_dir / "status").write_text("running")

        stdout_f = open(job_dir / "stdout.log", "w", encoding="utf-8")
        stderr_f = open(job_dir / "stderr.log", "w", encoding="utf-8")

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=cwd,
                start_new_session=True,
            )
        except Exception as e:
            stdout_f.close()
            stderr_f.close()
            # Clean up on launch failure
            import shutil
            shutil.rmtree(job_dir, ignore_errors=True)
            return {"status": "error", "message": f"Failed to start async job: {e}"}

        (job_dir / "pid").write_text(str(proc.pid))
        # Store Popen + file handles in-process so we can reap and close them.
        if not hasattr(self, "_open_handles"):
            self._open_handles: dict[str, tuple] = {}
        self._open_handles[job_id] = (proc, stdout_f, stderr_f)

        # Start background watcher — writes .notification/bash.json when
        # the process exits, so the agent gets notified via the standard
        # notification sync mechanism (same channel as email/soul/molt).
        watcher = threading.Thread(
            target=self._watch_async_job,
            args=(job_id, command, proc, job_dir, stdout_f, stderr_f),
            daemon=True,
        )
        watcher.start()

        return {
            "status": "ok",
            "job_id": job_id,
            "pid": proc.pid,
            "message": f'Job started. Use bash(action="poll", job_id="{job_id}") to check.',
        }

    def _watch_async_job(
        self, job_id: str, command: str, proc: subprocess.Popen,
        job_dir: Path, stdout_f, stderr_f,
    ) -> None:
        """Background thread: wait for async job, then write notification."""
        try:
            returncode = proc.wait()
        except Exception:
            returncode = -1

        # Close file handles
        try:
            stdout_f.close()
            stderr_f.close()
        except Exception:
            pass

        # Read stdout preview
        stdout_preview = ""
        try:
            stdout_text = (job_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
            stdout_preview = stdout_text[:200]
        except Exception:
            pass

        # Write notification to .notification/bash.json
        try:
            from datetime import datetime, timezone
            notif_dir = Path(self._working_dir) / ".notification"
            notif_dir.mkdir(exist_ok=True)
            payload = {
                "header": f"Job {job_id} completed (exit {returncode})",
                "icon": "⚡",
                "priority": "normal",
                "published_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "data": {
                    "job_id": job_id,
                    "command": command[:200],
                    "exit_code": returncode,
                    "stdout_preview": stdout_preview,
                },
            }
            target = notif_dir / "bash.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.rename(target)
        except Exception:
            pass  # Notification failure should not break the job

    def _handle_poll(self, args: dict) -> dict:
        """Check status of an async job."""
        job_id = args.get("job_id", "")
        err = self._validate_job_id(job_id)
        if err:
            return err

        jobs_dir = self._ensure_jobs_dir()
        job_dir = jobs_dir / job_id
        if not job_dir.is_dir():
            return {"status": "error", "message": f"Job not found: {job_id}"}

        status = (job_dir / "status").read_text(encoding="utf-8").strip()
        if status != "running":
            return {"status": "error", "message": f"Job already finished ({status})"}

        pid = int((job_dir / "pid").read_text(encoding="utf-8").strip())

        # Use Popen.poll() if we have the handle (same process), else os.waitpid
        handles = getattr(self, "_open_handles", {})
        entry = handles.get(job_id)
        if entry:
            proc = entry[0]
            returncode = proc.poll()
        else:
            # Fallback: try waitpid (different manager instance, same PID file)
            try:
                wpid, wait_status = os.waitpid(pid, os.WNOHANG)
                returncode = os.waitstatus_to_exitcode(wait_status) if wpid != 0 else None
            except ChildProcessError:
                # Not our child — check if alive via signal 0
                try:
                    os.kill(pid, 0)
                    returncode = None  # still alive
                except OSError:
                    returncode = -1  # dead but we can't get the code

        if returncode is None:
            return {"status": "running", "job_id": job_id, "pid": pid}

        # Process finished — close file handles, read output
        self._close_handles(job_id)

        stdout = (job_dir / "stdout.log").read_text(encoding="utf-8", errors="replace")
        stderr = (job_dir / "stderr.log").read_text(encoding="utf-8", errors="replace")

        if len(stdout) > self._max_output:
            stdout = stdout[: self._max_output] + f"\n... (truncated, {len(stdout)} chars total)"
        if len(stderr) > self._max_output:
            stderr = stderr[: self._max_output] + f"\n... (truncated, {len(stderr)} chars total)"

        # Clean up
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)

        return {
            "status": "done",
            "exit_code": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    def _handle_cancel(self, args: dict) -> dict:
        """Kill an async job."""
        job_id = args.get("job_id", "")
        err = self._validate_job_id(job_id)
        if err:
            return err

        jobs_dir = self._ensure_jobs_dir()
        job_dir = jobs_dir / job_id
        if not job_dir.is_dir():
            return {"status": "error", "message": f"Job not found: {job_id}"}

        status = (job_dir / "status").read_text(encoding="utf-8").strip()
        if status != "running":
            return {"status": "error", "message": f"Job already finished ({status})"}

        pid = int((job_dir / "pid").read_text(encoding="utf-8").strip())

        # Kill the entire process group (start_new_session=True makes pid the pgid)
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # Already dead

        # Reap via Popen if we have the handle, to avoid zombies
        handles = getattr(self, "_open_handles", {})
        entry = handles.get(job_id)
        if entry:
            proc = entry[0]
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        self._close_handles(job_id)

        # Clean up
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)

        return {"status": "cancelled", "job_id": job_id}

    def _close_handles(self, job_id: str) -> None:
        """Close open file handles for a job if we hold them."""
        handles = getattr(self, "_open_handles", {})
        entry = handles.pop(job_id, None)
        if entry:
            # entry is (Popen, stdout_file, stderr_file)
            for fh in entry[1:]:
                try:
                    fh.close()
                except Exception:
                    pass


def setup(
    agent: "BaseAgent",
    policy_file: str | None = None,
    yolo: bool = False,
) -> BashManager:
    """Set up the bash capability on an agent.

    Args:
        agent: The agent to extend.
        policy_file: Path to JSON policy file (required unless yolo=True).
        yolo: If True, allow all commands (no policy file needed).

    Returns:
        The BashManager instance for programmatic access.
    """
    # Resolve policy: explicit arg or default
    resolved_policy_file = policy_file

    if yolo:
        policy = BashPolicy.yolo()
    elif resolved_policy_file is not None:
        policy = BashPolicy.from_file(resolved_policy_file)
    else:
        policy = BashPolicy.from_file(str(_DEFAULT_POLICY_FILE))

    lang = agent._config.language

    mgr = BashManager(
        policy=policy,
        working_dir=str(agent._working_dir),
    )
    # Build description with policy rules
    desc = get_description(lang)
    policy_summary = policy.describe()
    if policy_summary:
        desc = f"{desc}\n\n{policy_summary}"

    agent.add_tool("bash", schema=get_schema(lang), handler=mgr.handle, description=desc)
    return mgr
