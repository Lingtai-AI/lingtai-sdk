# File-search Rust backend contract

Status: proposal / PoC contract.

This note defines the boundary for a future native file-search backend for
LingTai's `glob`, `grep`, and related file discovery tools. It is deliberately
separate from the current pure-Python implementation so the runtime contract can
stabilize before packaging native code.

## Motivation

LingTai currently exposes cross-platform structured file primitives (`glob`,
`grep`) backed by Python traversal in `LocalFileIOService`. PR #168 improves the
baseline by pushing `grep` glob filtering before file reads, streaming matching
line-by-line, and skipping binary files via a NUL sniff.

Research into other coding harnesses shows a broader pattern:

- **oh-my-pi** embeds the ripgrep Rust stack directly (`ignore`, `globset`,
  `grep_regex`, `grep_searcher`, `rayon`) and exposes native Node tools.
- **OpenCode**, **Hermes Agent**, and **OpenClaw** delegate to `rg` / `fd`
  subprocesses for mature traversal, ignore handling, and parallel search.
- **Aider**, **Cursor**, and **Continue** combine lexical search with repo maps,
  codebase indexes, or semantic retrieval.

The common lesson is not “replace LingTai's tools with shell commands.” The
lesson is that file search should remain a small POSIX-like runtime contract
while the implementation underneath is replaceable.

## Contract framing

LingTai's runtime boundary is the tool contract, not the language used behind it.
Python is the current host/runtime layer. The stable contract is:

1. a schema the model can learn and call reliably;
2. sandboxed path resolution and filesystem safety;
3. deterministic structured output;
4. explicit truncation and skipped-work metadata;
5. stable fallback behavior when faster/native backends are unavailable.

The backend may be any of:

1. pure Python baseline;
2. managed `rg` / `fd` subprocess;
3. Rust sidecar binary/service;
4. PyO3/maturin native extension.

The model-facing tool schema should not change just because the backend changes.

## Backend operation schema

A native backend should implement small operations, not arbitrary shell access.
The Python host remains responsible for resolving the user path into an allowed
sandbox root before calling the backend.

### Common request fields

```json
{
  "op": "grep",
  "root": "/absolute/sandbox/root",
  "path": "/absolute/sandbox/root/src",
  "max_results": 100,
  "timeout_ms": 5000,
  "include_hidden": false,
  "respect_gitignore": true,
  "include_globs": ["**/*.py"],
  "exclude_globs": ["**/.venv/**", "**/node_modules/**"]
}
```

Rules:

- `root` and `path` are absolute paths already validated by Python.
- The backend must reject results outside `root` after symlink/canonicalization
  checks where possible.
- Globs are relative to `root` unless explicitly documented otherwise.
- Include/exclude patterns are compiled once per request.
- `timeout_ms` and `max_results` are hard budgets, not hints.

### `grep` request

```json
{
  "op": "grep",
  "pattern": "class Agent",
  "regex": true,
  "case_sensitive": true,
  "context_before": 0,
  "context_after": 0,
  "max_columns": 240,
  "output_mode": "content"
}
```

`output_mode` values:

- `content`: return matching lines and optional context;
- `filesWithMatches`: return each matching file once;
- `count`: return per-file and total match counts.

### `glob` / find request

```json
{
  "op": "glob",
  "pattern": "src/**/*.py",
  "file_type": "file",
  "sort": "path"
}
```

`file_type` values: `file`, `dir`, `any`.

`sort` values: `path`, `mtime_desc`, `none`.

## Response envelope

Every backend response should use one envelope shape:

```json
{
  "ok": true,
  "backend": "rust-sidecar",
  "elapsed_ms": 12,
  "matches": [],
  "files": [],
  "counts": {},
  "files_searched": 123,
  "dirs_visited": 18,
  "truncated": false,
  "truncated_reason": null,
  "next_offset": null,
  "skipped": {
    "binary": 2,
    "too_large": 1,
    "permission": 0,
    "ignored": 33,
    "unreadable": 0
  },
  "policy": {
    "include_hidden": false,
    "respect_gitignore": true,
    "binary": "skip-on-nul",
    "max_file_bytes": 1048576
  },
  "errors": []
}
```

