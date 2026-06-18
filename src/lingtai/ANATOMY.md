# lingtai

PyPI wrapper package — `Agent(BaseAgent)` with composable capabilities, preset materialization, CLI, and public re-exports. The public SDK doorway `lingtai_sdk` may lazily import wrapper symbols such as `Agent`; the wrapper must not depend on the SDK.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | Role |
|---|---|
| `__init__.py` | Public API facade — re-exports `Agent`, `BaseAgent`, `Message`, services from kernel+wrapper |
| `__main__.py` | `python -m lingtai` → `cli.main()` |
| `agent.py` | **THE key file.** `Agent(BaseAgent)` — layer-2 agent with capability composition, preset swap, MCP, init.json refresh |
| `cli.py` | Compatibility shim — re-exports the host surface (`load_init`/`build_agent`/`run`/`main`, plus the patchable `LLMService`/`Agent`/`FilesystemMailService` names) from `lingtai_cli.host`. The `lingtai-agent` console script still points here. CLI host behaviour moved to the `lingtai_cli` package |
| `guard_wiring.py` | Advisory-first wrapper wiring of the SDK guard bridge — installs an advisory `ToolCallGuard` built from declared bundle manifests onto the Stage-16 `BaseAgent._tool_call_guard` seam. Stage 19 adds provenance tracking so a re-wire with no manifests resets only a wrapper-installed guard (never a host/manual one). Stage 3K now uses `lingtai_sdk.bundle_registry.default_registry()` as the default live declared-set source: all declared caution/destructive SDK bundle tools warn in advisory mode, safe tools pass through cleanly, and unknown/unmanifested tools remain fail-open. `core_manifest_registry` / `collect_core_bundle_manifests` remain compatibility views for explicit core-only callers. |
| `network.py` | Read-only network topology crawler — avatar/contact/mail edge discovery |
| `presets.py` | Compatibility shim re-exporting the kernel preset library (`lingtai_kernel.presets`) |
| `init_schema.py` | `validate_init()` plus `strip_deprecated()` — strict schema for active init.json fields, simple deprecated-field cleanup, and known-but-inactive legacy fields migrated by `lingtai_kernel.migrate` |
| `venv_resolve.py` | Python venv resolution — init.json → global runtime → auto-create |
| `intrinsic_skills/__init__.py` | Standalone skill bundles (docs-only) copied into `.library/intrinsic/` |
| `mcp_servers/` | Curated MCP server implementations shipped in the `lingtai` distribution and launched by `mcp_catalog.json` via `python -m lingtai.mcp_servers.<name>`; historical top-level `lingtai_*` packages remain thin wrappers |

### Key functions / classes

