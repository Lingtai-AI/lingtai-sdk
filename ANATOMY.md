---
related_files:
  - CLAUDE.md
  - CODE_OF_CONDUCT.md
  - CONTRIBUTING.md
  - MANIFEST.in
  - README.md
  - SECURITY.md
  - SUPPORT.md
  - docs/references/claude-code-guide.md
  - pyproject.toml
  - setup.py
  - src/lingtai/ANATOMY.md
  - src/lingtai_kernel/ANATOMY.md
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# lingtai-kernel Repository Anatomy

This root anatomy is a router for the repository. It intentionally stays short:
code-level anatomy begins at [`src/lingtai_kernel/ANATOMY.md`](src/lingtai_kernel/ANATOMY.md),
while long-form references live under [`docs/`](docs/).

## Components

- [`.github/`](.github/) — GitHub Actions, issue templates, and pull request templates.
- [`crates/lingtai-search-sidecar/`](crates/lingtai-search-sidecar/) — Rust
  file-search sidecar crate packaged with the Python runtime.
- [`docs/`](docs/) — durable documentation, plans, language-specific readmes,
  and long-form references.
- [`src/lingtai/`](src/lingtai/) — compatibility package and service modules
  exposed under the `lingtai` package name.
- [`src/lingtai_kernel/`](src/lingtai_kernel/) — core Python runtime; start with
  [`src/lingtai_kernel/ANATOMY.md`](src/lingtai_kernel/ANATOMY.md).
- [`tests/`](tests/) — pytest suite for runtime, services, tools, and packaging
  behavior.

## Root files

- [`README.md`](README.md) — public English entry point; links to translated
  readmes under `docs/readmes/`.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — contributor entry point.
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md), [`SECURITY.md`](SECURITY.md), and [`SUPPORT.md`](SUPPORT.md) — GitHub community and safety entry points.
- [`CLAUDE.md`](CLAUDE.md) — short Claude Code entry point; full guidance is
  [`docs/references/claude-code-guide.md`](docs/references/claude-code-guide.md).
- [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE) — legal metadata; both are kept at root and included in source distributions.
- [`pyproject.toml`](pyproject.toml), [`setup.py`](setup.py), and
  [`MANIFEST.in`](MANIFEST.in) — Python packaging and Rust sidecar build hooks.
- [`.gitignore`](.gitignore) — local scratch/build/cache exclusions.

## Composition

`pyproject.toml` declares the Python package metadata and delegates sidecar build
hooks to `setup.py`. `MANIFEST.in` keeps the Rust sidecar sources and packaged
prompt Markdown resources connected to source distributions. The runtime code under `src/lingtai_kernel/` is the primary source
of truth for agent behavior; `src/lingtai/` contains public compatibility/service
surfaces. Documentation that does not need root discovery is kept in `docs/` so
the root remains an entry-point layer rather than an archive.

## Maintenance notes

- Keep this file aligned with root-level moves and top-level package layout.
- If code moves under `src/lingtai_kernel/`, update the nearest nested
  `ANATOMY.md` together with the code.
- Keep long-form guidance out of root unless an external tool requires the file
  to be discovered there.