Errors should still be structured:

```json
{
  "ok": false,
  "backend": "rust-sidecar",
  "error": {
    "code": "invalid_pattern",
    "message": "regex parse error at offset 3"
  },
  "partial": false,
  "matches": [],
  "errors": []
}
```

## Safety and policy requirements

The Python host should continue to own:

- resolving relative paths against the agent working directory;
- rejecting paths outside the configured sandbox;
- deciding whether the backend is enabled;
- falling back to Python on unavailable/failed native execution.

The backend should still enforce defense-in-depth:

- do not follow symlink escapes outside `root`;
- do not read files above `max_file_bytes` unless explicitly configured;
- skip binary files by default using a NUL sniff or equivalent;
- honor directory prune policy (`.git`, `node_modules`, `.venv`, caches);
- report skipped counters instead of silently hiding work;
- enforce time/result budgets even when traversal is slow.

## Backend ladder

Recommended staged ladder:

1. **`python-baseline`** — always available; correct, portable, conservative.
2. **`rg/fd-subprocess`** — optional acceleration where binaries are present or
   managed; use `--json`, `--no-config`, `--no-messages`, explicit globs, and
   sanitized environment.
3. **`rust-sidecar`** — optional single binary speaking JSON over stdin/stdout;
   lower Python ABI risk, slightly higher process protocol cost.
4. **`rust-native`** — PyO3/maturin extension for lowest overhead and cleanest
   embedding, but highest packaging/CI burden.

The runtime should expose which backend answered via `backend` metadata.

## Native integration shapes

### PyO3/maturin extension

Pros:

- direct Python calls;
- no subprocess protocol;
- can share typed structures with Python wrappers;
- likely best long-term latency profile.

Cons:

- wheel matrix for macOS/Linux/Windows and x86_64/aarch64;
- Python ABI/version compatibility;
- PyPI release complexity;
- local development requires Rust toolchain;
- harder to keep as a purely optional capability without careful packaging.

### Rust sidecar binary/service

Pros:

- simple process boundary and language isolation;
- can be optional and discovered via environment/path;
- no Python extension ABI concerns;
- easier to benchmark and replace independently.

Cons:

- subprocess overhead;
- need a stable JSON protocol;
- binary discovery/distribution still needs solving;
- long-running service mode would need lifecycle supervision.

## Packaging risks

Before making Rust part of the default install, answer:

- Which platforms get wheels: macOS, Linux, Windows; x86_64 and arm64/aarch64?
- Does `pip install lingtai` require Rust, or is native search an extra?
- How does CI build and test wheels?
- What happens when the native package is missing, incompatible, or crashes?
- Is the native backend versioned independently from `lingtai`?
- Can users disable native search for reproducibility/debugging?

Default recommendation: keep native search optional until the fallback path and
wheel publishing are boring.

## Benchmark and validation plan

Measure against the same fixtures:

- small repo, medium repo, monorepo-sized tree;
- many ignored directories (`node_modules`, `.venv`, `.git`);
- binary files, unreadable files, large files, invalid UTF-8;
- high-match query and no-match query;
- include/exclude glob-heavy query;
- cancellation/timeout query.

Record:

- elapsed wall time;
- files visited/searched;
- bytes read;
- matches returned;
- truncation reason;
- skipped counters;
- parity against Python baseline for representative queries.

## Staged PR plan

1. **Design contract** — this document.
2. **Minimal PoC** — optional Rust sidecar with JSON stdin/stdout and explicit
   Python adapter tests; default behavior unchanged.
3. **Optional backend selection** — environment/config flag chooses backend;
   Python fallback remains authoritative.
4. **API improvements** — relative-path globs, multiple include/exclude globs,
   context lines, output modes, pagination, and better skip metadata.
5. **Production native backend** — PyO3/maturin or managed sidecar once packaging
   and CI are solved.

## Non-goals for the first PoC

- Replacing `LocalFileIOService` by default.
- Requiring Rust to install `lingtai`.
- Matching all ripgrep features.
- Introducing arbitrary shell execution through file tools.
- Changing model-facing tool schemas before the backend contract is reviewed.
