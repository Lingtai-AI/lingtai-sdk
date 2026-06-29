---
related_files:
  - ANATOMY.md
  - src/lingtai/__init__.py
  - src/lingtai/__main__.py
  - src/lingtai/agent.py
  - src/lingtai/auth/ANATOMY.md
  - src/lingtai/capabilities/ANATOMY.md
  - src/lingtai/capabilities/__init__.py
  - src/lingtai/cli.py
  - src/lingtai/core/avatar/ANATOMY.md
  - src/lingtai/core/bash/ANATOMY.md
  - src/lingtai/core/daemon/ANATOMY.md
  - src/lingtai/core/knowledge/ANATOMY.md
  - src/lingtai/core/mcp/ANATOMY.md
  - src/lingtai/core/skills/ANATOMY.md
  - src/lingtai/i18n/ANATOMY.md
  - src/lingtai/init_schema.py
  - src/lingtai/intrinsic_skills/__init__.py
  - src/lingtai/llm/ANATOMY.md
  - src/lingtai/mcp_catalog.json
  - src/lingtai/mcp_servers/ANATOMY.md
  - src/lingtai/mcp_servers/wechat/manager.py
  - src/lingtai/network.py
  - src/lingtai/presets.py
  - src/lingtai/services/ANATOMY.md
  - src/lingtai/venv_resolve.py
  - src/lingtai_kernel/ANATOMY.md
  - tests/test_agent_preset_manifest.py
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
# lingtai

PyPI wrapper package — `Agent(BaseAgent)` with composable capabilities, preset materialization, CLI, and public re-exports.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | Role |
|---|---|
| `__init__.py` | Public API facade — re-exports `Agent`, `BaseAgent`, `Message`, services from kernel+wrapper |
| `__main__.py` | `python -m lingtai` → `cli.main()` |
| `agent.py` | **THE key file.** `Agent(BaseAgent)` — layer-2 agent with capability composition, preset swap, MCP, init.json refresh |
| `cli.py` | `lingtai-agent run <dir>` / `lingtai-agent check-caps` / `lingtai-agent log ...` / `lingtai-agent maintenance cleanup <target>` entry points |
| `network.py` | Read-only network topology crawler — avatar/contact/mail edge discovery |
| `presets.py` | Compatibility shim re-exporting the kernel preset library (`lingtai_kernel.presets`) |
| `init_schema.py` | `validate_init()` plus `strip_deprecated()` — strict schema for active init.json fields, simple deprecated-field cleanup, and known-but-inactive legacy fields migrated by `lingtai_kernel.migrate` |
| `venv_resolve.py` | Python venv resolution — init.json → global runtime → auto-create |
| `intrinsic_skills/__init__.py` | Standalone skill bundles (docs-only) copied into `.library/intrinsic/` |
| `mcp_servers/` | Curated MCP server implementations shipped in the `lingtai` distribution and launched by `mcp_catalog.json` via `python -m lingtai.mcp_servers.<name>`; see `mcp_servers/ANATOMY.md` for the bundled `SKILL.md` manual-action and sidecar packaging contract. Stateful curated servers may own per-agent sidecars: the WeChat manager checkpoints `wechat/state.json` cursor progress and `wechat/inbox_seen.json` replay guards in `mcp_servers/wechat/manager.py:266` and `mcp_servers/wechat/manager.py:912`. |

### Key functions / classes

