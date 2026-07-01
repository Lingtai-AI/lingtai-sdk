---
name: bash-kimicode
description: >
  Nested bash-manual reference for Kimi Code CLI. Read this when you need to run,
  validate, or document `kimicode / kimi` as a long-running shell subprocess or
  LingTai daemon harness candidate.
version: 0.1.0
last_changed_at: "2026-07-01T00:00:00-07:00"
---

# Kimi Code CLI

Nested bash-manual reference. This page owns **shell execution hygiene** for
`kimicode / kimi`: command shape, async/poll discipline, one-shot mode, and the
session/resume caveats that keep `ask` unsupported in the daemon backend.

## Status

Use for MoonshotAI Kimi Code subprocesses (official `MoonshotAI/kimi-code`
single binary `kimi`, version observed 0.20.2). Keep provider/model credential
discovery elsewhere; this page only owns shell execution hygiene.

## Command shape

```bash
kimi --prompt '<prompt>' --output-format text
```

- Output formats are `text` or `stream-json` via `--output-format` (note: it is
  `--output-format`, not `--format`).
- Model selection is `-m/--model <model>`.
- **Do not combine `--prompt` with `--yolo`** — the CLI refuses that pairing.
- The daemon backend owns `--prompt` / `--output-format` and forbids `--yolo`;
  free-form `backend_options` are inserted before those owned flags.

Before relying on the command in production, run the current CLI's `--help` and
prefer `bash(async=true)` for work that can think, edit files, or run tools for
minutes. Do not run long coding CLIs synchronously from the parent turn.

## Environment

The daemon backend sets per run (never logging secret values):

- `KIMI_CODE_HOME` — a run-private directory so concurrent runs don't share state.
- `KIMI_DISABLE_TELEMETRY=1`, `KIMI_CODE_NO_AUTO_UPDATE=1`.
- `KIMI_MODEL_API_KEY` — mapped from the first set of `KIMICODE_API_KEY` /
  `KIMI_API_KEY` / `MOONSHOT_API_KEY`, only when not already set.
- `KIMI_MODEL_NAME` / `KIMI_MODEL_PROVIDER_TYPE` / `KIMI_MODEL_BASE_URL` /
  `KIMI_MODEL_MAX_CONTEXT_SIZE` — provider defaults, applied only when absent.

## LingTai daemon notes

- The daemon start command is deterministic and non-interactive (one-shot
  `--prompt` + `--output-format text`).
- `ask`/resume is **not supported yet**: `-S/--session` and `-c/--continue`
  exist, but a stable machine-readable session-id output was not verified, so a
  reliable resume contract could not be source-cited. `daemon(action='ask')`
  returns an explicit unsupported-backend error.
- MCP arbitrary-server loading is not wired: help shows `acp` but no clear
  `--mcp` server path, so the backend ships no-MCP for now.

## Validation checklist

1. `command -v kimi` or documented installation path exists.
2. `--help` confirms `--prompt` / `--output-format` and that `--yolo` conflicts
   with `--prompt`.
3. A dry-run in a disposable worktree exits non-interactively.
4. Before enabling `ask`, source-cite a stable session-id output + tested resume
   command from local help/code (do not guess).
