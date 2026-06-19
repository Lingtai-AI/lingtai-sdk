"""Kernel-managed on-disk migrations.

Per-machine analogue of `tui/internal/globalmigrate` in the TUI repo:
versioned, append-only, forward-only migrations applied to kernel-owned
on-disk shapes.

Domains:
- Preset library migrations target a directory of preset `*.json`/`*.jsonc`
  files (typically `~/.lingtai-tui/presets/`, or a directory referenced by an
  agent's `manifest.preset.allowed`). The version number is tracked in
  `<presets_dir>/_kernel_meta.json`. `lingtai.presets.discover_presets_in_dirs`
  invokes `run_migrations(presets_path)` before listing files, and
  `load_preset` runs them on the file's parent directory before reading.
- Agent workdir migrations target one agent working directory and include
  `init.json` shape migrations. The version number is tracked in
  `<workdir>/system/migrations/_kernel_meta.json`. `lingtai.cli.load_init` and
  `Agent._read_init()` invoke `run_agent_migrations(working_dir)` before
  reading/validating `init.json`.

Conventions:
- Append-only ordered registries in `migrate.py`.
- Each migration lives in `m<NNN>_<name>.py` for preset-domain migrations or
  `agent_m<NNN>_<name>.py` for agent-domain migrations, exporting a
  `migrate_<name>(path: Path) -> None` function.
- Failures are reported via `logging.warning` and abort the run for that path
  (no partial advancement of the version counter).
"""
from __future__ import annotations

from .migrate import (
    AGENT_CURRENT_VERSION,
    CURRENT_VERSION,
    agent_meta_relative_path,
    meta_filename,
    run_agent_migrations,
    run_migrations,
)

__all__ = [
    "AGENT_CURRENT_VERSION",
    "CURRENT_VERSION",
    "agent_meta_relative_path",
    "meta_filename",
    "run_agent_migrations",
    "run_migrations",
]
