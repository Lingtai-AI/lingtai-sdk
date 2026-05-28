# lingtai

PyPI wrapper package — `Agent(BaseAgent)` with composable capabilities, preset materialization, CLI, and public re-exports.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | Role |
|---|---|
| `__init__.py` | Public API facade — re-exports `Agent`, `BaseAgent`, `Message`, services from kernel+wrapper |
| `__main__.py` | `python -m lingtai` → `cli.main()` |
| `agent.py` | **THE key file.** `Agent(BaseAgent)` — layer-2 agent with capability composition, preset swap, MCP, init.json refresh |
| `cli.py` | `lingtai-agent run <dir>` / `lingtai-agent check-caps` entry points |
| `network.py` | Read-only network topology crawler — avatar/contact/mail edge discovery |
| `presets.py` | Compatibility shim re-exporting the kernel preset library (`lingtai_kernel.presets`) |
| `init_schema.py` | `validate_init()` — strict schema for init.json |
| `config_resolve.py` | Compatibility shim for `lingtai_kernel.config_resolve` |
| `venv_resolve.py` | Python venv resolution — init.json → global runtime → auto-create |
| `preset_connectivity.py` | Compatibility shim for `lingtai_kernel.preset_connectivity` |
| `intrinsic_skills/__init__.py` | Standalone skill bundles (docs-only) copied into `.library/intrinsic/` |

### Key functions / classes

