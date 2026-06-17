# src/lingtai_sdk/

Batteries-included tool/capability substrate — the third importable package, between the minimal `lingtai_kernel` runtime and the `lingtai` product/CLI wrapper. Owns the capability *registry seam* and the tool/service implementations that depend only on the kernel.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | Role |
|---|---|
| `__init__.py` | Package docstring + the one-directional dependency rule (`lingtai_kernel ← lingtai_sdk ← lingtai`) |
| `capabilities/__init__.py` | **The seam.** Static registry (`_BUILTIN`, `_GROUPS`, `CORE_DEFAULTS`), normalization (`normalize_capabilities`, `apply_core_defaults`, `expand_groups`), `setup_capability()`, `get_all_providers()` |
| `capabilities/file/{read,write,edit,glob,grep}.py` | File-I/O tool capabilities — each exports `get_schema`/`get_description`/`setup`; operate on `agent._file_io` (a `FileIOService`) |
| `services/file_io.py` | `FileIOService`/`FileIOBackend` ABCs, `LocalFileIOService` facade, `LocalFileIOBackend` (pure-Python local FS), `GrepMatch`, `TraversalStats`, traversal-budget `DEFAULT_*` constants |
| `services/file_io_sidecar.py` | Rust-backed grep/glob backend: `RustFileIOBackend`, `SidecarAdapter`, `SidecarError`, `resolve_sidecar_binary`, `default_file_io_service` (the factory `Agent.__init__` calls) |
| `services/mcp.py` | Generic MCP transport clients: `MCPClient` (stdio subprocess) + `HTTPMCPClient` (streamable HTTP/SSE) — async-to-sync bridges with a shared synchronous `call_tool()` API. Owns the MCP *transport* seam only; the catalog, registry, inbox poller, and `agent.py` loaders stay under `lingtai` |

## Connections

- **→ `lingtai_kernel`** — capability `setup()` functions type against `lingtai_kernel.base_agent.BaseAgent`; `services/mcp.py` uses `lingtai_kernel.logging.get_logger`. This is the only allowed outward dependency direction.
- **→ `mcp.client.{stdio,streamable_http,session}`** — `services/mcp.py` imports the third-party MCP SDK lazily inside its async connect methods.
- **→ `lingtai.i18n`** — the file capabilities pull translation strings (`read.description`, …) from `lingtai.i18n.t` (see Notes — a follow-up should give the SDK its own strings).
- **→ `lingtai/bin/`** — `file_io_sidecar._packaged_binary` resolves the bundled Rust binary via `files("lingtai").joinpath("bin", ...)`; the binary deliberately stays under `lingtai/bin/` for this slice.
- **← `lingtai.capabilities`** — re-export shim; every `from lingtai.capabilities import setup_capability/_BUILTIN/CORE_DEFAULTS/...` resolves here.
- **← `lingtai.services.file_io` / `lingtai.services.file_io_sidecar` / `lingtai.services.mcp`** — `sys.modules` aliases (module identity preserved) pointing at `services/file_io.py` / `file_io_sidecar.py` / `mcp.py` here.
- **← `lingtai.agent` / `lingtai.core.daemon` / `lingtai.services.{vision,websearch}.*` / `lingtai.llm.minimax.mcp_client`** — import `MCPClient`/`HTTPMCPClient` via the `lingtai.services.mcp` alias (absolute or `...services.mcp` relative); both resolve here.
- **← `lingtai.core.{read,write,edit,glob,grep}`** — `sys.modules` aliases pointing at `capabilities/file/*.py` here.
- **← `lingtai.agent.Agent`** — `__init__` auto-creates a FileIO service via `default_file_io_service` (through the `lingtai.services.file_io_sidecar` alias) and routes capability setup through `setup_capability`.

## Composition

