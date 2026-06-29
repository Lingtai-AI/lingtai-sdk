---
related_files:
  - src/lingtai_kernel/ANATOMY.md
  - src/lingtai_kernel/migrate/__init__.py
  - src/lingtai_kernel/migrate/agent_m001_init_procedures_override.py
  - src/lingtai_kernel/migrate/agent_m002_mcp_launch_args_rewrite.py
  - src/lingtai_kernel/migrate/agent_m003_init_prompt_contract.py
  - src/lingtai_kernel/migrate/m001_context_limit_relocation.py
  - src/lingtai_kernel/migrate/m002_description_object.py
  - src/lingtai_kernel/migrate/migrate.py
  - tests/test_cli.py
  - tests/test_deep_refresh.py
  - tests/test_kernel_migrate.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# migrate

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues/mail/PR proposals; do not silently fix.

Versioned, append-only, forward-only migrations for kernel-managed on-disk state. This folder is the kernel's migration system: when changing an on-disk shape owned by the kernel, look here first and extend the appropriate registry instead of adding an ad-hoc boot-time helper. The runner currently has two domains: **preset library** migrations over preset `.json`/`.jsonc` directories, and **agent workdir** migrations over one agent directory (including `init.json`). Each domain has its own version counter and append-only registry, but both use the same runner/validation/cache machinery.

## Components

- `__init__.py` — public migration facade. Exports preset-domain `CURRENT_VERSION` / `run_migrations`, agent-domain `AGENT_CURRENT_VERSION` / `run_agent_migrations`, plus meta helpers (`__init__.py:31-45`).
- `migrate.py` — shared runner, registries, and version tracking.
  - `_META_FILENAME = "_kernel_meta.json"` — preset-domain meta file name (`migrate.py:53`).
  - `_AGENT_META_REL = Path("system") / "migrations" / _META_FILENAME` — agent-domain meta file path relative to the workdir (`migrate.py:57`).
  - `_migrated: set[str]` — process-level guard keyed by `domain:resolved_path` (`migrate.py:61`).
  - `_PRESET_MIGRATIONS` — append-only preset registry (`migrate.py:67-70`).
  - `_AGENT_MIGRATIONS` — append-only agent/workdir registry (`migrate.py:72-76`).
  - `_MIGRATIONS` — backwards-compatible alias for `_PRESET_MIGRATIONS`, retained for older internal tests/callers (`migrate.py:80`).
  - `_validate_registry()` — import-time sanity check: contiguous, strictly-increasing, callable (`migrate.py:83-128`).
  - `CURRENT_VERSION` / `AGENT_CURRENT_VERSION` — derived from the two registries; no hand-maintained constants (`migrate.py:131-132`).
  - `meta_filename()` / `agent_meta_relative_path()` — expose on-disk meta locations (`migrate.py:135-142`).
  - `_load_version(meta_path)` — reads a version file, returns 0 on missing/malformed (`migrate.py:145-166`).
  - `_save_version(meta_path, version, domain=...)` — atomic PID-suffixed tmp + `os.replace`; accepts either a concrete meta file or a preset directory for compatibility (`migrate.py:170-196`).
  - `_run_versioned_migrations(...)` — shared domain runner: version gate, future-version downgrade guard, failure abort, per-process cache (`migrate.py:201-256`).
  - `run_migrations(presets_path)` — preset-domain entry point (`migrate.py:261-282`).
  - `run_agent_migrations(working_dir)` — agent-domain entry point, called before `init.json` read/validation (`migrate.py:286-304`).
  - `reset_process_cache()` — test-only; clears `_migrated` (`migrate.py:309-313`).
- `m001_context_limit_relocation.py` — preset m001: moves `manifest.context_limit` → `manifest.llm.context_limit` (`m001_context_limit_relocation.py:42`). Local `_load_jsonc()` avoids importing from `lingtai` (`m001_context_limit_relocation.py:25-39`).
- `m002_description_object.py` — preset m002: promotes string `description` to `{summary, tier?}`; folds `tags:[tier:N]` into `description.tier`; deletes `tags` (`m002_description_object.py:64`). Local `_load_jsonc()` (`m002_description_object.py:37-44`); `_extract_tier()` (`m002_description_object.py:47-61`).
- `agent_m001_init_procedures_override.py` — agent m001: archives non-empty `init.json.procedures` to `<workdir>/system/migrations/init-procedures-<sha256>.md`, removes `procedures` and `procedures_file`, and best-effort logs `init_procedures_override_migrated` before the agent object exists (`agent_m001_init_procedures_override.py:62`).
- `agent_m002_mcp_launch_args_rewrite.py` — agent m002: rewrites legacy `["-m", "lingtai_<name>"]` MCP launch module args to the canonical `lingtai.mcp_servers.<name>` form in `init.json` / `mcp_registry.jsonl` (`agent_m002_mcp_launch_args_rewrite.py:120`).
- `agent_m003_init_prompt_contract.py` — agent m003: enforces the init-prompt contract (external prompt surface = `base_prompt`/`covenant`/`comment`). Archives non-empty inline `init.json.substrate`, removes retired `substrate`/`substrate_file`, removes deprecated `brief`/`brief_file` without seeding prompt content, and best-effort logs `init_prompt_contract_migrated` (`agent_m003_init_prompt_contract.py:82`).