**`agent.py`** — `Agent(BaseAgent)`: `__init__` :38 (accept `capabilities=` + `disable=`, expand groups, `apply_core_defaults`, decompress addons, setup caps, install manuals, load MCP) · `_setup_capability` :161 · `_persist_llm_config` :136 · `_install_intrinsic_manuals` :183 · `_build_system_prompt` :368 / `_build_system_prompt_batches` :386 (pass the init-prompt contract's third-party `base_prompt` (`self._base_prompt`) to the kernel builder, which renders it after raw `principle` and before the rest of Batch 1) · `_load_mcp_from_workdir` :398 (also tracks specs in `_mcp_init_specs`) · `_retry_failed_mcps` :550 (re-spawn dead MCPs on `system(refresh)` — issue #34) · `_read_init` :906 (runs `lingtai_kernel.migrate.run_agent_migrations()` before reading `init.json`, then materializes preset, strips plain deprecated fields, validates, resolves paths, and publishes the resolved manifest to `system/manifest.resolved.json` via `lingtai_kernel.workdir.write_resolved_manifest` — issue #259) · `_setup_from_init` :1062 (**full reconstruct** — shared by boot and live refresh; reads `manifest.disable` and re-applies `apply_core_defaults`) · `_activate_preset` :988 (runtime swap, atomic write) · `_reload_prompt_sections` :1354 (init-prompt contract: the externally changeable prompt surface is exactly `base_prompt`/`covenant`/`comment`. Resolves `base_prompt` into `self._base_prompt` (mirrored to `system/base_prompt.md`), writes `covenant`/`rules` plus kernel-owned `substrate`/`principle`/`procedures` (init overrides ignored) and `guidance.json`, then disk-sourced `brief` and `comment`; delegates `character` to `_lingtai_load` and `pad` to `_pad_load` — the canonical composers — so boot/refresh/molt are consistent and hook-order-independent) · `connect_mcp` :771 / `connect_mcp_http` :826 · `start` :764 / `stop` :875

**`cli.py`**: `load_init` :21 · `build_agent` :77 · `run` :232 · `_handle_log_command` :282 · `_handle_maintenance_command` :327 · `main` :372

**`presets.py`**: compatibility re-export shim (`presets.py:1-21`); implementation lives in `lingtai_kernel.presets` (`load_preset` :174 · `materialize_active_preset` :289 · `expand_inherit` :503 · `discover_presets_in_dirs` :121).

**`init_schema.py`**: `DEPRECATED_TOP_FIELDS` :28 (plain deprecated top-level fields stripped by `strip_deprecated`), `LEGACY_MIGRATED_TOP_FIELDS` :38 (legacy fields removed by version-controlled agent migrations and known only as inactive schema), `validate_init` :94, `TOP_OPTIONAL` :13, `MANIFEST_OPTIONAL` :55; `manifest.llm.compact_threshold` is validated as positive int or null in the LLM block.

**`network.py`**: `build_network` :306 · `_discover_agents` :143 · `_build_avatar_edges` :168

**`venv_resolve.py`**: `resolve_venv` :19 · `venv_python` :40 · `ensure_package` :94

> Config-resolution helpers (`load_jsonc`/`resolve_env`/`resolve_paths`/`_resolve_capabilities`) and preset-connectivity probing (`check_connectivity`/`check_many`) live in the kernel — import directly from `lingtai_kernel.config_resolve` / `lingtai_kernel.preset_connectivity`. The former wrapper-side compatibility shims were removed (no back-compat shims per repo policy).

## Connections

**Inbound:** `lingtai-tui` calls `cli.run()` to boot agents; imports `load_preset`, `discover_presets_in_dirs` for UI. Kernel's `BaseAgent` is the parent class.

**Outbound — kernel:** `lingtai_kernel.base_agent.BaseAgent`, `.config.AgentConfig`, `.prompt.build_system_prompt`, `.handshake.resolve_address`, `.intrinsics.{email,psyche}`, `.services.mail.FilesystemMailService`, `.migrate.run_migrations` (preset libraries) and `.migrate.run_agent_migrations` (agent workdir/init migrations; see `../lingtai_kernel/migrate/ANATOMY.md`).

**Cross-module:** `agent.py` → `capabilities.setup_capability`, `core.mcp.{decompress_addons,read_registry,MCPInboxPoller}`, `services.mcp.{MCPClient,HTTPMCPClient}`, `llm.service.LLMService`, `presets`, `lingtai_kernel.config_resolve`, `init_schema`. `cli.py` → `agent.Agent`, `lingtai_kernel.config_resolve`, `presets`.

**Agent → BaseAgent:** Three-layer hierarchy: `BaseAgent` (kernel) → `Agent` (capabilities) → `CustomAgent` (domain). Agent adds capability registration, MCP auto-loading, preset swap, full init.json reconstruct.

**Capability registration:** `setup_capability()` in `capabilities/__init__.py`; the registry is `_BUILTIN` (per-capability module paths) plus `CORE_DEFAULTS` (which boot automatically). Agent calls `apply_core_defaults` + `_setup_capability` (agent.py:152) during `__init__` and `_setup_from_init`. Hosts disable defaults via the `disable=[...]` kwarg or `manifest.disable` in init.json.

**Agent init migration + preset materialization:** `run_agent_migrations` (`lingtai_kernel/migrate/migrate.py:285`) is called by `cli.load_init` (boot) and `Agent._read_init` (refresh) before `init.json` is read/validated. Then `materialize_active_preset` (`lingtai_kernel/presets.py:289`) reads `manifest.preset.active`, loads preset, substitutes `llm`+`capabilities` into manifest before validation. The preset owns explicit opt-in capabilities, but per-agent init.json kwargs survive in two ways: (1) for capabilities the preset *also* enables, init.json wins key-by-key; (2) for always-on `CORE_DEFAULTS` capabilities the preset *omits* (daemon, bash, knowledge, …), init.json kwargs are carried forward so `apply_core_defaults` doesn't re-add an empty entry and lose e.g. `daemon.max_emanations`. Non-core optional caps the preset omits are dropped (the swap). `CORE_DEFAULTS` lives in `lingtai.capabilities` and is injected via the `core_defaults=` arg by both callers (`agent._read_init` :866, `cli.load_init` :49) — the kernel does not import the wrapper. `skills.paths` additionally append-merges (preset defaults first).

## Composition

Parent: `src/lingtai/` under `lingtai-kernel/src/` alongside `lingtai_kernel/` (kernel package). Siblings: `capabilities/`, `core/`, `llm/`, `services/`, `auth/`, `i18n/`. See `../ANATOMY.md`.

## State

| Path | When | What |
|---|---|---|
| `<workdir>/init.json` | `_activate_preset` :988, `cli.run` :232, `_read_init` :906 | Preset swap (atomic write); venv_path writeback; boot/refresh cleanup of deprecated top-level fields and archive-preserving init migrations |
| `<workdir>/system/llm.json` | `_persist_llm_config` :136 | LLM provider/model/base_url for revive |
| `<workdir>/system/manifest.resolved.json` | `_read_init` :906 via `lingtai_kernel.workdir.write_resolved_manifest` | Derived runtime artifact (issue #259): fully-resolved manifest (preset materialized, validated, paths resolved) with secret-bearing keys removed, plus `schema`/`generated_at`/`source`/`preset` metadata. Atomic write, regenerated on every boot/refresh/molt-reload; init.json is never written back. |
| `<workdir>/system/{base_prompt,covenant,principle,substrate,procedures,brief,rules,pad,lingtai}.md` + `system/guidance.json` + `pad_append.json` | `_reload_prompt_sections` :1354 | Prompt sections from init.json + disk. **Init-prompt contract:** the externally changeable system-prompt surface is exactly `base_prompt`, `covenant`, and `comment`. `base_prompt` is the third-party (application / recipe / preset) injection point: resolved from `data["base_prompt"]` (inline or `base_prompt_file`) into `self._base_prompt`, mirrored to `system/base_prompt.md`, and rendered by the kernel builder after the raw `principle` section and before the rest of Batch 1 (it is NOT a prompt-manager section). `covenant.md`→`covenant` (operator contract). `lingtai.md`→`character` (via `_lingtai_load`); `pad.md`+`pad_append.json`→`pad` (via `_pad_load`). `character` is the agent's self-authored identity (灵台): the required init seed field `lingtai` (inline or `lingtai_file`) seeds `system/lingtai.md` on boot/refresh (covenant-style disk mirror — once self-authored the on-disk file wins; an empty init `lingtai` does not blank it). It is distinct from `covenant`, from the third-party `base_prompt` injection point, and from the mechanical `identity` section. `lingtai`/`lingtai_file` was renamed from `prompt`/`prompt_file` with **no legacy alias** — a stale `prompt` field is an unknown-field warning and a missing `lingtai` is a hard validation error. Kernel-owned layers are NOT external overrides: `principle.md` mirrors packaged `lingtai/prompts/principle.md` (init `principle`/`principle_file` ignored — legacy-migrated); `substrate` mirrors packaged `lingtai/prompts/substrate.md` on every boot (init `substrate`/`substrate_file` retired and archived by `agent_m003_init_prompt_contract` — kept compact and routed to the packaged `system-manual` skill); `procedures` likewise kernel-owned. `brief` (secretary life context) is no longer an init override: sourced solely from `system/brief.md` on disk (legacy init `brief`/`brief_file` removed and ignored by the same migration because the field is deprecated). `system/guidance.json` is a TUI-readable **derived** mirror serialized from the skill-style Markdown runtime-guidance catalog (`lingtai/prompts/guidance/` — `INDEX.md` + per-section `<id>.md`, assembled by `lingtai_kernel.prompt_catalog.load_guidance_catalog`) and refreshed by `_reload_prompt_sections`; it is not itself a prompt section. The kernel-owned `principle`/`substrate`/`procedures` mirrors keep their skill-style frontmatter on disk, but the rendered prompt section is body-only (frontmatter stripped on read). In those prompt/guidance frontmatter blocks, `related_files` is not ANATOMY and not a dependency map: it is a maintained inner-link graph for crawling related prompt sources (principle ↔ prompt/guidance sources, guidance INDEX ↔ guidance sections), and it should not list tests or indirect package/runtime dependencies merely because they validate or load the files. `lingtai_kernel.prompt` composes no runtime principle prose: language/activeness remain legacy compatibility fields. |
| `<workdir>/.library/intrinsic/` | `_install_intrinsic_manuals` :174 | Wipe-and-rewrite every boot |
| `<workdir>/.agent.json` | `_build_manifest` :262 via `_workdir.write_manifest` | Runtime manifest snapshot. Includes sanitized `llm` (provider/model/base_url) from the live LLMService and `preset` (active/default/allowed) read from `init.json` by `_read_preset_from_init` :300 — see issue #78. |
| `<workdir>/.mcp_inbox/` | MCPInboxPoller (started at :701) | LICC events from out-of-process MCPs |

## Notes

- **Boot vs refresh share one code path:** `cli.build_agent` constructs minimal `Agent`, calls `_setup_from_init()` :989. Live refresh re-enters the same method.
- **Migration discipline:** `lingtai_kernel.migrate` is the kernel's version-controlled migration system for both preset-library and agent-workdir domains (`../lingtai_kernel/migrate/ANATOMY.md:5`). If an `init.json` or other workdir-local on-disk shape changes, add an agent-domain migration there and call/keep `run_agent_migrations()` before validation; do not add ad-hoc one-off helpers in `Agent._read_init()`. Use `init_schema.strip_deprecated()` only for simple discard-only fields that do not need archive/event/version tracking (`init_schema.py:82`). Keep migration tests in `tests/test_kernel_migrate.py` plus boot/refresh coverage in `tests/test_deep_refresh.py` / `tests/test_cli.py`, and update anatomy in the same PR.
- **`materialize_active_preset` is pure dict mutation** — disk write only in `_activate_preset` :915 (atomic `.tmp` → replace).
- **Preset implementation moved to kernel** — wrapper `presets.py` re-exports `lingtai_kernel.presets`; preset validation normalizes legacy shapes via kernel migrations before type-checking.
- **Sensitive key stripping (capabilities):** `_build_manifest` :262 strips `api_key`, `api_key_env`, `api_secret`, `token`, `password` (`_SENSITIVE_KEYS`) from capability kwargs before writing `.agent.json`.
- **LLM / preset safelists (issue #78):** `_build_manifest` :262 also re-applies `_LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")` to the kernel-supplied `llm` block as defense-in-depth, and reads `manifest.preset` from init.json via `_read_preset_from_init` :300 filtered to `_PRESET_PUBLIC_KEYS = ("active", "default", "allowed")`. Anything outside the safelists never reaches `.agent.json` or the identity prompt section. This is the central safety claim of #78 — see `tests/test_agent_preset_manifest.py::test_manifest_never_contains_api_key`.
- **AgentConfig hydration:** `_setup_from_init` :989 rebuilds runtime config via `build_agent_config`, overlaying explicit init.json values onto `lingtai_kernel.config.AgentConfig` defaults. Legacy `max_turns` and `molt_*` manifest values remain deliberately ignored.
- **Addon decompression** runs BEFORE capability setup so `mcp` capability sees populated `mcp_registry.jsonl` on first reconcile (`Agent.__init__` :33, `_setup_from_init` :989).
- **MCP retry contract (issue #34):** `_load_mcp_from_workdir` :376 records every registered init.json mcp entry into `self._mcp_init_specs` (name → `{cfg, source, client}`). `_retry_failed_mcps` :524 walks this dict, closes any dead client (`is_connected()` False), respawns with the original config, and reports `{retried, recovered, still_failed, healthy}`. `system(action="refresh")` calls it via `intrinsics/system/preset.py:_refresh` before `_perform_refresh` so the documented "fix config → refresh" recovery path works without full process restart.
