# lingtai-search-sidecar

Rust sidecar that backs LingTai's `glob` and `grep` ops behind the
`FileIOBackend` seam in `src/lingtai/services/file_io.py`. Internally it
uses the ripgrep stack (`ignore::WalkBuilder` for traversal, `globset`
for glob matching, `grep-regex` + `grep-searcher` for the regex scan) so
the engineering work that makes `rg` fast carries through.

## How it ships

Starting with this PR, the sidecar is the **formal native backend** for
`pip install lingtai`:

* Building a wheel runs `cargo build --release` via `setup.py`, copies
  the resulting binary into `src/lingtai/bin/lingtai-search-sidecar`,
  and marks the wheel platform-specific.
* On import, `lingtai.services.file_io_sidecar.resolve_sidecar_binary()`
  finds that bundled binary via `importlib.resources` — users don't need
  Rust at runtime, and don't need to set any env var.
* The pure-Python `LocalFileIOBackend` remains in the codebase as an
  internal fallback for environments where no binary is available
  (sdist-only builds, machines without Rust at build time, operators who
  explicitly opt out).

### Wheel CI matrix

This PR adds `.github/workflows/wheels.yml`, a `cibuildwheel` matrix for
macOS, Linux, and Windows. Release builds install Rust in CI and set
`LINGTAI_REQUIRE_RUST_BUILD=1`, so missing or failed sidecar builds are
caught before artifacts are published. The resulting wheels carry the
native sidecar binary for their target platform, so ordinary users do not
need Rust at runtime.

Source installs (`pip install lingtai` resolving to sdist on a machine
without a matching prebuilt wheel) still keep the soft fallback behavior:
if Cargo/Rust is present the sidecar is built locally; if Rust is absent
the install succeeds without the bundled binary and `auto` falls back to
pure Python with a warning.

Setting `LINGTAI_SKIP_RUST_BUILD=1` forces a pure-Python build (no cargo
invocation, universal wheel). Set `LINGTAI_REQUIRE_RUST_BUILD=1` to make
any cargo failure abort the build instead of fallback.

## Build manually

```bash
cd crates/lingtai-search-sidecar
cargo build --release
export LINGTAI_FILE_IO_SIDECAR="$(pwd)/target/release/lingtai-search-sidecar"
```

The `setup.py` build hook automates this for wheel builds. Editable
installs (`pip install -e .`) skip the cargo step and rely on the dev-
tree binary discovery (see below) once you've run `cargo build` once.

## Runtime selection

Two env vars control which backend `lingtai` agents actually use:

### `LINGTAI_FILE_IO_BACKEND` — picks the backend

| Value | Behavior |
|---|---|
| `auto` (default, or unset) | Use the Rust sidecar if a binary can be resolved; silently fall back to pure Python otherwise. |
| `rust` | Require the Rust sidecar. Raise `SidecarError(code="not_configured")` if no binary is available — the operator asked for the native path explicitly. |
| `python` | Force the pure-Python `LocalFileIOBackend`. Useful for debugging or environments where the sidecar misbehaves. |

### `LINGTAI_FILE_IO_SIDECAR` (and legacy `LINGTAI_SEARCH_SIDECAR`) — overrides the binary path

When set, points at the sidecar binary directly. Highest priority in the
resolver, so operators can pin a specific build without touching the
package.

### Resolver priority

`resolve_sidecar_binary()` walks these sources in order and returns the
first match:

1. **Explicit `binary_path=`** argument to `SidecarAdapter` /
   `RustFileIOBackend`.
2. **`LINGTAI_FILE_IO_SIDECAR`** env var (canonical name).
3. **`LINGTAI_SEARCH_SIDECAR`** env var (legacy alias).
4. **Packaged binary** at `lingtai/bin/lingtai-search-sidecar[.exe]`
   — present in platform-specific wheels.
5. **Dev-tree binary** at `crates/lingtai-search-sidecar/target/
   {release,debug}/lingtai-search-sidecar[.exe]` — found automatically
   from editable installs after `cargo build`.

If all five miss, `default_file_io_service` honors
`LINGTAI_FILE_IO_BACKEND`: `auto` quietly hands out the pure-Python
backend; `rust` raises.

## API surface

```python
from lingtai.services.file_io_sidecar import (
    default_file_io_service,    # factory used by Agent's auto-create path
    resolve_sidecar_binary,     # full resolver, callable from host code
    RustFileIOBackend,          # explicit opt-in (strict, no fallback)
    SidecarAdapter,             # one subprocess per call() — JSON in/out
    SidecarError,               # raised when the sidecar can't satisfy a call
    BACKEND_ENV_VAR,            # "LINGTAI_FILE_IO_BACKEND"
)

# Default path used by Agent.__init__ — picks the best backend automatically.
svc = default_file_io_service(root=working_dir)
```

`Agent.__init__` calls `default_file_io_service` for any agent that
doesn't pass an explicit `file_io=` to the constructor. Hosts that want
finer control can construct `RustFileIOBackend(...)` (strict; raises on
sidecar errors) or `LocalFileIOService(root=...)` (pure Python)
directly.

## Protocol

Speak JSON on stdin, get JSON on stdout. One request per process — the
binary is intentionally a short-lived child process, never a daemon.

Request:

```json
{
  "op": "grep",
  "root": "/abs/sandbox/root",
  "path": "/abs/sandbox/root/sub",
  "pattern": "needle",
  "max_results": 50,
  "max_visited": 20000,
  "walltime_ms": 8000,
  "max_file_bytes": 4194304,
  "exclude_dirs": [".git", "node_modules"]
}
```

Response (always, even on error — but exit code is `2` when `ok: false`):

```json
{
  "ok": true,
  "backend": "lingtai-search-sidecar",
  "op": "grep",
  "matches": [{"path": "sub/a.py", "line_number": 2, "line": "needle one"}],
  "paths": [],
  "visited": 12,
  "files_skipped_size": 0,
  "files_skipped_binary": 1,
  "dirs_pruned": 0,
  "elapsed_ms": 3,
  "truncated_reason": null,
  "error": null
}
```

For `op: "glob"` the `paths` field carries absolute paths (mirrors
`LocalFileIOBackend.glob`) and `matches` is empty. The sidecar applies the
same default-exclude / walltime / visited / file-size budgets as the Python
backend; the adapter passes the active values explicitly so the two stay
in lock-step.