## Connections

- **Inbound — preset domain:** `lingtai.presets.discover_presets_in_dirs` calls `run_migrations(p)` before listing presets (`presets.py:144,157`). `lingtai.presets.load_preset` calls `run_migrations(p.parent)` before reading a file (`presets.py:200,224`). Both paths import `meta_filename()` to skip `_kernel_meta.json` during directory scans (`presets.py:145`).
- **Inbound — agent domain:** `lingtai.cli.load_init` calls `run_agent_migrations(working_dir)` before it reads `init.json` for process boot (`../lingtai/cli.py:32-39`). `lingtai.Agent._read_init` calls the same entry before live refresh/setup reads `init.json` (`../lingtai/agent.py:920-924`). This keeps boot and refresh on one migration path.
- **Outbound — preset migrations:** Rewrite preset files in the target directory. Each migration uses atomic tmp + `os.replace` and local JSONC parsing; no imports from the wrapper package.
- **Outbound — agent migrations:** Rewrite files under one agent workdir, including `init.json` and archive artifacts under `system/migrations/`. Agent migrations may best-effort append events to `logs/events.jsonl` because they run before `Agent` / `BaseAgent._log` may exist.
- **Boundary contract:** Public imports come from `lingtai_kernel.migrate`: use `run_migrations` only for preset-library directories; use `run_agent_migrations` for per-agent workdirs/init migrations. Do not add one-off init cleanup in `Agent._read_init()` unless it is merely invoking this versioned runner.

## Composition

- **Parent:** `src/lingtai_kernel/` (see `src/lingtai_kernel/ANATOMY.md`).
- **Subfolders:** none.

## State

- **Preset on-disk:** `<presets_dir>/_kernel_meta.json` — `{"version": N}`, persisted after each successful preset migration step (`migrate.py:279-282` via `_save_version`). Created only when at least one preset migration runs.
- **Agent on-disk:** `<workdir>/system/migrations/_kernel_meta.json` — `{"version": N, "domain": "agent"}`, persisted after each successful agent migration step (`migrate.py:298-301` via `_save_version`). The same directory may hold migration artifacts such as `init-procedures-<sha256>.md`.
- **Process-level:** `_migrated: set[str]` — `domain:resolved_path` keys already migrated this process. Checked in `_run_versioned_migrations` (`migrate.py:206-208`); populated on no-op/future/current/success paths (`migrate.py:212,225,229,256`).
- **Ephemeral:** migrations rewrite target files in place (atomic tmp + `os.replace`). No rollback artifacts are left on success except intentional archives.

## Notes

- **This is the reminder:** if you are about to change an on-disk kernel-owned shape (preset JSON, agent `init.json`, or another durable kernel file), first inspect/extend this migration system. Do not “just add a cleanup helper” in the boot path and call it a migration.
- **Forward-only:** a meta file with version > current domain version (e.g. from a newer kernel later downgraded) is honored as-is; no migrations run; a warning is logged (`migrate.py:216-226`).
- **Contiguity enforced per domain:** `_validate_registry` raises `RuntimeError` if a registry has gaps, duplicates, or non-callable entries (`migrate.py:76-126`).
- **Concurrency safety:** PID-suffixed tmp files prevent parent + avatar processes sharing a target from clobbering each other's in-flight writes (`migrate.py:180`).
- **Domain separation:** preset migrations should stay generic over preset directories and should not assume an agent workdir. Agent migrations may touch `init.json`, `system/`, `logs/`, and other workdir-local files, but must remain idempotent and version-gated.
- **Validation ordering:** agent migrations run before `validate_init()` so retired keys can be removed/archived before schema validation; schema should mark migrated legacy keys as known-but-inactive only when stale files might still be observed.
- **Tests:** runner/domain behavior lives in `tests/test_kernel_migrate.py`; boot/refresh integration behavior lives in `tests/test_deep_refresh.py` and `tests/test_cli.py`.
