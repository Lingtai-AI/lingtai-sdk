---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/core/knowledge/CONTRACT.md
  - src/lingtai/core/knowledge/__init__.py
  - src/lingtai/core/knowledge/manual/SKILL.md
  - tests/test_knowledge.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# core/knowledge

Knowledge capability — private durable knowledge across molts. The catalog is
filesystem-backed: each immediate subdirectory of `<agent>/knowledge/` with a
`KNOWLEDGE.md` file is one entry. The frontmatter `name` + `description` are
injected as a compact YAML catalog in the system prompt's
`knowledge` section. Bodies and supporting files are loaded on demand through
the regular `read` tool.

## Components

- `knowledge/__init__.py` — the capability implementation. `_parse_frontmatter`,
  `_scan`, `_build_catalog_yaml`, `_reconcile`, `get_description`, `get_schema`,
  and `setup` live here.
- `knowledge/CONTRACT.md` — public behavior contract: tool surface, on-disk
  layout, prompt injection, knowledge/skill directionality, anchored claims,
  and verification matrix.

## Connections

- `lingtai.capabilities` maps builtin capability name `knowledge` here. Former
  `library` and `codex` capability names are not registered.
- `setup()` registers exactly one tool, `knowledge`, with a single `info`
  action. The historical `knowledge_limit` kwarg is accepted and ignored.
- `_reconcile()` writes protected prompt section `knowledge`.
- `skills/` is the structurally isomorphic, physically separate sibling
  capability — it owns `<agent>/.library/{intrinsic,custom}/<name>/SKILL.md`,
  knowledge owns `<agent>/knowledge/<name>/KNOWLEDGE.md`. Two separate
  modules, two separate tools, two separate prompt sections.

## State

- Root path: `<agent>/knowledge/`.
- Entry layout: `<agent>/knowledge/<name>/KNOWLEDGE.md` plus arbitrary
  supporting files (scripts, assets, notes, raw logs).
- Required frontmatter: `name`, `description`. Optional: `version`.
- Prompt state: protected `knowledge` section holds the preamble + YAML catalog
  (one `- name:` block per entry, with `location:` and `description:` fields).
- No JSON store and no per-entry size cap. A one-time legacy migration
  converts `knowledge/knowledge.json` and old `codex/codex.json` entries into `KNOWLEDGE.md` folders, writes old `supplementary` text to `references/supplementary.md`, and renames the source JSON to `<name>.json.migrated`.

## Invariants

- `knowledge` is private, agent-owned memory. It is not the public skill
  catalog.
- `library` and `codex` are gone as durable-memory aliases. This is a breaking
  rename by design.
- The catalog injects only `name`/`description`/`path`. Bodies and supporting
  files never appear in the prompt; the agent loads them via `read`.
- The capability normally never writes inside `<agent>/knowledge/`; the sole
  exception is the one-time legacy JSON migration. After migration, the agent is
  the sole author.
- `SKILL.md` belongs to skills; `KNOWLEDGE.md` belongs to knowledge. The two
  filenames are not aliases.
- For the stable behavior contract, read `src/lingtai/core/knowledge/CONTRACT.md`
  before editing this capability.
