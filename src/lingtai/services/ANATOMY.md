# src/lingtai/services/

Root services package — pluggable backends for intrinsic tools and MCP clients.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|---|---|---|
| `__init__.py` | 1 | Docstring-only package marker |
| `file_io.py` | ~22 | **Shim (SDK-02).** Aliases `lingtai_sdk.services.file_io` into `sys.modules` under this name — implementation moved to the SDK; module identity preserved for monkeypatch-based callers |
| `file_io_sidecar.py` | ~25 | **Shim (SDK-02).** Aliases `lingtai_sdk.services.file_io_sidecar` into `sys.modules` under this name. The packaged Rust binary stays under `lingtai/bin/` for this slice |
| `mail.py` | 4 | Re-exports `MailService`, `FilesystemMailService` from `lingtai_kernel.services.mail` |
| `mcp.py` | ~26 | **Shim (SDK-MCP-01).** Aliases `lingtai_sdk.services.mcp` into `sys.modules` under this name — the `MCPClient` (stdio) + `HTTPMCPClient` (streamable HTTP) transport bridges moved to the SDK; module identity preserved for absolute/relative importers and monkeypatch-based callers |

**SDK-owned (moved):** the FileIO implementation now lives in `src/lingtai_sdk/services/file_io.py` (`FileIOService`/`FileIOBackend`/`LocalFileIOBackend`/`LocalFileIOService`) and `file_io_sidecar.py` (`RustFileIOBackend`, `SidecarAdapter`, `SidecarError`, `resolve_sidecar_binary`, `default_file_io_service`); the MCP transport clients now live in `src/lingtai_sdk/services/mcp.py` (`MCPClient`, `HTTPMCPClient`). See `../../lingtai_sdk/ANATOMY.md`. The three files here (`file_io.py`, `file_io_sidecar.py`, `mcp.py`) are thin `sys.modules` aliases that keep `from lingtai.services.{file_io,mcp} import ...`, `Agent.__init__`'s `default_file_io_service` import, and `Agent.connect_mcp`/`connect_mcp_http` working unchanged.

**Sub-packages (not covered here):** `vision/` (7 provider files), `websearch/` (6 provider files).
**Sibling crates:** `experimental/lingtai-search-sidecar/` (Rust) — opt-in binary that backs `RustFileIOBackend`. Not required for install/tests.

## Connections

- **→ `lingtai_kernel.services.mail`** (mail.py:2) — pure re-export of kernel mail types.
- **← `lingtai.capabilities.vision`** — uses `services.vision.VisionService`.
- **← `lingtai.capabilities.web_search`** — uses `services.websearch.SearchService`.
- **← `lingtai_sdk.capabilities.file.*`** — read/write/edit/glob/grep use `FileIOService` (now resolved through `lingtai_sdk.services.file_io`; the `lingtai.services.file_io` shim points at the same module).
- **← `lingtai.agent` / `lingtai.core.daemon` / `services.vision.*` / `services.websearch.*` / `llm.minimax.mcp_client`** — import `MCPClient`/`HTTPMCPClient` via `lingtai.services.mcp` (absolute) or `...services.mcp` (relative); both resolve to the SDK module the `mcp.py` shim aliases.

## Composition

The FileIO abstraction layer now lives in `lingtai_sdk.services` (SDK-02): `file_io.py` is a pure stdlib abstraction (`LocalFileIOService` facade over the default Python `LocalFileIOBackend`); `file_io_sidecar.py` provides the opt-in `RustFileIOBackend` that delegates `read`/`write`/`edit` to a private `LocalFileIOBackend` but routes `grep`/`glob` to the Rust binary under `experimental/lingtai-search-sidecar/` via short-lived JSON subprocess calls. The file-I/O and `mcp.py` files in *this* package are `sys.modules` aliases to the SDK modules (module identity preserved). `mail.py` is a passthrough re-export. The MCP transport client implementation (two parallel client classes sharing the same pattern) now lives in `lingtai_sdk.services.mcp` — see `../../lingtai_sdk/ANATOMY.md`.

## State

