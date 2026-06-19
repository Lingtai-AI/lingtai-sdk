"""Rust-backed ``FileIOBackend`` — grep/glob via subprocess sidecar.

This is the formal native backend for ``glob`` and ``grep`` on agents
that pip-installed a platform-specific wheel. The wheel ships the Rust
sidecar binary under ``lingtai/bin/`` and ``resolve_sidecar_binary``
discovers it automatically — no env var required.

Read/write/edit are delegated verbatim to ``LocalFileIOBackend`` so that
sandbox semantics (root resolution, parent-dir creation, etc.) stay in
one place. Only the recursive operations — ``grep`` and ``glob`` — are
routed through the sidecar.

Sidecar resolution order
------------------------

``resolve_sidecar_binary()`` consults each of these sources in order and
uses the first one that points at an executable file:

1. explicit ``binary_path=`` constructor argument.
2. ``LINGTAI_FILE_IO_SIDECAR`` env var — operator override (preferred).
3. ``LINGTAI_SEARCH_SIDECAR`` env var — legacy alias from the original
   PoC. Still honored so older deployments keep working.
4. **Packaged binary** at ``lingtai/bin/lingtai-search-sidecar[.exe]``
   — present in platform-specific wheels built by ``setup.py``.
5. **Dev-tree binary** at ``experimental/lingtai-search-sidecar/target/
   {release,debug}/lingtai-search-sidecar[.exe]`` — picked up
   automatically when running out of an editable / source checkout.

``SidecarAdapter()`` remains strict for direct/public callers: without an
explicit ``binary_path`` it only honors env vars. The default agent path
uses ``default_file_io_service()``, which calls ``resolve_sidecar_binary``
for packaged/dev-tree autodiscovery and soft Python fallback.

Failure mode
~~~~~~~~~~~~

``RustFileIOBackend`` itself does **not** silently fall back to Python.
If the sidecar cannot run or returns an error, a ``SidecarError``
bubbles up. The soft-fallback policy lives one layer up in
``default_file_io_service``, which reads ``LINGTAI_FILE_IO_BACKEND`` and
chooses between the Rust and Python backends accordingly.

Defaults (``DEFAULT_WALLTIME_S``, ``DEFAULT_MAX_VISITED``,
``DEFAULT_MAX_FILE_BYTES``, ``DEFAULT_EXCLUDED_DIRS``) are imported from
``file_io`` so that the two backends remain in lock-step. The sidecar is
short-lived: one subprocess per request — persistent / daemonized
variants can be layered on top later without changing the Python contract.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_io import (
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_VISITED,
    DEFAULT_WALLTIME_S,
    FileIOBackend,
    GrepMatch,
    LocalFileIOBackend,
    TraversalStats,
)

#: Environment variables that point at the sidecar binary. The first one is
#: the canonical name; the second one is kept for backwards compatibility
#: with the original PoC under ``experimental/lingtai-search-sidecar``.
SIDECAR_ENV_VARS: tuple[str, ...] = (
    "LINGTAI_FILE_IO_SIDECAR",
    "LINGTAI_SEARCH_SIDECAR",
)

#: File name of the sidecar binary as bundled inside the wheel.
_BINARY_NAME = "lingtai-search-sidecar.exe" if os.name == "nt" else "lingtai-search-sidecar"


def _is_executable_file(path: Path) -> bool:
    """Return True only for files the sidecar subprocess can execute."""
    if not path.is_file():
        return False
    if os.name == "nt":
        # Windows execution semantics are extension/PATHEXT based; os.X_OK is
        # not a reliable executable-bit signal there.
        return path.suffix.lower() in {".exe", ".bat", ".cmd", ".com"}
    return os.access(path, os.X_OK)


class SidecarError(RuntimeError):
    """Raised when the optional Rust sidecar cannot satisfy a request.

    Carries the operation, the structured error code emitted by the sidecar
    (if any), and the human-readable message. The exit code is included
    when the failure was a non-zero subprocess exit. All of these are
    surfaced to operators so they can tell *why* the explicit backend
    refused to silently fall back.
    """

    def __init__(
        self,
        message: str,
        *,
        op: str | None = None,
        code: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.op = op
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class SidecarRequest:
    """JSON payload sent to the sidecar.

    Mirrors ``Request`` on the Rust side. Sidecar callers pass an explicit
    list for ``exclude_dirs`` so the Python defaults stay the single source
    of truth.
    """

    op: str
    root: str
    path: str
    pattern: str
    max_results: int
    max_visited: int
    walltime_ms: int
    max_file_bytes: int
    exclude_dirs: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "root": self.root,
            "path": self.path,
            "pattern": self.pattern,
            "max_results": self.max_results,
            "max_visited": self.max_visited,
            "walltime_ms": self.walltime_ms,
            "max_file_bytes": self.max_file_bytes,
            "exclude_dirs": list(self.exclude_dirs),
        }


class SidecarAdapter:
    """Thin JSON-over-subprocess client for ``lingtai-search-sidecar``.

    The adapter is intentionally small: build a payload, run the binary,
    parse the response envelope, raise ``SidecarError`` on any failure.
    Higher-level routing (sandbox, dispatch by op) lives in
    ``RustFileIOBackend``.

    Two construction modes:

    * ``SidecarAdapter()`` / ``SidecarAdapter(binary_path=...)`` — strict.
      The binary is whatever the operator passed (or one of the legacy
      env vars). When neither is set the adapter reports ``not_configured``.
      Used by the public ``RustFileIOBackend`` API so opt-in callers see
      a loud failure rather than picking up a stale dev-tree binary.

    * ``SidecarAdapter.autodiscover()`` — auto-resolve via the full
      priority list (explicit > env > packaged > dev tree). Used by the
      ``default_file_io_service`` factory and by the wheel-installed
      default agent path.
    """

    def __init__(
        self,
        binary_path: str | None = None,
        *,
        timeout_s: float = 30.0,
    ) -> None:
        self.binary_path = binary_path or _binary_from_env()
        self.timeout_s = timeout_s

    @classmethod
    def autodiscover(cls, *, timeout_s: float = 30.0) -> "SidecarAdapter":
        """Return an adapter pointing at the best-effort resolved binary.

        Walks ``resolve_sidecar_binary`` (explicit unset, so the env vars,
        packaged binary, and dev-tree binary all participate). The
        resulting adapter may still report ``available() is False`` if
        nothing was found — that's the signal the operator's environment
        has no Rust backend at all, and the factory layer above will
        fall through to ``LocalFileIOBackend``.
        """
        adapter = cls(binary_path=None, timeout_s=timeout_s)
        # Bypass the env-var-only constructor default by stamping the
        # resolved path in directly.
        adapter.binary_path = resolve_sidecar_binary()
        return adapter

    def available(self) -> bool:
        return self._resolve_binary() is not None

    def call(self, request: SidecarRequest) -> dict[str, Any]:
        binary = self._resolve_binary()
        if not binary:
            envs = " or ".join(SIDECAR_ENV_VARS)
            raise SidecarError(
                f"Rust file I/O sidecar is not configured; pass binary_path= or set {envs}",
                op=request.op,
                code="not_configured",
            )
        try:
            completed = subprocess.run(
                [binary],
                input=json.dumps(request.to_payload()),
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SidecarError(
                f"sidecar binary not found at {binary!r}",
                op=request.op,
                code="binary_missing",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SidecarError(
                f"sidecar timed out after {self.timeout_s:g}s",
                op=request.op,
                code="timeout",
            ) from exc

        envelope = _parse_envelope(completed.stdout, completed.stderr, completed.returncode, request.op)
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            if not isinstance(error, dict):
                error = {"code": "unknown", "message": str(error)}
            raise SidecarError(
                str(error.get("message") or "sidecar returned an error"),
                op=str(envelope.get("op") or request.op),
                code=str(error.get("code") or "unknown"),
                exit_code=completed.returncode,
            )
        return envelope

    def _resolve_binary(self) -> str | None:
        if not self.binary_path:
            return None
        candidate = Path(self.binary_path).expanduser()
        if _is_executable_file(candidate):
            return str(candidate)
        return shutil.which(self.binary_path)


def _binary_from_env() -> str | None:
    for name in SIDECAR_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _packaged_binary() -> str | None:
    """Locate the Rust binary bundled inside the installed ``lingtai`` wheel.

    Wheels built by ``setup.py`` drop the binary at
    ``src/lingtai/bin/lingtai-search-sidecar[.exe]`` and declare it in
    ``package_data``. We resolve it via ``importlib.resources`` so the
    lookup works for both flat installs and zip-imported wheels.

    Returns ``None`` if no binary is bundled (pure-Python wheel, or sdist
    build that couldn't run cargo). Never raises.
    """
    try:
        from importlib.resources import files

        candidate = files("lingtai").joinpath("bin", _BINARY_NAME)
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        return None
    try:
        # ``files()`` may return a MultiplexedPath / Traversable that isn't
        # backed by a real on-disk file. ``as_file`` would copy it out to a
        # temp dir, but the sidecar adapter just needs a stable filesystem
        # path it can ``exec``, so we ask for that directly. When the
        # resource isn't materialized as a file (rare, e.g. zip imports
        # without resource extraction), report "not found" and let the
        # next source in the priority list answer.
        path = Path(str(candidate))
    except (TypeError, OSError):
        return None
    return str(path) if _is_executable_file(path) else None


def _dev_tree_binary() -> str | None:
    """Locate the Rust binary inside a source / editable checkout.

    Walks upward from this file looking for
    ``experimental/lingtai-search-sidecar/target/release/<bin>`` (or the
    ``debug`` build). This is the path that developers hitting ``pip
    install -e .`` get for free once they've run ``cargo build`` once.
    """
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        crate_root = parent / "experimental" / "lingtai-search-sidecar"
        if not crate_root.is_dir():
            continue
        for profile in ("release", "debug"):
            cand = crate_root / "target" / profile / _BINARY_NAME
            if _is_executable_file(cand):
                return str(cand)
        # Found the crate but no build artifact — stop walking; further
        # ancestors won't have a different crate that would be more
        # authoritative.
        return None
    return None


def resolve_sidecar_binary(
    explicit: str | None = None,
    *,
    skip_env: bool = False,
    skip_packaged: bool = False,
    skip_dev_tree: bool = False,
) -> str | None:
    """Resolve the path of the sidecar binary using the documented order.

    See the module docstring for the four-step priority list. The
    optional ``skip_*`` flags exist for tests that want to isolate one
    layer at a time without monkey-patching the others — production
    callers always leave them at their defaults.

    Returns the absolute path to an executable file on disk, or ``None``
    when no source produced a usable binary.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if _is_executable_file(candidate):
            return str(candidate)
        # Fall back to PATH lookup so ``explicit="lingtai-search-sidecar"``
        # works the same way it does in a shell.
        on_path = shutil.which(explicit)
        if on_path:
            return on_path
        return None
    if not skip_env:
        from_env = _binary_from_env()
        if from_env:
            candidate = Path(from_env).expanduser()
            if _is_executable_file(candidate):
                return str(candidate)
            on_path = shutil.which(from_env)
            if on_path:
                return on_path
    if not skip_packaged:
        packaged = _packaged_binary()
        if packaged:
            return packaged
    if not skip_dev_tree:
        dev = _dev_tree_binary()
        if dev:
            return dev
    return None


def _parse_envelope(stdout: str, stderr: str, returncode: int, op: str) -> dict[str, Any]:
    """Parse the sidecar's JSON envelope.

    The sidecar always prints exactly one JSON object on stdout — even on
    error. We special-case empty output (e.g. binary missing, ``exec``
    failed before printing) so the caller gets a useful message instead of
    an opaque "Expecting value" decode error.
    """
    if not stdout.strip():
        detail = stderr.strip() or f"exit {returncode} with no output"
        raise SidecarError(
            f"sidecar emitted no JSON envelope ({detail})",
            op=op,
            code="empty_output",
            exit_code=returncode,
        )
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SidecarError(
            f"sidecar returned invalid JSON: {exc.msg}",
            op=op,
            code="bad_json",
            exit_code=returncode,
        ) from exc
    if not isinstance(envelope, dict):
        raise SidecarError(
            "sidecar returned non-object JSON",
            op=op,
            code="bad_envelope",
            exit_code=returncode,
        )
    return envelope


def _stats_from_envelope(envelope: dict[str, Any]) -> TraversalStats:
    return TraversalStats(
        visited=int(envelope.get("visited") or 0),
        elapsed_ms=int(envelope.get("elapsed_ms") or 0),
        truncated_reason=envelope.get("truncated_reason"),
        files_skipped_size=int(envelope.get("files_skipped_size") or 0),
        files_skipped_binary=int(envelope.get("files_skipped_binary") or 0),
        dirs_pruned=int(envelope.get("dirs_pruned") or 0),
    )


def _matches_from_envelope(envelope: dict[str, Any], root: str) -> list[GrepMatch]:
    """Convert the sidecar's ``matches`` array into ``GrepMatch`` instances.

    The sidecar emits paths relative to ``root`` (matching its protocol).
    For LingTai's tool surface we want absolute paths — they match what
    ``LocalFileIOBackend.grep`` returns — so we rejoin them here.
    """
    out: list[GrepMatch] = []
    base = Path(root)
    for item in envelope.get("matches") or []:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path", ""))
        absolute = str(base / rel) if rel and not Path(rel).is_absolute() else rel
        out.append(
            GrepMatch(
                path=absolute,
                line_number=int(item.get("line_number") or 0),
                line=str(item.get("line", "")),
            )
        )
    return out


def _paths_from_envelope(envelope: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in envelope.get("paths") or []:
        out.append(str(item))
    return out


class RustFileIOBackend(FileIOBackend):
    """``FileIOBackend`` that delegates grep/glob to the Rust sidecar.

    Read/write/edit go through an internal ``LocalFileIOBackend`` so root
    resolution and sandbox semantics stay consistent with the default
    backend. Recursive ops are routed to the sidecar, which enforces the
    same default-exclude / walltime / visited / file-size budgets.

    Failure mode: the sidecar is treated as load-bearing. If it cannot run,
    or returns an error, the call raises ``SidecarError`` instead of
    silently falling back to Python. Callers who want soft fallback
    should explicitly use ``LocalFileIOBackend`` instead.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        binary_path: str | None = None,
        adapter: SidecarAdapter | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._local = LocalFileIOBackend(root=root)
        self._adapter = adapter or SidecarAdapter(
            binary_path=binary_path, timeout_s=timeout_s
        )
        self.last_traversal: TraversalStats = TraversalStats()

    @property
    def root(self) -> Path | None:
        return self._local._root  # noqa: SLF001 — sibling backend, intentional.

    def available(self) -> bool:
        return self._adapter.available()

    # ------------------------------------------------------------------
    # Direct-delegation surface
    # ------------------------------------------------------------------
    def read(self, path: str) -> str:
        return self._local.read(path)

    def write(self, path: str, content: str) -> None:
        self._local.write(path, content)

    def edit(self, path: str, old_string: str, new_string: str) -> str:
        return self._local.edit(path, old_string, new_string)

    # ------------------------------------------------------------------
    # Sidecar-backed surface
    # ------------------------------------------------------------------
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
        search_root, sandbox_root = self._sandbox_paths(root)
        envelope = self._adapter.call(
            SidecarRequest(
                op="glob",
                root=str(sandbox_root),
                path=str(search_root),
                pattern=pattern,
                max_results=_int_or_default(max_results, 2000),
                max_visited=_int_or_default(max_visited, DEFAULT_MAX_VISITED),
                walltime_ms=_walltime_ms(walltime_s, DEFAULT_WALLTIME_S),
                max_file_bytes=DEFAULT_MAX_FILE_BYTES,
                exclude_dirs=_excludes(exclude_dirs),
            )
        )
        self.last_traversal = _stats_from_envelope(envelope)
        results = _paths_from_envelope(envelope)
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
        search_path, sandbox_root = self._sandbox_paths(path)
        envelope = self._adapter.call(
            SidecarRequest(
                op="grep",
                root=str(sandbox_root),
                path=str(search_path),
                pattern=pattern,
                max_results=max_results,
                max_visited=_int_or_default(max_visited, DEFAULT_MAX_VISITED),
                walltime_ms=_walltime_ms(walltime_s, DEFAULT_WALLTIME_S),
                max_file_bytes=_int_or_default(max_file_bytes, DEFAULT_MAX_FILE_BYTES),
                exclude_dirs=_excludes(exclude_dirs),
            )
        )
        self.last_traversal = _stats_from_envelope(envelope)
        return _matches_from_envelope(envelope, str(sandbox_root))

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------
    def _sandbox_paths(self, target: str | None) -> tuple[Path, Path]:
        """Resolve ``target`` to ``(absolute_search_path, sandbox_root)``.

        The sidecar always wants two absolute paths: the root that anchors
        relative match-paths and bounds the traversal, and the actual
        starting directory (or file) within that root. We mirror the
        rules from ``LocalFileIOBackend``: an explicit ``root=`` on the
        backend wins; otherwise the caller-supplied path doubles as both
        the search target and the root; otherwise CWD.
        """
        backend_root = self.root
        if target is not None:
            search = Path(target)
            if backend_root is not None and not search.is_absolute():
                search = backend_root / search
        elif backend_root is not None:
            search = backend_root
        else:
            search = Path.cwd()
        search = search.expanduser().resolve()
        sandbox = (
            backend_root.expanduser().resolve() if backend_root is not None else search
        )
        # If the caller provides a target outside the sandbox root, treat
        # the target's own root as the sandbox — this matches the
        # ``LocalFileIOBackend`` behavior of "no root means anywhere".
        try:
            search.relative_to(sandbox)
        except ValueError:
            sandbox = search
        return search, sandbox


def _int_or_default(value: int | None, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _walltime_ms(value: float | None, default: float) -> int:
    seconds = default if value is None else value
    return max(0, int(round(seconds * 1000)))


def _excludes(value: frozenset[str] | set[str] | None) -> tuple[str, ...]:
    if value is None:
        return tuple(sorted(DEFAULT_EXCLUDED_DIRS))
    return tuple(sorted(value))


#: Env var that selects which file-I/O backend the default factory hands out.
#:
#: ``auto`` (default) — return ``LocalFileIOService`` backed by the Rust
#:   sidecar when one can be resolved, otherwise the pure-Python
#:   ``LocalFileIOBackend``. Silent, soft fallback.
#: ``rust`` — return the Rust-backed service. If no sidecar can be
#:   resolved, raise ``SidecarError(code="not_configured")`` rather than
#:   silently using Python — the operator asked for the native path
#:   explicitly and a quiet downgrade would defeat the purpose.
#: ``python`` — return ``LocalFileIOService`` backed by ``LocalFileIOBackend``
#:   unconditionally. Use this to force the pure-Python path for
#:   debugging or in environments where the sidecar misbehaves.
BACKEND_ENV_VAR = "LINGTAI_FILE_IO_BACKEND"
_VALID_BACKENDS = frozenset({"auto", "rust", "python"})


def default_file_io_service(
    root: Path | str | None = None,
    *,
    backend: str | None = None,
):
    """Return a tool-facing ``LocalFileIOService`` with the right backend.

    This is the single entry point used by ``Agent`` (and any host code
    that wants the same behavior). It reads ``LINGTAI_FILE_IO_BACKEND``
    when ``backend`` is not passed explicitly, then assembles the right
    ``LocalFileIOService`` instance.

    Args:
        root: Working directory anchor passed to ``LocalFileIOBackend`` /
            ``RustFileIOBackend``. ``None`` means "no sandbox" — callers
            can still pass absolute paths to the service.
        backend: Override for the env var. One of ``"auto"``, ``"rust"``,
            ``"python"``. ``None`` (default) defers to the env var; if
            that's also unset, ``"auto"`` is used.

    Returns:
        A ``LocalFileIOService`` instance ready to back read/write/edit/
        glob/grep tool calls. The Python and Rust paths are wire-compatible —
        callers don't need to know which one they got.

    Raises:
        SidecarError: ``backend == "rust"`` but no sidecar binary could
            be resolved. Soft "auto" mode never raises.
        ValueError: ``backend`` (or the env var) is not one of the three
            recognized values.
    """
    # Import here to avoid a circular import at module load time:
    # ``file_io`` imports nothing from this module, but having the
    # service factory defined here keeps the resolver and backend wiring
    # next to one another.
    from .file_io import LocalFileIOService

    selection = (backend or os.environ.get(BACKEND_ENV_VAR) or "auto").strip().lower()
    if selection not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown {BACKEND_ENV_VAR}={selection!r}; "
            f"expected one of: {sorted(_VALID_BACKENDS)}"
        )

    if selection == "python":
        return LocalFileIOService(root=root)

    # Both "auto" and "rust" want the native path if one is available.
    binary = resolve_sidecar_binary()
    if binary is None:
        if selection == "rust":
            envs = " or ".join(SIDECAR_ENV_VARS)
            raise SidecarError(
                f"{BACKEND_ENV_VAR}=rust requested but no sidecar binary was "
                f"found. Install Rust and rebuild the wheel, point {envs} at a "
                "prebuilt binary, or set the env var to 'python' / 'auto'.",
                code="not_configured",
            )
        # auto: soft fallback to pure Python.
        return LocalFileIOService(root=root)

    rust_backend = RustFileIOBackend(root=root, binary_path=binary)
    return LocalFileIOService(backend=rust_backend)