**`agent.py`** — `Agent(BaseAgent)`: `__init__` :33 (accept `capabilities=` + `disable=`, expand groups, `apply_core_defaults`, decompress addons, setup caps, install manuals, load MCP) · `_setup_capability` :148 · `_persist_llm_config` :123 · `_install_intrinsic_manuals` :170 · `_load_mcp_from_workdir` :372 (also tracks specs in `_mcp_init_specs`) · `_retry_failed_mcps` :520 (re-spawn dead MCPs on `system(refresh)` — issue #34) · `_read_init` :829 (read + materialize preset + validate) · `_setup_from_init` :965 (**full reconstruct** — shared by boot and live refresh; reads `manifest.disable` and re-applies `apply_core_defaults`) · `_activate_preset` :891 (runtime swap, atomic write) · `_reload_prompt_sections` :1205 (writes `covenant`/`substrate`/`rules`/`principle`/`procedures`/`brief`/`comment`; delegates `character` to `_lingtai_load` and `pad` to `_pad_load` — the canonical composers — so boot/refresh/molt are consistent and hook-order-independent) · `connect_mcp` :700 / `connect_mcp_http` :752 · `start` :693 / `stop` :798

**`cli.py`**: `load_init` :21 · `build_agent` :72 · `run` :200 · `main` :246

**`presets.py`**: compatibility re-export shim (`presets.py:1-21`); implementation lives in `lingtai_kernel.presets` (`load_preset` :175 · `materialize_active_preset` :290 · `expand_inherit` :444 · `discover_presets_in_dirs` :121).

**`init_schema.py`**: `validate_init` :85 · `TOP_OPTIONAL` :13 · `MANIFEST_OPTIONAL` :42

**`config_resolve.py`**: compatibility shim; implementation lives in `lingtai_kernel.config_resolve` (`load_jsonc` :16 · `resolve_env` :42 · `resolve_paths` :98 · `_resolve_capabilities` :122).

**`network.py`**: `build_network` :306 · `_discover_agents` :143 · `_build_avatar_edges` :168

**`venv_resolve.py`**: `resolve_venv` :19 · `venv_python` :40 · `ensure_package` :94

**`preset_connectivity.py`**: compatibility shim; implementation lives in `lingtai_kernel.preset_connectivity` (`check_connectivity` :64 · `check_many` :119).

## Connections

**Inbound:** `lingtai-tui` calls `cli.run()` to boot agents; imports `load_preset`, `discover_presets_in_dirs` for UI. Kernel's `BaseAgent` is the parent class.

**Outbound — kernel:** `lingtai_kernel.base_agent.BaseAgent`, `.config.AgentConfig`, `.prompt.build_system_prompt`, `.handshake.resolve_address`, `.intrinsics.{email,psyche}`, `.services.mail.FilesystemMailService`, `.migrate.run_migrations`.

**Cross-module:** `agent.py` → `capabilities.setup_capability`, `core.mcp.{decompress_addons,read_registry,MCPInboxPoller}`, `services.mcp.{MCPClient,HTTPMCPClient}`, `llm.service.LLMService`, `presets`, `config_resolve`, `init_schema`. `cli.py` → `agent.Agent`, `config_resolve`, `presets`.

**Agent → BaseAgent:** Three-layer hierarchy: `BaseAgent` (kernel) → `Agent` (capabilities) → `CustomAgent` (domain). Agent adds capability registration, MCP auto-loading, preset swap, full init.json reconstruct.

**Capability registration:** `setup_capability()` in `capabilities/__init__.py`; the registry is `_BUILTIN` (per-capability module paths) plus `CORE_DEFAULTS` (which boot automatically). Agent calls `apply_core_defaults` + `_setup_capability` (agent.py:148) during `__init__` and `_setup_from_init`. Hosts disable defaults via the `disable=[...]` kwarg or `manifest.disable` in init.json.

**Preset materialization:** `materialize_active_preset` (`lingtai_kernel/presets.py:290`) called by `cli.load_init` (boot) and `Agent._read_init` (refresh). Reads `manifest.preset.active`, loads preset, substitutes `llm`+`capabilities` into manifest before validation.

## Composition

Parent: `src/lingtai/` under `lingtai-kernel/src/` alongside `lingtai_kernel/` (kernel package). Siblings: `capabilities/`, `core/`, `llm/`, `services/`, `auth/`, `i18n/`. See `../ANATOMY.md`.

## State

| Path | When | What |
|---|---|---|
| `<workdir>/init.json` | `_activate_preset` :880, `cli.run` :200 | Preset swap (atomic write); venv_path writeback |
| `<workdir>/system/llm.json` | `_persist_llm_config` :111 | LLM provider/model/base_url for revive |
| `<workdir>/system/{covenant,principle,substrate,procedures,brief,rules,pad,lingtai}.md` + `pad_append.json` | `_reload_prompt_sections` :1205 | Prompt sections from init.json + disk. `covenant.md`→`covenant`, `lingtai.md`→`character` (via `_lingtai_load`), `pad.md`+`pad_append.json`→`pad` (via `_pad_load`). `character` is the agent's self-authored identity (灵台), distinct from `covenant` (operator contract) and the mechanical `identity` section. `substrate` is kernel-owned, cross-app stable (issue #39); auto-seeded from packaged `lingtai/prompts/substrate.md` (v1) on first boot if neither `data["substrate"]` nor `system/substrate.md` provides content — see `_setup_from_init` :965. |
| `<workdir>/.library/intrinsic/` | `_install_intrinsic_manuals` :158 | Wipe-and-rewrite every boot |
| `<workdir>/.agent.json` | `_build_manifest` :246 via `_workdir.write_manifest` | Runtime manifest snapshot. Includes sanitized `llm` (provider/model/base_url) from the live LLMService and `preset` (active/default/allowed) read from `init.json` by `_read_preset_from_init` :284 — see issue #78. |
| `<workdir>/.mcp_inbox/` | MCPInboxPoller (started at :761) | LICC events from out-of-process MCPs |

## Notes

- **Boot vs refresh share one code path:** `cli.build_agent` constructs minimal `Agent`, calls `_setup_from_init()` :954. Live refresh re-enters the same method.
- **`materialize_active_preset` is pure dict mutation** — disk write only in `_activate_preset` :880 (atomic `.tmp` → replace).
- **Preset implementation moved to kernel** — wrapper `presets.py` re-exports `lingtai_kernel.presets`; preset validation normalizes legacy shapes via kernel migrations before type-checking.
- **Sensitive key stripping (capabilities):** `_build_manifest` :246 strips `api_key`, `api_key_env`, `api_secret`, `token`, `password` (`_SENSITIVE_KEYS`) from capability kwargs before writing `.agent.json`.
- **LLM / preset safelists (issue #78):** `_build_manifest` :246 also re-applies `_LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")` to the kernel-supplied `llm` block as defense-in-depth, and reads `manifest.preset` from init.json via `_read_preset_from_init` :284 filtered to `_PRESET_PUBLIC_KEYS = ("active", "default", "allowed")`. Anything outside the safelists never reaches `.agent.json` or the identity prompt section. This is the central safety claim of #78 — see `tests/test_agent_preset_manifest.py::test_manifest_never_contains_api_key`.
- **Drift hazard:** `_setup_from_init` :954 manually reconstructs `AgentConfig` with inline defaults that MUST mirror `lingtai_kernel.config.AgentConfig` (inline construction in `_setup_from_init`, nothing enforces).
- **Addon decompression** runs BEFORE capability setup so `mcp` capability sees populated `mcp_registry.jsonl` on first reconcile (`Agent.__init__` :33, `_setup_from_init` :954).
- **MCP retry contract (issue #34):** `_load_mcp_from_workdir` :360 records every registered init.json mcp entry into `self._mcp_init_specs` (name → `{cfg, source, client}`). `_retry_failed_mcps` :508 walks this dict, closes any dead client (`is_connected()` False), respawns with the original config, and reports `{retried, recovered, still_failed, healthy}`. `system(action="refresh")` calls it via `intrinsics/system/preset.py:_refresh` before `_perform_refresh` so the documented "fix config → refresh" recovery path works without full process restart.