`capabilities/__init__.py` is the entry point — one uniform `name → absolute-module-path` map loaded through a single `setup(agent, **kwargs)` contract. Entries are absolute paths so resolution is import-location independent: file tools point at `lingtai_sdk.capabilities.file.*`; the always-on floor (`knowledge`/`skills`/`bash`/`avatar`/`daemon`/`mcp`) and the optional `vision`/`web_search` still point at `lingtai.*` until later SDK slices move them. The `file/` capabilities are thin tool handlers over `services.file_io`: `LocalFileIOService` is the tool-facing facade over the default Python `LocalFileIOBackend`; `RustFileIOBackend` is the opt-in alternative that delegates read/write/edit to a private `LocalFileIOBackend` and routes grep/glob to a short-lived Rust subprocess. `services/mcp.py` is a standalone transport layer — two parallel client classes (`MCPClient`/`HTTPMCPClient`) sharing the same async-loop-on-a-daemon-thread pattern; it is independent of the capability registry and consumed directly by `lingtai` callers via the `lingtai.services.mcp` alias.

## State

- The registry (`_BUILTIN`, `_GROUPS`, `CORE_DEFAULTS`) is static module-level data; no mutable runtime state.
- `LocalFileIOService` / `LocalFileIOBackend` hold an optional `_root` plus `last_traversal` (a `TraversalStats` surfaced to glob/grep tool metadata).
- `RustFileIOBackend` holds an embedded `LocalFileIOBackend`, a `SidecarAdapter`, and a `last_traversal` rebuilt per sidecar envelope. `SidecarAdapter` is stateless apart from the resolved binary path (one subprocess per `call()`).
- `MCPClient` / `HTTPMCPClient` each manage a background daemon thread, an asyncio event loop (`_loop`), a `ClientSession` (`_session`), and a 50-entry activity log; thread-safe via `threading.Lock` / `threading.Event`. Lazy start (auto-connect on first `call_tool()`). `MCPClient` has stale-resource recovery (issue #104): it detects a dead stdio transport in `call_tool`, `restart()`s, and retries once. Tests: `tests/test_mcp_closed_resource_restart.py`.

## Notes

- **One-directional rule:** `lingtai_kernel` must never import `lingtai_sdk` (mirrors the kernel/wrapper rule). `lingtai_sdk` → `lingtai_kernel` is allowed; `lingtai` → `lingtai_sdk` is the wrapper re-export direction.
- **i18n follow-up:** file capabilities currently import `lingtai.i18n.t`. That is an SDK→wrapper edge for translation data only (not the protected kernel rule), kept to avoid moving i18n in this slice. A later slice should give the SDK its own strings or move shared strings to the kernel.
- **Sidecar binary stays put (SDK-02 decision):** only the adapter code moved; the wheel-bundled binary remains at `lingtai/bin/` so `setup.py`/`wheels.yml` need no changes. Relocating the binary is a deferred follow-up.
- **Distribution:** `lingtai_sdk` is a new top-level package *inside* the existing `lingtai` wheel (added to `[tool.setuptools.packages.find].include`). PyPI name stays `lingtai`; whether the SDK ever becomes a separate wheel is a deferred decision.
- **Compatibility is shim-based, not copy-based:** old paths alias the SDK modules via `sys.modules`, preserving module identity so `monkeypatch.setattr(lingtai.services.file_io_sidecar, "_packaged_binary", ...)` reaches the resolver's real globals. Pinned by `tests/test_sdk_namespace_fileio.py` and `tests/test_sdk_namespace_mcp.py`.
- **MCP transport-only move (SDK-MCP-01):** only the generic `MCPClient`/`HTTPMCPClient` transport bridges moved here. The MCP catalog (`lingtai/mcp_catalog.json`), bundled `lingtai/mcp_servers/*`, the `lingtai/core/mcp/*` registry + LICC inbox poller, and `agent.py`'s `connect_mcp`/`connect_mcp_http` loaders deliberately stay under `lingtai` — relocating them is a deferred follow-up.
