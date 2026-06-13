---
name: bash-zed-acp
description: >
  Nested bash-manual reference for Zed / ACP agent bridge. Read this when you need to run,
  validate, or document `ACP servers` as a long-running shell subprocess or LingTai
  daemon harness candidate.
version: 0.1.0
---

# Zed / ACP agent bridge

Nested bash-manual reference. This page owns **shell execution hygiene** for
`ACP servers`: command shape, async/poll discipline, approval flags, session/resume
caveats, and whether the CLI is ready for a LingTai daemon backend.

## Status

Ecosystem bridge rather than a daemon backend by itself. Document only unless there is a direct headless ACP client command to wrap.

## Command shape

```bash
agent-specific ACP command
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

1. Identify the specific ACP-compatible client or server command you intend to wrap; ACP is a protocol, not a standalone `ACP` binary.
2. Run that command's `--help` (or documented equivalent) to confirm non-interactive mode and approval flags.
3. A dry-run in a disposable worktree exits non-interactively.
4. If promoted to daemon backend, tests mock subprocess launch, output parsing,
   session capture, resume/ask, and reserved `backend_options` handling.
