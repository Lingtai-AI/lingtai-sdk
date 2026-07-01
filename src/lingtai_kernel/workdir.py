"""WorkingDir — agent working directory: lock, git, manifest."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt as _msvcrt

    def _lock_fd(fd):
        _msvcrt.locking(fd.fileno(), _msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd):
        _msvcrt.locking(fd.fileno(), _msvcrt.LK_UNLCK, 1)
else:
    import fcntl as _fcntl

    def _lock_fd(fd):
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    def _unlock_fd(fd):
        _fcntl.flock(fd, _fcntl.LOCK_UN)


_LOCK_FILE = ".agent.lock"
_MANIFEST_FILE = ".agent.json"
_MANIFEST_CORRUPT_FILE = ".agent.json.corrupt"
_HEARTBEAT_FILE = ".agent.heartbeat"
_STATUS_FILE = ".status.json"
_INIT_FILE = "init.json"
_SYSTEM_DIR = "system"
_LOGS_DIR = "logs"
_HISTORY_DIR = "history"
_NOTIFICATION_DIR = ".notification"

_RESOLVED_MANIFEST_FILE = "manifest.resolved.json"
_RESOLVED_MANIFEST_SCHEMA = "lingtai.manifest.resolved/v1"


@dataclass(frozen=True)
class WorkdirLayout:
    """Names the kernel-owned paths inside an agent working directory.

    Deliberately dumb: it only *names* paths from a root. No validation, no
    file creation, no policy — those stay in the owning modules
    (``handshake.is_alive``, ``notifications.validate_allowed_channel``, …).
    The point is a single discoverable surface for the agent workdir
    filesystem protocol (``.agent.json``, ``.agent.heartbeat``,
    ``.notification/<channel>.json``, ``tmp/tool-results/``, …) so the same
    relative names are not retyped across runtime modules and tests.

    Internal by convention: import from ``lingtai_kernel.workdir``; not
    re-exported from ``lingtai_kernel.__init__``.
    """

    root: Path

    @property
    def agent_lock(self) -> Path:
        return self.root / _LOCK_FILE

    @property
    def agent_manifest(self) -> Path:
        return self.root / _MANIFEST_FILE

    @property
    def agent_manifest_corrupt(self) -> Path:
        return self.root / _MANIFEST_CORRUPT_FILE

    @property
    def heartbeat(self) -> Path:
        return self.root / _HEARTBEAT_FILE

    @property
    def status_json(self) -> Path:
        return self.root / _STATUS_FILE

    @property
    def init_json(self) -> Path:
        return self.root / _INIT_FILE

    @property
    def system_dir(self) -> Path:
        return self.root / _SYSTEM_DIR

    @property
    def logs_dir(self) -> Path:
        return self.root / _LOGS_DIR

    @property
    def history_dir(self) -> Path:
        return self.root / _HISTORY_DIR

    @property
    def chat_history(self) -> Path:
        return self.history_dir / "chat_history.jsonl"

    @property
    def notification_dir(self) -> Path:
        return self.root / _NOTIFICATION_DIR

    @property
    def tool_results_dir(self) -> Path:
        return self.root / "tmp" / "tool-results"

    @property
    def resolved_manifest(self) -> Path:
        return self.system_dir / _RESOLVED_MANIFEST_FILE

    @property
    def resolved_manifest_tmp(self) -> Path:
        return self.system_dir / (_RESOLVED_MANIFEST_FILE + ".tmp")

    def notification_file(self, channel: str) -> Path:
        """Path to a ``.notification/<channel>.json`` file (no validation)."""
        return self.notification_dir / f"{channel}.json"

    def system_file(self, name: str) -> Path:
        """Path to a file inside the ``system/`` directory."""
        return self.system_dir / name


def workdir_layout(path: Path | str) -> WorkdirLayout:
    """Return the :class:`WorkdirLayout` rooted at *path*.

    A ``str`` is coerced to ``Path`` (so ``handshake.is_agent(str(dir))`` keeps
    working); anything else — a real ``Path`` or a duck-typed stand-in such as a
    test ``MagicMock`` — is stored as-is so the ``root / name`` joins stay on
    whatever object the caller passed. This mirrors the pre-helper behavior
    where producers used ``workdir / "…"`` directly without coercion.
    """
    root = Path(path) if isinstance(path, str) else path
    return WorkdirLayout(root)

# Key names that carry (or point at) secret material — dropped recursively
# before the resolved manifest is published. `*_env` names are included to
# stay consistent with the `.agent.json` `_SENSITIVE_KEYS` hygiene. The token
# alternative is anchored on `_`/edges so plural "tokens" fields
# (e.g. `max_tokens`) survive.
_SECRET_KEY_RE = re.compile(
    r"(^|_)(api_?key|secret|secrets|password|passwd|credential|credentials"
    r"|private_key|access_key|token)(_|$)",
    re.IGNORECASE,
)


def _is_secret_key(key: Any) -> bool:
    """Return whether a mapping key likely names secret material.

    Handles snake/kebab case through ``_SECRET_KEY_RE`` and common compact or
    camelCase spellings such as ``apiKey``, ``appSecret``, and ``botToken``
    without treating ordinary words like ``secretary`` or ``max_tokens`` as
    secrets.
    """
    raw = str(key)
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    if _SECRET_KEY_RE.search(normalized):
        return True
    compact = re.sub(r"[^a-z0-9]+", "", raw.lower())
    if compact in {"apikey", "password", "passwd", "credential", "credentials", "privatekey", "accesskey"}:
        return True
    return compact.endswith("secret") or compact.endswith("token")


def _redact_secrets(value: Any) -> Any:
    """Return a deep copy of *value* with secret-bearing keys removed.

    Recurses through dicts and lists; any dict key matching
    ``_SECRET_KEY_RE`` is dropped entirely (value and all). Non-container
    leaves are returned as-is.
    """
    if isinstance(value, dict):
        return {
            k: _redact_secrets(v)
            for k, v in value.items()
            if not _is_secret_key(k)
        }
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


def write_resolved_manifest(working_dir: Path | str, data: dict) -> Path | None:
    """Publish the kernel-resolved manifest as a derived runtime artifact.

    Writes ``<working_dir>/system/manifest.resolved.json`` from fully-resolved
    init data (after preset materialization, validation, and path resolution).
    init.json stays user-owned input; this artifact is regenerated on every
    boot/refresh, safe to delete, and is what TUI/portal consumers should read
    instead of re-implementing the preset merge over the raw snapshot
    (issue #259).

    Secrets (api_key/password/token-like fields) are removed recursively.
    The write is atomic (``.tmp`` → ``os.replace``) and best-effort: returns
    the artifact path on success, None when *data* has no manifest or the
    write failed.
    """
    manifest = data.get("manifest") if isinstance(data, dict) else None
    if not isinstance(manifest, dict):
        return None

    artifact: dict[str, Any] = {
        "schema": _RESOLVED_MANIFEST_SCHEMA,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "kernel",
        "manifest": _redact_secrets(manifest),
    }
    preset = manifest.get("preset")
    if isinstance(preset, dict):
        artifact["preset"] = _redact_secrets(preset)

    try:
        layout = workdir_layout(working_dir)
        layout.system_dir.mkdir(parents=True, exist_ok=True)
        target = layout.resolved_manifest
        tmp = layout.resolved_manifest_tmp
        tmp.write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp), str(target))
        return target
    except (OSError, TypeError, ValueError):
        return None


class WorkingDir:
    """Manages an agent's working directory — locking, git, manifest."""

    def __init__(self, working_dir: Path | str) -> None:
        self._path = Path(working_dir)
        self._path.mkdir(parents=True, exist_ok=True)
        self._layout = workdir_layout(self._path)
        self._lock_file: Any = None

    @property
    def path(self) -> Path:
        return self._path

    # --- Lock lifecycle ---

    def acquire_lock(self, timeout: float = 0) -> None:
        """Acquire an exclusive file lock on the working directory.

        Args:
            timeout: Max seconds to wait for the lock. 0 = fail immediately
                (default, backward compatible). Polls at 250ms intervals.
        """
        lock_path = self._layout.agent_lock
        deadline = time.monotonic() + timeout
        while True:
            self._lock_file = open(lock_path, "w")
            try:
                _lock_fd(self._lock_file)
                return  # success
            except OSError:
                self._lock_file.close()
                self._lock_file = None
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Working directory '{self._path}' is already in use "
                        f"by another agent. Each agent needs its own directory."
                    )
                time.sleep(0.25)

    def release_lock(self) -> None:
        if self._lock_file is not None:
            lock_path = self._layout.agent_lock
            try:
                _unlock_fd(self._lock_file)
                self._lock_file.close()
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._lock_file = None

    # --- Git operations ---

    def init_git(self) -> None:
        git_dir = self._path / ".git"
        if git_dir.is_dir():
            return

        try:
            subprocess.run(
                ["git", "init"], cwd=self._path,
                capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "agent@lingtai"],
                cwd=self._path, capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "灵台 Agent"],
                cwd=self._path, capture_output=True, check=True,
            )

            gitignore = self._path / ".gitignore"
            # Exclude secrets and transient lifecycle files from git tracking.
            # Without this, snapshot()'s `git add -A` would commit .secrets/
            # (MCP addon credentials such as bot tokens) to git history.
            gitignore.write_text(
                "# Secrets — MCP addon credentials (bot tokens, API keys)\n"
                ".secrets/\n"
                "\n"
                "# Transient lifecycle signal files\n"
                ".sleep\n"
                ".suspend\n"
                ".agent.heartbeat\n"
                ".timemachine.pid\n"
            )

            system_dir = self._path / "system"
            system_dir.mkdir(exist_ok=True)
            covenant_file = system_dir / "covenant.md"
            if not covenant_file.is_file():
                covenant_file.write_text("")
            principle_file = system_dir / "principle.md"
            if not principle_file.is_file():
                principle_file.write_text("")
            pad_file = system_dir / "pad.md"
            if not pad_file.is_file():
                pad_file.write_text("")

            subprocess.run(
                ["git", "add", ".gitignore", "system/"],
                cwd=self._path, capture_output=True, check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "init: agent working directory"],
                cwd=self._path, capture_output=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            system_dir = self._path / "system"
            system_dir.mkdir(exist_ok=True)
            covenant_file = system_dir / "covenant.md"
            if not covenant_file.is_file():
                covenant_file.write_text("")
            principle_file = system_dir / "principle.md"
            if not principle_file.is_file():
                principle_file.write_text("")
            pad_file = system_dir / "pad.md"
            if not pad_file.is_file():
                pad_file.write_text("")

    def diff(self, rel_path: str) -> str:
        try:
            result = subprocess.run(
                ["git", "diff", rel_path],
                cwd=self._path, capture_output=True, text=True,
            )
            diff_text = result.stdout.strip()
            if not diff_text:
                status_result = subprocess.run(
                    ["git", "status", "--porcelain", rel_path],
                    cwd=self._path, capture_output=True, text=True,
                )
                if status_result.stdout.strip():
                    file_path = self._path / rel_path
                    diff_text = f"(new/untracked file)\n{file_path.read_text(encoding='utf-8')}"
        except (FileNotFoundError, subprocess.CalledProcessError):
            diff_text = ""
        return diff_text

    def diff_and_commit(self, rel_path: str, label: str) -> tuple[str | None, str | None]:
        try:
            diff_result = subprocess.run(
                ["git", "diff", rel_path],
                cwd=self._path, capture_output=True, text=True,
            )
            diff_cached = subprocess.run(
                ["git", "diff", "--cached", rel_path],
                cwd=self._path, capture_output=True, text=True,
            )
            status_result = subprocess.run(
                ["git", "status", "--porcelain", rel_path],
                cwd=self._path, capture_output=True, text=True,
            )

            has_changes = bool(
                diff_result.stdout.strip()
                or diff_cached.stdout.strip()
                or status_result.stdout.strip()
            )

            if not has_changes:
                return None, None

            diff_text = diff_result.stdout or status_result.stdout

            subprocess.run(
                ["git", "add", rel_path],
                cwd=self._path, capture_output=True, check=True,
            )

            if not diff_text.strip():
                staged = subprocess.run(
                    ["git", "diff", "--cached", rel_path],
                    cwd=self._path, capture_output=True, text=True,
                )
                diff_text = staged.stdout

            subprocess.run(
                ["git", "commit", "-m", f"system: update {label}"],
                cwd=self._path, capture_output=True, check=True,
            )

            hash_result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self._path, capture_output=True, text=True,
            )
            commit_hash = hash_result.stdout.strip()

            return diff_text, commit_hash

        except (FileNotFoundError, subprocess.CalledProcessError):
            return None, None

    def snapshot(self) -> str | None:
        """Commit entire working directory state. Returns commit hash or None.

        No-op if nothing changed. Like Apple Time Machine — captures everything.
        """
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self._path, capture_output=True, check=True,
            )
            status = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=self._path, capture_output=True,
            )
            if status.returncode == 0:
                return None  # nothing staged

            from datetime import datetime, timezone
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            subprocess.run(
                ["git", "commit", "-m", f"snapshot {ts}"],
                cwd=self._path, capture_output=True, check=True,
            )
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=self._path, capture_output=True, text=True,
            )
            return result.stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    def gc(self) -> None:
        """Run git garbage collection to optimize repo storage."""
        try:
            subprocess.run(
                ["git", "gc", "--auto"],
                cwd=self._path, capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    # --- Manifest ---

    def read_manifest(self) -> str:
        """Read the covenant from the manifest file. Returns empty string if missing."""
        path = self._layout.agent_manifest
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("covenant", "")
        except (json.JSONDecodeError, OSError):
            corrupt = self._layout.agent_manifest_corrupt
            try:
                path.rename(corrupt)
            except OSError:
                pass
            return ""

    def read_full_manifest(self) -> dict:
        """Read entire .agent.json as dict. Returns empty dict if missing or corrupt."""
        path = self._layout.agent_manifest
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def write_manifest(self, manifest: dict) -> None:
        # Atomic temp-file + os.replace, UTF-8 preserved, no trailing newline.
        # Routed through the shared helper (issue #510); on-disk format is
        # byte-identical to the previous inline implementation.
        from ._fsutil import atomic_write_json

        atomic_write_json(self._layout.agent_manifest, manifest)
