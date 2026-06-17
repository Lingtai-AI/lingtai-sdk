"""FileIOService — abstract file access backing read/edit/write/glob/grep intrinsics.

First implementation: LocalFileIOService facade over LocalFileIOBackend (local filesystem, text files only).
Future: RichFileIOService, RemoteFileIOService, and SandboxedFileIOService can swap backends without changing tool schemas.

Recursive traversal budgets (issue #164)
---------------------------------------

``LocalFileIOBackend.glob`` and ``LocalFileIOBackend.grep`` enforce defaults
that keep a single call from wedging the agent for ~17 min on a broad root
like ``/Users/<name>/work``:

* default-prune directories (``DEFAULT_EXCLUDED_DIRS``): VCS metadata,
  language caches, build outputs, and per-agent ``.lingtai`` history/tmp.
* wall-clock budget (``DEFAULT_WALLTIME_S``) and visited-entry budget
  (``DEFAULT_MAX_VISITED``) — the traversal short-circuits when either is
  exceeded.
* per-file size cap (``DEFAULT_MAX_FILE_BYTES``) for ``grep`` to skip
  large binaries / logs without reading them in full.

When a budget is exceeded the call returns the partial results gathered so
far. The kernel-side ``grep``/``glob`` tool handlers surface a structured
``truncated_reason`` so the agent can see *why* it was cut short. Callers
that explicitly want unbounded behavior can pass ``walltime_s=None`` and
``max_visited=None``; the kernel tool wrappers do not expose those knobs.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class GrepMatch:
    """A single grep match result."""
    path: str
    line_number: int
    line: str


@dataclass
class TraversalStats:
    """Bookkeeping about a recursive traversal — surfaced via ``last_traversal``.

    ``truncated_reason`` is one of ``None`` (clean finish), ``"walltime"``
    (wall-clock budget exceeded), ``"visited"`` (visited-entry budget
    exceeded), or ``"max_results"`` (the caller's cap was reached).
    """
    visited: int = 0
    elapsed_ms: int = 0
    truncated_reason: str | None = None
    files_skipped_size: int = 0
    files_skipped_binary: int = 0
    dirs_pruned: int = 0


#: Directories that are skipped by default during recursive traversal.
#: Listed as path *components* — any directory whose name is in this set is
#: pruned from ``os.walk``. Callers can override with ``exclude_dirs=set()``.
DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset({
    # VCS metadata
    ".git", ".hg", ".svn",
    # Language ecosystems
    "node_modules",
    ".venv", "venv", "env",
    "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "target",  # rust/maven build dir
    # Build artefacts
    "dist", "build", ".cache",
    # LingTai per-agent runtime state — large, fast-growing, never useful
    # for a user-level search and the primary culprit in #164.
    "history", "tmp", "daemons",
    ".notification",
})

#: Default wall-clock budget (seconds) for a single traversal call.
DEFAULT_WALLTIME_S: float = 8.0

#: Default max number of filesystem entries (files + dirs) inspected.
DEFAULT_MAX_VISITED: int = 20_000

#: Default per-file size limit (bytes) read for grep. Files larger than this
#: are skipped (counted in ``TraversalStats.files_skipped_size``).
DEFAULT_MAX_FILE_BYTES: int = 4 * 1024 * 1024  # 4 MiB


class FileIOService(ABC):
    """Abstract file I/O service.

    Backs the read, edit, write, glob, and grep intrinsics.
    Implementations can provide local filesystem, remote, sandboxed, or
    format-aware (PDF, images) file access.
    """

    @abstractmethod
    def read(self, path: str) -> str:
        """Read file contents as text."""
        ...

    @abstractmethod
    def write(self, path: str, content: str) -> None:
        """Write content to a file (create or overwrite)."""
        ...

    @abstractmethod
    def edit(self, path: str, old_string: str, new_string: str) -> str:
        """Replace old_string with new_string in the file. Returns updated content."""
        ...

    @abstractmethod
    def glob(self, pattern: str, root: str | None = None) -> list[str]:
        """Find files matching a glob pattern."""
        ...

    @abstractmethod
    def grep(self, pattern: str, path: str | None = None, max_results: int = 50) -> list[GrepMatch]:
        """Search file contents by regex pattern."""
        ...


class FileIOBackend(ABC):
    """Backend protocol for concrete file operations.

    ``FileIOService`` is the stable tool-facing contract. Backends own the
    implementation details for read/write/edit/glob/grep: local Python today,
    optional rg/fd or Rust/native backends later. This split keeps tool schemas
    and safety semantics stable while allowing the execution engine underneath
    to change.
    """

    last_traversal: TraversalStats

    @abstractmethod
    def read(self, path: str) -> str:
        """Read file contents as text."""
        ...

    @abstractmethod
    def write(self, path: str, content: str) -> None:
        """Write content to a file (create or overwrite)."""
        ...

    @abstractmethod
    def edit(self, path: str, old_string: str, new_string: str) -> str:
        """Replace old_string with new_string in the file. Returns updated content."""
        ...

    @abstractmethod
    def glob(
        self,
        pattern: str,
        root: str | None = None,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_results: int | None = 2000,
    ) -> list[str]:
        """Find files matching a glob pattern."""
        ...

    @abstractmethod
    def grep(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
    ) -> list[GrepMatch]:
        """Search file contents by regex pattern."""
        ...


class LocalFileIOBackend(FileIOBackend):
    """Local Python backend — text files only.

    This is the default backend. It preserves the historical LocalFileIOService
    behavior while making the backend boundary explicit for future Rust/native
    implementations.
    """

    def __init__(self, root: Path | str | None = None):
        self._root = Path(root) if root else None
        #: Stats from the most recent recursive call (glob/grep). Mutated
        #: in place by ``_walk_files`` so the kernel-side tool handlers can
        #: surface ``truncated_reason`` to the LLM. Reset at the start of
        #: each traversal call.
        self.last_traversal: TraversalStats = TraversalStats()

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute() and self._root:
            p = self._root / p
        return p

    def read(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def edit(self, path: str, old_string: str, new_string: str) -> str:
        p = self._resolve(path)
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            raise ValueError(f"old_string not found in {path}")
        count = content.count(old_string)
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times in {path} — must be unique. "
                "Provide more context to make it unique."
            )
        content = content.replace(old_string, new_string, 1)
        p.write_text(content, encoding="utf-8")
        return content

    def _walk_files(
        self,
        root: Path,
        *,
        exclude_dirs: frozenset[str] | set[str] | None,
        walltime_s: float | None,
        max_visited: int | None,
    ) -> Iterable[Path]:
        """Yield files under ``root`` while pruning excluded dirs and
        enforcing wall-clock / visited budgets.

        Stats are written into ``self.last_traversal``. The generator
        stops cleanly when a budget is exceeded; callers see only the
        partial result.
        """
        import os

        excludes: frozenset[str] | set[str]
        if exclude_dirs is None:
            excludes = DEFAULT_EXCLUDED_DIRS
        else:
            excludes = exclude_dirs

        stats = self.last_traversal
        start = time.monotonic()

        # If the user pointed us straight at a file, yield it and stop —
        # no budget machinery applies.
        if root.is_file():
            stats.visited = 1
            yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            if walltime_s is not None and (time.monotonic() - start) > walltime_s:
                stats.truncated_reason = "walltime"
                break

            stats.visited += 1
            if max_visited is not None and stats.visited > max_visited:
                stats.truncated_reason = "visited"
                stats.elapsed_ms = int((time.monotonic() - start) * 1000)
                return

            # Prune excluded dirs in place so os.walk does not descend.
            if excludes:
                before = len(dirnames)
                dirnames[:] = [d for d in dirnames if d not in excludes]
                stats.dirs_pruned += before - len(dirnames)

            for filename in filenames:
                if walltime_s is not None and (time.monotonic() - start) > walltime_s:
                    stats.truncated_reason = "walltime"
                    stats.elapsed_ms = int((time.monotonic() - start) * 1000)
                    return
                stats.visited += 1
                if max_visited is not None and stats.visited > max_visited:
                    stats.truncated_reason = "visited"
                    stats.elapsed_ms = int((time.monotonic() - start) * 1000)
                    return
                yield Path(dirpath) / filename

        stats.elapsed_ms = int((time.monotonic() - start) * 1000)

    def glob(
        self,
        pattern: str,
        root: str | None = None,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_results: int | None = 2000,
    ) -> list[str]:
        """Find files matching ``pattern`` under ``root``.

        Budgets and exclusions are applied by default (see module
        docstring). When a budget trips, the returned list is the
        partial result; inspect ``self.last_traversal.truncated_reason``
        to find out which one fired.
        """
        import fnmatch
        import os

        self.last_traversal = TraversalStats()
        search_root = Path(root) if root else (self._root or Path("."))
        results: list[str] = []
        for path in self._walk_files(
            search_root,
            exclude_dirs=exclude_dirs,
            walltime_s=walltime_s,
            max_visited=max_visited,
        ):
            # Match against pattern relative to search root so the same
            # glob expression behaves identically regardless of cwd.
            try:
                rel = os.path.relpath(str(path), search_root)
            except ValueError:
                # cross-volume on Windows — fall back to absolute.
                rel = str(path)
            if fnmatch.fnmatch(rel, pattern):
                results.append(str(path))
                if max_results is not None and len(results) >= max_results:
                    self.last_traversal.truncated_reason = (
                        self.last_traversal.truncated_reason or "max_results"
                    )
                    break
        results.sort()
        return results

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
    ) -> list[GrepMatch]:
        """Search file contents by regex.

        Per-file: skipped (counted) when larger than ``max_file_bytes``
        or when ``read_text(utf-8)`` raises ``UnicodeDecodeError`` /
        ``PermissionError`` / ``OSError`` (binary, unreadable). Across
        files: bounded by ``walltime_s`` and ``max_visited``. See module
        docstring for default values.
        """
        import re

        regex = re.compile(pattern)
        self.last_traversal = TraversalStats()
        search_path = Path(path) if path else (self._root or Path("."))
        results: list[GrepMatch] = []

        for f in self._walk_files(
            search_path,
            exclude_dirs=exclude_dirs,
            walltime_s=walltime_s,
            max_visited=max_visited,
        ):
            if not f.is_file():
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if max_file_bytes is not None and size > max_file_bytes:
                self.last_traversal.files_skipped_size += 1
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError):
                self.last_traversal.files_skipped_binary += 1
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    results.append(GrepMatch(path=str(f), line_number=i, line=line))
                    if len(results) >= max_results:
                        self.last_traversal.truncated_reason = (
                            self.last_traversal.truncated_reason or "max_results"
                        )
                        return results
        return results

class LocalFileIOService(FileIOService):
    """Tool-facing file I/O service facade using a pluggable backend.

    Existing agents continue to instantiate ``LocalFileIOService(root=...)`` and
    see the same behavior. The implementation is delegated to
    ``LocalFileIOBackend`` by default, so future Rust/native backends can be
    introduced behind the same read/write/edit/glob/grep contract.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        backend: FileIOBackend | None = None,
    ):
        self._backend = backend or LocalFileIOBackend(root=root)

    @property
    def last_traversal(self) -> TraversalStats:
        return self._backend.last_traversal

    @last_traversal.setter
    def last_traversal(self, value: TraversalStats) -> None:
        self._backend.last_traversal = value

    def _resolve(self, path: str) -> Path:
        """Compatibility shim for callers that reached into the old local service."""
        resolver = getattr(self._backend, "_resolve", None)
        if resolver is None:
            raise AttributeError("configured file I/O backend does not expose _resolve")
        return resolver(path)

    def _walk_files(
        self,
        root: Path,
        *,
        exclude_dirs: frozenset[str] | set[str] | None,
        walltime_s: float | None,
        max_visited: int | None,
    ) -> Iterable[Path]:
        """Compatibility shim for callers that reached into the old local service."""
        walker = getattr(self._backend, "_walk_files", None)
        if walker is None:
            raise AttributeError("configured file I/O backend does not expose _walk_files")
        return walker(
            root,
            exclude_dirs=exclude_dirs,
            walltime_s=walltime_s,
            max_visited=max_visited,
        )

    def read(self, path: str) -> str:
        return self._backend.read(path)

    def write(self, path: str, content: str) -> None:
        self._backend.write(path, content)

    def edit(self, path: str, old_string: str, new_string: str) -> str:
        return self._backend.edit(path, old_string, new_string)

    def glob(
        self,
        pattern: str,
        root: str | None = None,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_results: int | None = 2000,
    ) -> list[str]:
        return self._backend.glob(
            pattern,
            root=root,
            exclude_dirs=exclude_dirs,
            walltime_s=walltime_s,
            max_visited=max_visited,
            max_results=max_results,
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        max_results: int = 50,
        *,
        exclude_dirs: frozenset[str] | set[str] | None = None,
        walltime_s: float | None = DEFAULT_WALLTIME_S,
        max_visited: int | None = DEFAULT_MAX_VISITED,
        max_file_bytes: int | None = DEFAULT_MAX_FILE_BYTES,
    ) -> list[GrepMatch]:
        return self._backend.grep(
            pattern,
            path=path,
            max_results=max_results,
            exclude_dirs=exclude_dirs,
            walltime_s=walltime_s,
            max_visited=max_visited,
            max_file_bytes=max_file_bytes,
        )
