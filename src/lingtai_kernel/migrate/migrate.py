"""Versioned migration runner for kernel-managed on-disk state.

Mirrors `tui/internal/globalmigrate` from the TUI repo. The TUI runs its
analogue once at process start against `~/.lingtai-tui/`; the kernel owns
append-only, forward-only migration registries for the on-disk shapes it
manages.

Two domains currently share this runner:

- **Preset library migrations** run once per preset directory, triggered
  lazily from `lingtai.presets.discover_presets` / `load_preset`. Their
  version counter lives in `<presets_dir>/_kernel_meta.json`.
- **Agent workdir migrations** run once per agent working directory before
  `init.json` validation/refresh. Their version counter lives in
  `<workdir>/system/migrations/_kernel_meta.json`.

Each domain has an append-only registry. Each migration claims a strictly
increasing version number. A migration runs at most once per target directory;
when its version number ≤ the on-disk counter, it is skipped.

Best-practice invariants:
- Versions form a contiguous strictly-increasing sequence (1, 2, 3, ...)
  within each domain. The runner asserts this at import time so a typo in a
  registry fails fast rather than silently mis-ordering migrations.
- Current versions are derived from registries — there are no hand-maintained
  constants that can drift out of sync with the migrations actually registered.
- Forward-only: a meta file with a future version (e.g. from a newer kernel
  that was later downgraded) is honored as-is and never rolled back. A warning
  is logged so the operator knows.
- Tmp-file writes use a PID suffix so concurrent processes (parent + avatar)
  sharing the same target directory do not clobber each other's in-flight write.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable

from .agent_m001_init_procedures_override import migrate_init_procedures_override
from .agent_m002_mcp_launch_args_rewrite import migrate_mcp_launch_args_rewrite
from .m001_context_limit_relocation import migrate_context_limit_relocation
from .m002_description_object import migrate_description_object

log = logging.getLogger(__name__)

# Filename used to track preset-library migration state. Lives inside the
# presets directory itself so each preset library carries its own migration
# state. The leading underscore signals "internal" to humans browsing the
# directory; discover_presets_in_dirs explicitly skips this filename.
_META_FILENAME = "_kernel_meta.json"

# Agent-workdir migration state lives under system/migrations beside any archive
# artifacts produced by agent-domain migrations.
_AGENT_META_REL = Path("system") / "migrations" / _META_FILENAME

# Per-process guard so we run at most once per (domain, target path) per process.
# Keyed by "<domain>:<resolved absolute path>".
_migrated: set[str] = set()


# Append-only registries. Each entry: (version, name, function).
# Versions MUST form a strictly-increasing contiguous sequence starting at 1
# within each registry. The validator below catches violations at import time.
_PRESET_MIGRATIONS: tuple[tuple[int, str, Callable[[Path], None]], ...] = (
    (1, "context_limit_relocation", migrate_context_limit_relocation),
    (2, "description_object", migrate_description_object),
)

_AGENT_MIGRATIONS: tuple[tuple[int, str, Callable[[Path], None]], ...] = (
    (1, "init_procedures_override", migrate_init_procedures_override),
    (2, "mcp_launch_args_rewrite", migrate_mcp_launch_args_rewrite),
)

# Backwards-compatible alias for tests and older internal callers that inspect
# the original preset migration registry directly.
_MIGRATIONS = _PRESET_MIGRATIONS


def _validate_registry(
    migrations: tuple[tuple[int, str, Callable[[Path], None]], ...] | None = None,
    *,
    domain: str = "kernel migrate",
) -> int:
    """Sanity-check a registry shape at import time.

    Returns the highest registered version, which becomes the domain's current
    version. Raises RuntimeError if the registry violates contiguity, ordering,
    or uniqueness — programmer errors that should fail loudly before user data
    is touched.

    ``migrations`` defaults to ``_MIGRATIONS`` for compatibility with older
    tests that monkeypatch that name directly.
    """
    if migrations is None:
        migrations = _MIGRATIONS
    if not migrations:
        return 0
    seen: set[int] = set()
    expected = 1
    for entry in migrations:
        if not (isinstance(entry, tuple) and len(entry) == 3):
            raise RuntimeError(
                f"{domain}: malformed entry {entry!r} — expected (version, name, function)"
            )
        version, name, fn = entry
        if not isinstance(version, int) or version <= 0:
            raise RuntimeError(
                f"{domain}: version must be a positive int, got {version!r} "
                f"(in {name!r})"
            )
        if version in seen:
            raise RuntimeError(f"{domain}: duplicate version {version} (in {name!r})")
        if version != expected:
            raise RuntimeError(
                f"{domain}: expected version {expected}, got {version} "
                f"(in {name!r}) — versions must be strictly increasing and contiguous"
            )
        if not callable(fn):
            raise RuntimeError(
                f"{domain}: function for version {version} ({name!r}) is not callable"
            )
        seen.add(version)
        expected += 1
    return migrations[-1][0]


CURRENT_VERSION: int = _validate_registry(_PRESET_MIGRATIONS, domain="kernel preset migrate registry")
AGENT_CURRENT_VERSION: int = _validate_registry(_AGENT_MIGRATIONS, domain="kernel agent migrate registry")


def meta_filename() -> str:
    """The filename `discover_presets` must skip when listing presets."""
    return _META_FILENAME


def agent_meta_relative_path() -> Path:
    """Relative path of the agent-workdir migration version file."""
    return _AGENT_META_REL


def _load_version(meta_path: Path) -> int:
    """Read an on-disk version counter. Returns 0 when missing or unreadable."""
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError as e:
        log.warning("kernel migrate: failed to read %s: %s", meta_path, e)
        return 0
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("kernel migrate: malformed %s: %s — treating as version 0", meta_path, e)
        return 0
    if not isinstance(data, dict):
        log.warning(
            "kernel migrate: malformed %s: expected object, got %s — treating as version 0",
            meta_path,
            type(data).__name__,
        )
        return 0
    v = data.get("version", 0)
    return v if isinstance(v, int) else 0


def _save_version(meta_path: Path, version: int, *, domain: str | None = None) -> None:
    """Atomically persist a version counter.

    Backwards-compatible input: callers may pass either the concrete meta file
    path or a preset directory. Directory input is normalized to
    ``<dir>/_kernel_meta.json``.

    The tmp file uses a PID suffix so concurrent processes sharing this target
    cannot clobber each other's in-flight write. os.replace is atomic on POSIX
    and Windows for same-filesystem renames.
    """
    if meta_path.is_dir():
        meta_path = meta_path / _META_FILENAME
    tmp = meta_path.with_name(f"{meta_path.name}.{os.getpid()}.tmp")
    payload_data: dict[str, object] = {"version": version}
    if domain is not None:
        payload_data["domain"] = domain
    payload = json.dumps(payload_data, ensure_ascii=False)
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(meta_path))
    except OSError as e:
        log.warning("kernel migrate: failed to write %s: %s", meta_path, e)
        # Best-effort cleanup so we don't leave orphan tmp files behind.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _run_versioned_migrations(
    target_path: Path | str,
    *,
    domain: str,
    migrations: tuple[tuple[int, str, Callable[[Path], None]], ...],
    current_version: int,
    meta_path_for: Callable[[Path], Path],
) -> None:
    """Run one migration registry against one target directory."""
    p = Path(target_path)
    try:
        resolved_key = f"{domain}:{p.resolve()}"
    except OSError:
        return  # path doesn't resolve — nothing to do
    if resolved_key in _migrated:
        return
    if not p.is_dir():
        _migrated.add(resolved_key)
        return

    meta_path = meta_path_for(p)
    current = _load_version(meta_path)

    if current > current_version:
        log.warning(
            "kernel %s migrate: %s reports version %d but this kernel only knows up to %d "
            "— honoring on-disk version, running no migrations (likely a downgrade)",
            domain,
            p,
            current,
            current_version,
        )
        _migrated.add(resolved_key)
        return

    if current == current_version:
        _migrated.add(resolved_key)
        return

    for version, name, fn in migrations:
        if version <= current:
            continue
        try:
            fn(p)
        except Exception as e:
            log.warning(
                "kernel %s migrate %d (%s) failed for %s: %s — aborting run, will retry next launch",
                domain,
                version,
                name,
                p,
                e,
            )
            return
        current = version
        _save_version(meta_path, current, domain=domain if domain != "preset" else None)

    _migrated.add(resolved_key)


def run_migrations(presets_path: Path | str) -> None:
    """Run pending kernel migrations against the given presets directory.

    Idempotent and process-cached: subsequent calls in the same process for
    the same path are no-ops. Reads the current version from
    `<presets_path>/_kernel_meta.json` (defaulting to 0), runs all registered
    preset-library migrations whose version is greater than the current value,
    and persists the new version after each successful step.

    Failures in individual migrations log a warning and abort the run for this
    path (no partial version advancement past the failed step). Subsequent
    process starts will retry from the last-successful version.

    A nonexistent presets directory is a no-op — there's nothing to migrate,
    and we don't want to create the directory implicitly.
    """
    _run_versioned_migrations(
        presets_path,
        domain="preset",
        migrations=_PRESET_MIGRATIONS,
        current_version=CURRENT_VERSION,
        meta_path_for=lambda p: p / _META_FILENAME,
    )


def run_agent_migrations(working_dir: Path | str) -> None:
    """Run pending kernel migrations against one agent working directory.

    This is the version-controlled entry point for agent-local on-disk shape
    changes, including `init.json` migrations. Call it before reading or
    validating `init.json` so boot and refresh see the migrated shape.

    A directory without ``init.json`` is a no-op. Do not create migration meta
    for a half-created workdir, or a later first boot would incorrectly skip
    init migrations.
    """
    p = Path(working_dir)
    if not (p / "init.json").is_file():
        return
    _run_versioned_migrations(
        p,
        domain="agent",
        migrations=_AGENT_MIGRATIONS,
        current_version=AGENT_CURRENT_VERSION,
        meta_path_for=lambda p: p / _AGENT_META_REL,
    )


def reset_process_cache() -> None:
    """Clear the per-process migration guard.

    Test-only — not part of the public API. Useful when a test needs to re-run
    migrations against a freshly-built fixture inside the same process.
    """
    _migrated.clear()
