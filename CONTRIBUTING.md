# Contributing to lingtai-kernel

Thank you for helping improve the LingTai Python runtime.

## Start here

- Repository navigation: [`ANATOMY.md`](ANATOMY.md)
- Claude Code / coding-agent guidance:
  [`docs/references/claude-code-guide.md`](docs/references/claude-code-guide.md)
- Source-root anatomy: [`src/lingtai_kernel/ANATOMY.md`](src/lingtai_kernel/ANATOMY.md)
- Rust sidecar notes: [`crates/lingtai-search-sidecar/README.md`](crates/lingtai-search-sidecar/README.md)

## Workflow

Use a branch and worktree for non-trivial changes, keep changes focused, and open
pull requests rather than pushing directly to `main`.

```bash
git fetch origin main
git worktree add -b <branch-slug> .worktrees/<slug> origin/main
cd .worktrees/<slug>
```

Before requesting review, run the narrow tests relevant to your change and at
least `git diff --check`. For code changes, prefer targeted `pytest` runs plus
any package/build checks affected by the diff.

## Root hygiene

The repository root is reserved for entry points, legal files, build metadata,
and tool files that must live at root. Long-form references, language variants,
plans, and archival material belong under `docs/`.
