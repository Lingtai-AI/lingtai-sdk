---
related_files:
  - src/lingtai/ANATOMY.md
  - src/lingtai/agent.py
  - src/lingtai/core/daemon/__init__.py
  - src/lingtai/core/skills/__init__.py
  - src/lingtai/core/skills/manual/SKILL.md
  - src/lingtai/init_schema.py
  - tests/test_skills.py
  - tests/test_validate_skill.py
maintenance: |
  Keep related_files as repo-relative paths to real files. Include neighboring
  ANATOMY.md files so the anatomy graph stays connected rather than isolated;
  anatomy links must be bidirectional. If you create a new ANATOMY.md, copy this
  maintenance field. If you notice drift between this anatomy and the code,
  report it. See lingtai-dev-guide for details.
---
# core/skills

Skills capability — per-agent skill catalog and skill-manual surface. This is the renamed successor of the old `library` capability. It scans the existing `.library/` directory plus configured extra paths, renders a YAML catalog (one `- name:` block per skill with `location:` and a `description:` block scalar), and injects it into the `skills` prompt section. It never writes skill files; installation remains the Agent initializer's job.

## Components

- `skills/__init__.py` — the capability implementation. `get_description` (`__init__.py:166-167`), `get_schema` (`__init__.py:170-181`), `setup` (`__init__.py:184-219`), `_reconcile` (`__init__.py:75-159`), and path/scanner helpers (`__init__.py:50-68`).
- `skills/manual/` — `skills-manual` skill documentation, template assets, and validator script. The validator can optionally require `last_changed_at` for LingTai-maintained skill bundles.

## Connections

- `lingtai.capabilities` maps canonical `skills` here. Former skill-catalog `library.paths` compatibility is removed in the clean rename.
- `Agent._install_intrinsic_manuals()` copies every capability `manual/` bundle into `.library/intrinsic/capabilities/<name>/`, then re-runs `skills._reconcile()` for first-turn catalog freshness when `skills` is loaded (`../../agent.py:158-229`).
- The daemon capability blacklists `skills` so emanations do not recursively receive the skill catalog tool (`../daemon/__init__.py:34`).

## Public API

The `skills` tool exposes one action:

| Action | Description |
|---|---|
| `info` | Return the skills manual body plus a runtime health snapshot (catalog size, paths report, problems) |

## State

- Skill storage remains `<agent>/.library/` for compatibility: `intrinsic/` is CLI-managed and `custom/` is agent-authored (`__init__.py:87-90`).
- Config path source is canonical `manifest.capabilities.skills.paths` (`../../init_schema.py:247-268`).
- Prompt state is the `skills` section (`__init__.py:125-131`).
- Health check expects `.library/intrinsic/capabilities/skills/SKILL.md` and reports `skills_manual`, with `library_manual` retained as a response compatibility key (`__init__.py:133-152`).

## Notes

- The `.library/` directory name and `.library_shared/` convention are intentionally preserved in this rename-only change; they are storage compatibility names, not the user-facing capability name.
- New callers should use `skills({"action":"info"})`; old `library({"action":"info"})` is not registered because private durable memory is now `knowledge` and `library` is not registered.
- LingTai-maintained `SKILL.md` files carry `last_changed_at` in frontmatter, initialized from git history for metadata-only backfills and updated on substantive skill edits.
