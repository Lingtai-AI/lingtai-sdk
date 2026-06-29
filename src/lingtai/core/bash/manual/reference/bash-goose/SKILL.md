---
name: bash-goose
description: >
  Nested bash-manual reference for Goose CLI. Read this when you need to run,
  validate, or document `goose` as a long-running shell subprocess or LingTai
  daemon harness candidate.
version: 0.1.0
last_changed_at: "2026-06-13T04:32:46-07:00"
---

# Goose CLI

Nested bash-manual reference. This page owns **shell execution hygiene** for
`goose`: command shape, async/poll discipline, approval flags, session/resume
caveats, and whether the CLI is ready for a LingTai daemon backend.

## Status

Candidate harness. Docs mention start/resume sessions and `--no-session`; exact headless command should be verified before backend implementation.

## Command shape

```bash
goose run/session commands (verify current `goose --help`)
```

Before relying on the command in production, run the current CLI's `--help` and
prefer `bash(async=true)` for work that can think, edit files, or run tools for
minutes. Do not run long coding CLIs synchronously from the parent turn.

## LingTai daemon notes

- A daemon backend needs a deterministic non-interactive start command.
- `ask`/resume needs a stable session id and a tested resume command.
- JSON/JSONL output is preferred, but a transcript parser may be acceptable when
  tests pin the output contract.
- If any of those are missing, keep the CLI as a documented bash harness rather
  than adding a speculative daemon backend.

## Validation checklist

1. `command -v goose` or documented installation path exists.
2. `--help` confirms the non-interactive command and approval flags.
3. A dry-run in a disposable worktree exits non-interactively.
4. If promoted to daemon backend, tests mock subprocess launch, output parsing,
   session capture, resume/ask, and reserved `backend_options` handling.
