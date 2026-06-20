# CLAUDE.md

This root file is intentionally short. It is the entry point Claude Code reads
first; the full repository guidance lives in
[`docs/references/claude-code-guide.md`](docs/references/claude-code-guide.md).

## Non-negotiable rules

1. **Use a worktree for non-trivial edits.** Do not edit the main checkout for
   anything beyond a tiny typo. Create `.worktrees/<slug>/` from `origin/main`.
2. **Read the full guide before changing code or docs.** The full guide contains
   the test commands, architecture notes, package layout, and repo conventions.
3. **Keep root clean.** Put durable long-form references under `docs/`; use
   root only for entry points, legal files, build metadata, and tool files that
   must be discovered from the repository root.

## Quick start

```bash
git fetch origin main
git worktree add -b <branch-slug> .worktrees/<slug> origin/main
cd .worktrees/<slug>
# read docs/references/claude-code-guide.md before editing
```

After merge or abandonment:

```bash
git worktree remove .worktrees/<slug>
git branch -d <branch-slug>  # or -D if abandoned
```
