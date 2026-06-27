# src/lingtai/services/

Root services package — pluggable backends for intrinsic tools and MCP clients.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

| File | LOC | Role |
|---|---|---|
| `__init__.py` | 1 | Docstring-only package marker |
| `file_io.py` | 785 | `FileIOService` facade contract + `LocalFileIOBackend`, `NoKVFileIOBackend`, and `HybridFileIOBackend` — backs read/edit/write/glob/grep |
| `file_io_sidecar.py` | 680 | Rust-backed grep/glob: `RustFileIOBackend`, `SidecarAdapter`, `SidecarError`, plus the `resolve_sidecar_binary` resolver and the `default_file_io_service` factory used by `Agent.__init__` |
| `mail.py` | 4 | Re-exports `MailService`, `FilesystemMailService` from `lingtai_kernel.services.mail` |
| `mcp.py` | 530 | `MCPClient` (stdio) + `HTTPMCPClient` (streamable HTTP) — async-to-sync MCP bridges |
| `nokv.py` | 123 | NoKV config/URI helpers, `NoKVUnsupportedError`, and selected-subtree classification |

**Sub-packages (not covered here):** `vision/` (7 provider files), `websearch/` (6 provider files).
**Sibling crates:** `crates/lingtai-search-sidecar/` (Rust) — opt-in binary that backs `RustFileIOBackend`. Not required for install/tests.

## Connections

- **→ `lingtai_kernel.logging.get_logger`** (mcp.py:16) — structured logging.
- **→ `lingtai_kernel.services.mail`** (mail.py:2) — pure re-export of kernel mail types.
- **→ `mcp.client.stdio`**, **`mcp.client.streamable_http`**, **`mcp.client.session`** (mcp.py:224, 406-407) — third-party MCP SDK. Imported lazily inside async connect methods.
- **← `lingtai.capabilities.vision`** — uses `services.vision.VisionService`.
- **← `lingtai.capabilities.web_search`** — uses `services.websearch.SearchService`.
- **← `lingtai.core.*`** — read/write/edit/glob/grep use `FileIOService`; `lingtai.core.nokv` uses `NoKVFileIOBackend`.
- **`file_io.py` → `nokv.py`** — `NoKVFileIOBackend` / `HybridFileIOBackend` import URI normalization and disabled-backend errors.

## Composition

`file_io.py` is a pure stdlib abstraction layer. `LocalFileIOService` is the tool-facing facade while `LocalFileIOBackend` owns the default Python local filesystem implementation. `NoKVFileIOBackend` owns explicit `nokv://` object operations (`file_io.py:402-594`), and `HybridFileIOBackend` routes ordinary paths to local storage while requiring configured NoKV for `nokv://` (`file_io.py:597-685`). `nokv.py` keeps URI/config/subtree policy outside the low-level backend (`nokv.py:13-123`). `file_io_sidecar.py` provides `RustFileIOBackend`, an opt-in alternative backend that delegates `read`/`write`/`edit` to a private `LocalFileIOBackend` but routes `grep`/`glob` to the Rust binary under `crates/lingtai-search-sidecar/` via short-lived JSON subprocess calls. `mail.py` is a passthrough re-export. `mcp.py` is the heavy module — two parallel client classes sharing the same pattern.

## State

- **`MCPClient` / `HTTPMCPClient`**: each instance manages a background daemon thread (L68, 292), an asyncio event loop (`_loop`), a `ClientSession` (`_session`), and a 50-entry activity log (`_activity_log`, L54, 286). Thread-safe via `threading.Lock` and `threading.Event`.
- **`LocalFileIOService`**: facade over a `_backend`; exposes `last_traversal` from the backend for tool metadata.
- **`LocalFileIOBackend`**: default Python local filesystem backend; state is optional `_root` plus `last_traversal`.
- **`NoKVConfig`**: immutable optional NoKV config shape; default `enabled=False` keeps runtime local (`nokv.py:43-56`).
- **`NoKVFileIOBackend`**: injected NoKV client plus URI prefixes and traversal stats; imports no NoKV SDK at module load (`file_io.py:402-418`).
- **`HybridFileIOBackend`**: local backend, optional NoKV backend, and `_last_backend` for `last_traversal` routing (`file_io.py:597-610`).
- **`RustFileIOBackend`**: holds an embedded `LocalFileIOBackend` (for read/write/edit), a `SidecarAdapter` (subprocess client), and a `last_traversal` rebuilt from each sidecar envelope.
- **`SidecarAdapter`**: stateless apart from the resolved binary path; one subprocess per `call()`.
- **`FileIOService` / `FileIOBackend` ABCs**: pure interfaces, no state.

## Notes

- `MCPClient` uses `stdio_client` transport (subprocess); `HTTPMCPClient` uses `streamablehttp_client` (remote HTTP/SSE). Both expose identical `call_tool()` / `list_tools()` / `close()` API.
- Lazy start: both clients auto-connect on first `call_tool()`.
- **Stale-resource recovery (issue #104):** `MCPClient` detects a dead stdio transport in `call_tool` and recovers. `_format_exception` renders `ClassName: message` (class-only when `str(e)` is empty) so an empty `ClosedResourceError` never surfaces as a blank `{"status":"error","message":""}`. `_is_stale_resource_error` flags closed/broken transports by class name + message substrings. On a stale error `call_tool` calls `restart()` (which `close()`s, clears `_ready`/`_error`, resets `_closed`/`_session`/`_loop`/`_thread`/`*_cm` so `start()` cannot lie) and retries **once**; a failed retry returns a helpful error naming the class and the retry failure. Non-stale errors surface the class name without churning the subprocess. `HTTPMCPClient` reuses `MCPClient._format_exception` for its connect error only — it has no stale-resource restart (stdio is the reported transport). Tests: `tests/test_mcp_closed_resource_restart.py`.
- `mcp.py` has significant code duplication between the two classes — same `call_tool()`, `list_tools()`, `_run_loop()`, `_async_cleanup()` pattern.
- `mail.py` is a thin shim — the real implementation lives in `lingtai_kernel.services.mail`.
- `file_io_sidecar.py` is the **default native backend** for `Agent`-created file-I/O services. `default_file_io_service` is the factory that `Agent.__init__` calls; it consults `LINGTAI_FILE_IO_BACKEND` (`auto` / `rust` / `python`, default `auto`) and `resolve_sidecar_binary` to pick between Rust and the pure-Python `LocalFileIOBackend`. Resolver priority: explicit `binary_path=` > `LINGTAI_FILE_IO_SIDECAR` env > `LINGTAI_SEARCH_SIDECAR` (legacy) env > packaged `lingtai/bin/` binary (shipped in platform-specific wheels by `setup.py`) > dev-tree `crates/lingtai-search-sidecar/target/{release,debug}/`. The strict `SidecarAdapter()` constructor still ignores packaged / dev-tree sources — opt-in callers see `not_configured` rather than picking up a stale binary. Defaults (`DEFAULT_*` constants) are imported from `file_io.py` so both backends stay in lock-step. Cargo is **not** required for install or the normal test suite — tests use a Python-script "sidecar"; only `test_rust_sidecar_integration_grep_and_glob` is cargo-gated.
- NoKV remains explicit: ordinary local paths never route to NoKV, and `nokv://` raises `NoKVUnsupportedError` unless a NoKV backend/client is injected.