**`agent.py`** — `Agent(BaseAgent)`: `__init__` :33 (accept `capabilities=` + `disable=`, expand groups, `apply_core_defaults`, decompress addons, setup caps, install manuals, load MCP) · `_setup_capability` :152 · `_persist_llm_config` :127 · `_install_intrinsic_manuals` :174 · `_load_mcp_from_workdir` :376 (also tracks specs in `_mcp_init_specs`) · `_retry_failed_mcps` :524 (re-spawn dead MCPs on `system(refresh)` — issue #34) · `_read_init` :833 (runs `lingtai_kernel.migrate.run_agent_migrations()` before reading `init.json`, then materializes preset, strips plain deprecated fields, validates, resolves paths, and publishes the resolved manifest to `system/manifest.resolved.json` via `lingtai_kernel.workdir.write_resolved_manifest` — issue #259) · `_setup_from_init` :989 (**full reconstruct** — shared by boot and live refresh; reads `manifest.disable` and re-applies `apply_core_defaults`) · `_activate_preset` :915 (runtime swap, atomic write) · `_reload_prompt_sections` :1229 (writes `covenant`/`substrate`/`rules`/`principle`/`procedures`/`brief`/`comment`; delegates `character` to `_lingtai_load` and `pad` to `_pad_load` — the canonical composers — so boot/refresh/molt are consistent and hook-order-independent) · `connect_mcp` :704 / `connect_mcp_http` :756 · `start` :697 / `stop` :802

**`cli.py`**: compatibility re-export shim (`cli.py:1-44`); implementation lives in `lingtai_cli.host` (`load_init` :35 · `build_agent` :91 · `run` :216 · `main` :301). See `../lingtai_cli/ANATOMY.md`.

**`guard_wiring.py`** — `DEFAULT_LIVE_GUARD_MODE` (advisory) · `CORE_BUNDLE_NAMES` · `default_manifest_registry` (empty compatibility capability registry) · `core_manifest_registry` / `collect_core_bundle_manifests` (compatibility core-only view backed by the Stage-3K canonical registry) · `collect_agent_bundle_manifests` (walk a caller-supplied `_capabilities` registry, fail-open per provider) · `collect_default_bundle_manifests` (full `lingtai_sdk.bundle_registry.default_registry().manifests()` fail-open default) · `install_bundle_guard` (build advisory `ToolCallGuard` via `lingtai_sdk.guard_bridge.tool_call_guard_from_manifests`, write to `_tool_call_guard`, tag provenance `PROVENANCE_FLAG`/`PROVENANCE_SOURCE` when manifests present) · `reset_bundle_guard` (restore a default pass-through `ToolCallGuard` and clear provenance) · `wire_agent_guard` (live entry point; default path installs advisory guard from the full canonical declared set; caller-supplied registry path may still add core manifests with `include_core=True`; `include_core=False` with the empty registry recovers pure pass-through; skips host/manual guards and fail-opens on errors). Invoked by `Agent._wire_bundle_guard` near the end of `__init__` and `_setup_from_init`.

**`presets.py`**: compatibility re-export shim (`presets.py:1-21`); implementation lives in `lingtai_kernel.presets` (`load_preset` :174 · `materialize_active_preset` :289 · `expand_inherit` :503 · `discover_presets_in_dirs` :121).

**`init_schema.py`**: `DEPRECATED_TOP_FIELDS` :28 (plain deprecated top-level fields stripped by `strip_deprecated`), `LEGACY_MIGRATED_TOP_FIELDS` :38 (legacy fields removed by version-controlled agent migrations and known only as inactive schema), `validate_init` :94, `TOP_OPTIONAL` :13, `MANIFEST_OPTIONAL` :55; `manifest.llm.compact_threshold` is validated as positive int or null in the LLM block.

**`network.py`**: `build_network` :306 · `_discover_agents` :143 · `_build_avatar_edges` :168

**`venv_resolve.py`**: `resolve_venv` :19 · `venv_python` :40 · `ensure_package` :94

> Config-resolution helpers (`load_jsonc`/`resolve_env`/`resolve_paths`/`_resolve_capabilities`) and preset-connectivity probing (`check_connectivity`/`check_many`) live in the kernel — import directly from `lingtai_kernel.config_resolve` / `lingtai_kernel.preset_connectivity`. The former wrapper-side compatibility shims were removed (no back-compat shims per repo policy).

## Connections

**Inbound:** `lingtai-tui` calls `cli.run()` to boot agents; imports `load_preset`, `discover_presets_in_dirs` for UI. Kernel's `BaseAgent` is the parent class. `lingtai_sdk` may lazily import this wrapper as a public SDK convenience path.

**Outbound — kernel:** `lingtai_kernel.base_agent.BaseAgent`, `.config.AgentConfig`, `.prompt.build_system_prompt`, `.handshake.resolve_address`, `.intrinsics.{email,psyche}`, `.services.mail.FilesystemMailService`, `.migrate.run_migrations` (preset libraries) and `.migrate.run_agent_migrations` (agent workdir/init migrations; see `../lingtai_kernel/migrate/ANATOMY.md`).

**Cross-module:** `agent.py` → `capabilities.setup_capability`, `core.mcp.{decompress_addons,read_registry,MCPInboxPoller}`, `services.mcp.{MCPClient,HTTPMCPClient}`, `llm.service.LLMService`, `presets`, `lingtai_kernel.config_resolve`, `init_schema`, `guard_wiring.wire_agent_guard`. `cli.py` re-exports `lingtai_cli.host` (the host, which → `agent.Agent`, `lingtai_kernel.config_resolve`, `presets`).

**Outbound — SDK (wrapper → SDK edge, allowed):** `guard_wiring.py` → `lingtai_sdk.bundle_registry.default_registry`, `lingtai_sdk.guard_bridge.{GuardPolicyMode, tool_call_guard_from_manifests}`, and `lingtai_sdk.capabilities.BundleManifest`. The wrapper may depend on the SDK; the kernel must never import the SDK, so the guard chain is built wrapper-side and installed onto the kernel's `_tool_call_guard` seam — no `lingtai_kernel → lingtai_sdk` inversion. `guard_wiring.py` also imports `lingtai_kernel.tool_call_guard.ToolCallGuard` (wrapper → kernel, allowed) to construct the default pass-through reset guard.

**Agent → BaseAgent:** Three-layer hierarchy: `BaseAgent` (kernel) → `Agent` (capabilities) → `CustomAgent` (domain). Agent adds capability registration, MCP auto-loading, preset swap, full init.json reconstruct.

**Capability registration:** `setup_capability()` in `capabilities/__init__.py`; the registry is `_BUILTIN` (per-capability module paths) plus `CORE_DEFAULTS` (which boot automatically). Agent calls `apply_core_defaults` + `_setup_capability` (agent.py:152) during `__init__` and `_setup_from_init`. Hosts disable defaults via the `disable=[...]` kwarg or `manifest.disable` in init.json.

**Agent init migration + preset materialization:** `run_agent_migrations` (`lingtai_kernel/migrate/migrate.py:285`) is called by `cli.load_init` (boot) and `Agent._read_init` (refresh) before `init.json` is read/validated. Then `materialize_active_preset` (`lingtai_kernel/presets.py:289`) reads `manifest.preset.active`, loads preset, substitutes `llm`+`capabilities` into manifest before validation. The preset owns explicit opt-in capabilities, but per-agent init.json kwargs survive in two ways: (1) for capabilities the preset *also* enables, init.json wins key-by-key; (2) for always-on `CORE_DEFAULTS` capabilities the preset *omits* (daemon, bash, knowledge, …), init.json kwargs are carried forward so `apply_core_defaults` doesn't re-add an empty entry and lose e.g. `daemon.max_emanations`. Non-core optional caps the preset omits are dropped (the swap). `CORE_DEFAULTS` lives in `lingtai.capabilities` and is injected via the `core_defaults=` arg by both callers (`agent._read_init` :866, `lingtai_cli.host.load_init` :63) — the kernel does not import the wrapper. `skills.paths` additionally append-merges (preset defaults first).

## Composition

Parent: `src/lingtai/` under `lingtai-kernel/src/` alongside `lingtai_kernel/` (kernel package). Siblings: `capabilities/`, `core/`, `llm/`, `services/`, `auth/`, `i18n/`. See `../ANATOMY.md`.

## State

| Path | When | What |
|---|---|---|
| `<workdir>/init.json` | `_activate_preset` :915, `lingtai_cli.host.run` :216, `_read_init` :833 | Preset swap (atomic write); venv_path writeback; boot/refresh cleanup of deprecated top-level fields and archive-preserving init migrations |
| `<workdir>/system/llm.json` | `_persist_llm_config` :127 | LLM provider/model/base_url for revive |
| `<workdir>/system/manifest.resolved.json` | `_read_init` :833 via `lingtai_kernel.workdir.write_resolved_manifest` | Derived runtime artifact (issue #259): fully-resolved manifest (preset materialized, validated, paths resolved) with secret-bearing keys removed, plus `schema`/`generated_at`/`source`/`preset` metadata. Atomic write, regenerated on every boot/refresh/molt-reload; init.json is never written back. |
| `<workdir>/system/{covenant,principle,substrate,procedures,brief,rules,pad,lingtai}.md` + `pad_append.json` | `_reload_prompt_sections` :1229 | Prompt sections from init.json + disk. `covenant.md`→`covenant`, `lingtai.md`→`character` (via `_lingtai_load`), `pad.md`+`pad_append.json`→`pad` (via `_pad_load`). `character` is the agent's self-authored identity (灵台), distinct from `covenant` (operator contract) and the mechanical `identity` section. `substrate` is kernel-owned, cross-app stable (issue #39); kept compact and routed to the packaged `system-manual` skill for expanded operating guidance; auto-seeded from packaged `lingtai/prompts/substrate.md` on first boot if neither `data["substrate"]` nor `system/substrate.md` provides content — see `_setup_from_init` :989. |
| `<workdir>/.library/intrinsic/` | `_install_intrinsic_manuals` :174 | Wipe-and-rewrite every boot |
| `<workdir>/.agent.json` | `_build_manifest` :262 via `_workdir.write_manifest` | Runtime manifest snapshot. Includes sanitized `llm` (provider/model/base_url) from the live LLMService and `preset` (active/default/allowed) read from `init.json` by `_read_preset_from_init` :300 — see issue #78. |
| `<workdir>/.mcp_inbox/` | MCPInboxPoller (started at :701) | LICC events from out-of-process MCPs |

## Notes

- **Boot vs refresh share one code path:** `lingtai_cli.host.build_agent` :91 constructs minimal `Agent`, calls `_setup_from_init()` :989. Live refresh re-enters the same method.
- **Migration discipline:** `lingtai_kernel.migrate` is the kernel's version-controlled migration system for both preset-library and agent-workdir domains (`../lingtai_kernel/migrate/ANATOMY.md:5`). If an `init.json` or other workdir-local on-disk shape changes, add an agent-domain migration there and call/keep `run_agent_migrations()` before validation; do not add ad-hoc one-off helpers in `Agent._read_init()`. Use `init_schema.strip_deprecated()` only for simple discard-only fields that do not need archive/event/version tracking (`init_schema.py:82`). Keep migration tests in `tests/test_kernel_migrate.py` plus boot/refresh coverage in `tests/test_deep_refresh.py` / `tests/test_cli.py`, and update anatomy in the same PR.
- **`materialize_active_preset` is pure dict mutation** — disk write only in `_activate_preset` :915 (atomic `.tmp` → replace).
- **Preset implementation moved to kernel** — wrapper `presets.py` re-exports `lingtai_kernel.presets`; preset validation normalizes legacy shapes via kernel migrations before type-checking.
- **Sensitive key stripping (capabilities):** `_build_manifest` :262 strips `api_key`, `api_key_env`, `api_secret`, `token`, `password` (`_SENSITIVE_KEYS`) from capability kwargs before writing `.agent.json`.
- **LLM / preset safelists (issue #78):** `_build_manifest` :262 also re-applies `_LLM_PUBLIC_KEYS = ("provider", "model", "base_url", "api_compat", "context_limit")` to the kernel-supplied `llm` block as defense-in-depth, and reads `manifest.preset` from init.json via `_read_preset_from_init` :300 filtered to `_PRESET_PUBLIC_KEYS = ("active", "default", "allowed")`. Anything outside the safelists never reaches `.agent.json` or the identity prompt section. This is the central safety claim of #78 — see `tests/test_agent_preset_manifest.py::test_manifest_never_contains_api_key`.
- **Drift hazard:** `_setup_from_init` :989 manually reconstructs `AgentConfig` with inline defaults that MUST mirror `lingtai_kernel.config.AgentConfig` (inline construction in `_setup_from_init`, nothing enforces).
- **Addon decompression** runs BEFORE capability setup so `mcp` capability sees populated `mcp_registry.jsonl` on first reconcile (`Agent.__init__` :33, `_setup_from_init` :989).
- **MCP retry contract (issue #34):** `_load_mcp_from_workdir` :376 records every registered init.json mcp entry into `self._mcp_init_specs` (name → `{cfg, source, client}`). `_retry_failed_mcps` :524 walks this dict, closes any dead client (`is_connected()` False), respawns with the original config, and reports `{retried, recovered, still_failed, healthy}`. `system(action="refresh")` calls it via `intrinsics/system/preset.py:_refresh` before `_perform_refresh` so the documented "fix config → refresh" recovery path works without full process restart.