- **`MCPClient` / `HTTPMCPClient`** (now in `lingtai_sdk.services.mcp`): each instance manages a background daemon thread, an asyncio event loop (`_loop`), a `ClientSession` (`_session`), and a 50-entry activity log (`_activity_log`). Thread-safe via `threading.Lock` and `threading.Event`. Behavior is unchanged; only the import home moved — see `../../lingtai_sdk/ANATOMY.md`.
- **`LocalFileIOService`**: facade over a `_backend`; exposes `last_traversal` from the backend for tool metadata.
- **`LocalFileIOBackend`**: default Python local filesystem backend; state is optional `_root` plus `last_traversal`.
- **`RustFileIOBackend`**: holds an embedded `LocalFileIOBackend` (for read/write/edit), a `SidecarAdapter` (subprocess client), and a `last_traversal` rebuilt from each sidecar envelope.
- **`SidecarAdapter`**: stateless apart from the resolved binary path; one subprocess per `call()`.
- **`FileIOService` / `FileIOBackend` ABCs**: pure interfaces, no state.

## Notes

- The MCP transport-client behavior below describes the implementation now hosted in `lingtai_sdk.services.mcp` (SDK-MCP-01) — the `mcp.py` here is a `sys.modules` alias, behavior unchanged. See `../../lingtai_sdk/ANATOMY.md`.
- `MCPClient` uses `stdio_client` transport (subprocess); `HTTPMCPClient` uses `streamablehttp_client` (remote HTTP/SSE). Both expose identical `call_tool()` / `list_tools()` / `close()` API.
- Lazy start: both clients auto-connect on first `call_tool()`.
- **Stale-resource recovery (issue #104):** `MCPClient` detects a dead stdio transport in `call_tool` and recovers. `_format_exception` renders `ClassName: message` (class-only when `str(e)` is empty) so an empty `ClosedResourceError` never surfaces as a blank `{"status":"error","message":""}`. `_is_stale_resource_error` flags closed/broken transports by class name + message substrings. On a stale error `call_tool` calls `restart()` (which `close()`s, clears `_ready`/`_error`, resets `_closed`/`_session`/`_loop`/`_thread`/`*_cm` so `start()` cannot lie) and retries **once**; a failed retry returns a helpful error naming the class and the retry failure. Non-stale errors surface the class name without churning the subprocess. `HTTPMCPClient` reuses `MCPClient._format_exception` for its connect error only — it has no stale-resource restart (stdio is the reported transport). Tests: `tests/test_mcp_closed_resource_restart.py`.
- The transport-client module has significant code duplication between the two classes — same `call_tool()`, `list_tools()`, `_run_loop()`, `_async_cleanup()` pattern.
- `mail.py` is a thin shim — the real implementation lives in `lingtai_kernel.services.mail`.
- The FileIO class-level State above (`FileIOService`/`LocalFileIOService`/`LocalFileIOBackend`/`RustFileIOBackend`/`SidecarAdapter`) describes the implementations now hosted in `lingtai_sdk.services` — see `../../lingtai_sdk/ANATOMY.md`. Behavior is unchanged; only the import home moved.
- `file_io_sidecar.py` is the **default native backend** for `Agent`-created file-I/O services. `default_file_io_service` is the factory that `Agent.__init__` calls; it consults `LINGTAI_FILE_IO_BACKEND` (`auto` / `rust` / `python`, default `auto`) and `resolve_sidecar_binary` to pick between Rust and the pure-Python `LocalFileIOBackend`. Resolver priority: explicit `binary_path=` > `LINGTAI_FILE_IO_SIDECAR` env > `LINGTAI_SEARCH_SIDECAR` (legacy) env > packaged `lingtai/bin/` binary (shipped in platform-specific wheels by `setup.py`) > dev-tree `experimental/lingtai-search-sidecar/target/{release,debug}/`. The strict `SidecarAdapter()` constructor still ignores packaged / dev-tree sources — opt-in callers see `not_configured` rather than picking up a stale binary. Defaults (`DEFAULT_*` constants) are imported from `file_io.py` so both backends stay in lock-step. Cargo is **not** required for install or the normal test suite — tests use a Python-script "sidecar"; only `test_rust_sidecar_integration_grep_and_glob` is cargo-gated.
