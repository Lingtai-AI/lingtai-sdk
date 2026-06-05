"""WorkingDir — agent working directory: lock, git, manifest."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


class WorkingDir:
    """Manages an agent's working directory — locking, git, manifest."""

    def __init__(self, working_dir: Path | str) -> None:
        self._path = Path(working_dir)
        self._path.mkdir(parents=True, exist_ok=True)
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
        lock_path = self._path / _LOCK_FILE
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
            lock_path = self._path / _LOCK_FILE
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
            gitignore.write_text("")

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

    # --- Filesystem Time Machine (list / diff / preview / rollback) ---
    #
    # These build on snapshot() — every snapshot is an ordinary git commit, so
    # rolling the working tree back to a prior state is `git reset --hard` after
    # a few safety checks. The guarantees we make:
    #   * never touch `.git` itself (rollback restores tracked files only),
    #   * never discard uncommitted work silently — a dirty tree is refused
    #     unless force=True,
    #   * always record where we came from. Before resetting we tag the prior
    #     HEAD with a `safety/rollback-<ts>` ref so an unwanted rollback is
    #     itself reversible (`git reset --hard <safety_ref>`).
    #
    # Limitations: untracked files matched by .gitignore are not snapshotted and
    # therefore not restored; rollback moves the branch pointer (it is a reset,
    # not a revert), so history after the target ref is only reachable via the
    # safety ref or reflog until gc expires it.

    _DIFF_PATCH_LIMIT = 20000  # chars — keep tool output bounded

    def _resolve_ref(self, ref: str) -> str | None:
        """Return the full commit hash for ``ref`` or None if it does not resolve."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                cwd=self._path, capture_output=True, text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        out = result.stdout.strip()
        return out or None

    def snapshot_list(self, limit: int = 20) -> list[dict]:
        """Return recent commits, newest first: ``[{hash, date, subject}, ...]``.

        Empty list if git is unavailable or the repo has no commits.
        """
        try:
            result = subprocess.run(
                [
                    "git", "log", f"-n{int(limit)}",
                    "--format=%h%x1f%cI%x1f%s",
                ],
                cwd=self._path, capture_output=True, text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            return []
        if result.returncode != 0:
            return []
        snaps: list[dict] = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) != 3:
                continue
            h, date, subject = parts
            snaps.append({"hash": h, "date": date, "subject": subject})
        return snaps

    def snapshot_diff(self, ref: str) -> dict:
        """Diff ``ref`` against the current working tree.

        Returns ``{ref, stat, patch}`` or ``{error: True, message}`` if the ref
        does not resolve. The patch is truncated to keep output bounded.
        """
        resolved = self._resolve_ref(ref)
        if resolved is None:
            return {"error": True, "message": f"ref does not resolve: {ref}"}
        try:
            stat = subprocess.run(
                ["git", "diff", "--stat", resolved],
                cwd=self._path, capture_output=True, text=True,
            ).stdout
            patch = subprocess.run(
                ["git", "diff", resolved],
                cwd=self._path, capture_output=True, text=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return {"error": True, "message": "git diff failed"}
        truncated = False
        if len(patch) > self._DIFF_PATCH_LIMIT:
            patch = patch[: self._DIFF_PATCH_LIMIT]
            truncated = True
        return {
            "error": False,
            "ref": ref,
            "ref_resolved": resolved,
            "stat": stat.strip(),
            "patch": patch,
            "truncated": truncated,
        }

    def _is_dirty(self) -> tuple[bool, bool]:
        """Return ``(dirty, has_untracked)`` for the current working tree."""
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self._path, capture_output=True, text=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False, False
        dirty = False
        untracked = False
        for line in status.splitlines():
            if not line:
                continue
            if line.startswith("??"):
                untracked = True
            dirty = True
        return dirty, untracked

    def rollback_preview(self, ref: str) -> dict:
        """Describe what rolling the working tree back to ``ref`` would do.

        Returns a structured status dict — never mutates anything. Keys:
        ``error``, ``ref``, ``ref_resolved``, ``current_head``, ``dirty``,
        ``untracked``, ``changes`` (``git diff --name-status ref..HEAD``),
        ``warning``.
        """
        resolved = self._resolve_ref(ref)
        if resolved is None:
            return {"error": True, "message": f"ref does not resolve: {ref}"}
        head = self._resolve_ref("HEAD") or ""
        dirty, untracked = self._is_dirty()
        try:
            name_status = subprocess.run(
                ["git", "diff", "--name-status", f"{resolved}..HEAD"],
                cwd=self._path, capture_output=True, text=True,
            ).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            name_status = ""
        changes = [ln for ln in name_status.splitlines() if ln.strip()]
        warning = (
            "Applying this rollback runs `git reset --hard` and discards commits "
            "made after the target. It requires force=True when the working tree "
            "is dirty. The prior HEAD is tagged as a safety ref before reset."
        )
        return {
            "error": False,
            "ref": ref,
            "ref_resolved": resolved,
            "current_head": head,
            "dirty": dirty,
            "untracked": untracked,
            "changes": changes,
            "warning": warning,
        }

    def rollback_apply(self, ref: str, force: bool = False) -> dict:
        """Restore the working tree to a prior commit via ``git reset --hard``.

        Safety contract:
          * refuse if ``ref`` does not resolve (``reason="invalid_ref"``),
          * refuse a dirty/untracked tree unless ``force=True``
            (``reason="dirty"``),
          * before resetting, tag the current HEAD as ``safety/rollback-<ts>``
            so the operation is reversible, and return that ref,
          * never delete ``.git``.

        Returns ``{status: "ok", restored_to, previous_head, safety_ref}`` on
        success or ``{status: "refused", reason, message}`` otherwise.
        """
        resolved = self._resolve_ref(ref)
        if resolved is None:
            return {
                "status": "refused",
                "reason": "invalid_ref",
                "message": f"ref does not resolve: {ref}",
            }

        dirty, untracked = self._is_dirty()
        if dirty and not force:
            return {
                "status": "refused",
                "reason": "dirty",
                "message": (
                    "Working tree has uncommitted or untracked changes. "
                    "Snapshot first, or pass force=True to discard them."
                ),
                "untracked": untracked,
            }

        previous_head = self._resolve_ref("HEAD") or ""

        # Record where we came from with a safety ref so the rollback is itself
        # reversible. Use a tag under refs/tags/safety/ keyed by UTC timestamp.
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_ref = f"safety/rollback-{ts}"
        try:
            if previous_head:
                subprocess.run(
                    ["git", "tag", "-f", safety_ref, previous_head],
                    cwd=self._path, capture_output=True, check=True,
                )
            subprocess.run(
                ["git", "reset", "--hard", resolved],
                cwd=self._path, capture_output=True, text=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            return {
                "status": "refused",
                "reason": "git_error",
                "message": f"rollback failed: {exc}",
            }

        return {
            "status": "ok",
            "restored_to": resolved,
            "previous_head": previous_head,
            "safety_ref": safety_ref,
            "forced": bool(force),
        }

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
        path = self._path / _MANIFEST_FILE
        if not path.is_file():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("covenant", "")
        except (json.JSONDecodeError, OSError):
            corrupt = self._path / ".agent.json.corrupt"
            try:
                path.rename(corrupt)
            except OSError:
                pass
            return ""

    def read_full_manifest(self) -> dict:
        """Read entire .agent.json as dict. Returns empty dict if missing or corrupt."""
        path = self._path / _MANIFEST_FILE
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def write_manifest(self, manifest: dict) -> None:
        target = self._path / _MANIFEST_FILE
        tmp = self._path / ".agent.json.tmp"
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        os.replace(str(tmp), str(target))
