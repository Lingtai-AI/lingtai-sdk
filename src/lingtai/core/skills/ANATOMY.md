# core/skills

Skills capability — per-agent skill catalog and skill-manual surface. This is the renamed successor of the old `library` capability. It scans the existing `.library/` directory plus configured extra paths, renders a YAML catalog (one `- name:` block per skill with `location:` and a `description:` block scalar), and injects it into the `skills` prompt section. It never writes skill files; installation remains the Agent initializer's job.

## Components

- `skills/__init__.py` — the capability implementation. `get_description` (`__init__.py:296-297`), `get_schema` (`__init__.py:300-311`), `setup` (`__init__.py:314-348`), `_reconcile` (`__init__.py:205-289`), and scanner helpers (`__init__.py:53-198`).
- `skills/manual/` — `skills-manual` skill documentation, template assets, and validator script.

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

- Skill storage remains `<agent>/.library/` for compatibility: `intrinsic/` is CLI-managed and `custom/` is agent-authored (`__init__.py:217-220`).
- Config path source is canonical `manifest.capabilities.skills.paths` (`../../init_schema.py:247-268`).
- Prompt state is the `skills` section (`__init__.py:255-261`).
- Health check expects `.library/intrinsic/capabilities/skills/SKILL.md` and reports `skills_manual`, with `library_manual` retained as a response compatibility key (`__init__.py:263-282`).

## Notes

- The `.library/` directory name and `.library_shared/` convention are intentionally preserved in this rename-only change; they are storage compatibility names, not the user-facing capability name.
- New callers should use `skills({"action":"info"})`; old `library({"action":"info"})` is not registered because private durable memory is now `knowledge` and `library` is not registered.
