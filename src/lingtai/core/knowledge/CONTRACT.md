# Knowledge capability contract

`knowledge` is the agent-private durable knowledge capability. It scans the
agent's local `knowledge/` directory for `KNOWLEDGE.md`-bearing entries and
injects a compact catalog into the system prompt. The implementation lives in
`src/lingtai/core/knowledge/`; the code is the source of truth.

## Routing Card

**Use this when:**
- You are editing the private durable knowledge capability.
- You are reviewing the catalog scanner, prompt injection, frontmatter schema,
  or the knowledge/skill boundary.
- You need to verify that knowledge entries can carry private references
  (local paths, mail ids, logs) without violating the skills contract.

**Do not use this for:**
- Skill catalog behavior: read `src/lingtai/core/skills/CONTRACT.md` (the
  structurally isomorphic, physically separate sibling).
- Code navigation only: read `src/lingtai/core/knowledge/ANATOMY.md`.
- Authoring procedures for sharing across agents: write a skill instead.

**Fast paths:** tool schema -> §Tool surface; on-disk layout -> §Storage;
how it differs from skills -> §Knowledge vs skills.

## Scope

- Canonical capability name: `knowledge`.
- Canonical tool name: `knowledge`.
- Former names `library` and `codex` are intentionally not compatibility aliases.

This is a breaking rename while the user base is still small. New manifests must
spell the private durable store as `knowledge`. Old `library`/`codex` capability
entries are skipped as unknown capabilities; old `library(...)`/`codex(...)`
tool calls are unavailable.

`knowledge` means private durable memory: what one agent has learned, decided,
and discovered. `skills` means portable procedure catalog. Knowledge entries
MAY reference public skills; skills MUST NOT depend on private knowledge entry
contents, agent-local paths, mail ids, or other private memory state.

## Knowledge vs skills

The two capabilities are structurally isomorphic but physically separate:

| Aspect | `skills` | `knowledge` |
|---|---|---|
| Root directory | `<agent>/.library/{intrinsic,custom}/` | `<agent>/knowledge/` |
| Manifest file | `SKILL.md` | `KNOWLEDGE.md` |
| Tool name | `skills` | `knowledge` |
| Tool surface | `info` | `info` |
| Prompt section | protected `skills` (YAML catalog) | protected `knowledge` (YAML catalog) |
| Extra path sources | `manifest.capabilities.skills.paths` | none — strictly per-agent |
| Visibility | portable / shareable | private / agent-owned |
| May reference local paths, mail ids, logs | no | yes |

Two separate handlers register two separate tools. Shared Markdown catalog
mechanics live in `src/lingtai/core/_catalog.py`; each capability keeps its own
tool, storage root, prompt section, and semantic contract so private knowledge
does not leak into the public skill catalog.

## Tool surface

The schema requires `action` and accepts exactly one action:

| Action | Required fields | Optional fields | Return on success |
|---|---|---|---|
| `info` | — | — | `{status: "ok", knowledge_dir, catalog_size, problems}` |

Unknown actions return `{status: "error", message: ...}` and do not mutate
state. The previous JSON-database actions (`submit`, `view`, `consolidate`,
`delete`) are intentionally removed: knowledge is now authored by writing
`KNOWLEDGE.md` files with the regular `write`/`edit` tools, just like skills.
There is no in-tool capacity limit; the historical `knowledge_limit` kwarg is
accepted but ignored.

Only `knowledge(...)` is registered. There is no `library(...)` or `codex(...)`
alias.

## Storage

The on-disk layout is:

```text
<agent>/knowledge/
  <entry-1>/
    KNOWLEDGE.md
    scripts/
    assets/
    notes/
  <entry-2>/
    KNOWLEDGE.md
    raw-log.json
  ...
```

Each entry is a directory whose name is the routing handle. The directory must
contain a `KNOWLEDGE.md` file with YAML frontmatter:

```markdown
---
name: <routing handle>
description: <one or more sentences; prompt-visible>
version: <optional>
---

<body — read on demand via the `read` tool>
```

Required frontmatter fields are `name` and `description`. Entries missing
either are skipped and surfaced in `problems`. Folders without a
`KNOWLEDGE.md` are recursed into so nested namespaces are allowed; folders
with loose files but no `KNOWLEDGE.md` are reported as corrupted.

Entries may carry supporting files (scripts, assets, notes, raw logs,
attachments). Those files are not parsed by the capability; the agent opens
them via the regular `read`/`bash` tools when it loads an entry.

The agent is the sole long-term author of `knowledge/`. The only capability
write is a one-time legacy migration: if `knowledge/knowledge.json` or old `codex/codex.json` exists, entries are converted to `knowledge/<slug>/KNOWLEDGE.md`, each legacy `supplementary` field is written to `references/supplementary.md`, and the source JSON is renamed to `<name>.json.migrated` to prevent repeat work.

## Prompt injection

On setup and on every `info` call, the capability rewrites protected prompt
section `knowledge`:

- If there are entries, the section contains a preamble plus a YAML
  catalog. Each entry is rendered as a `- name:` block with `location:`
  (absolute `KNOWLEDGE.md` path) and a `description:` block scalar.
- If there are no entries, the section is cleared.

Only `name`, `description`, and `path` are ever injected. Bodies and supporting
files stay out of the prompt until the agent loads them through `read`. This
mirrors the skills catalog and keeps the always-on prompt cheap.

## Knowledge / skill directionality

Knowledge entries MAY reference skills by public path/name when an agent has
learned that a skill is useful for a recurring situation.

Skills MUST NOT reference private knowledge entry paths, private agent paths,
mail ids, or agent-local memory state.

Reason: skills are portable shared procedures; knowledge is agent-local
accumulated memory. The dependency direction is knowledge -> skill, never
skill -> private knowledge.

## Anchored claims

| Claim | Source | Test |
|---|---|---|
| `knowledge` is the only private durable memory capability in the builtin registry | `src/lingtai/capabilities/__init__.py` | `tests/test_check_caps.py::test_get_all_providers_includes_expected_capabilities` |
| `knowledge` setup registers only the `knowledge` tool | `src/lingtai/core/knowledge/__init__.py` | `tests/test_knowledge.py::test_knowledge_setup_registers_only_knowledge_tool` |
| Legacy `knowledge/knowledge.json` and `codex/codex.json` entries migrate once into `KNOWLEDGE.md` folders; `supplementary` becomes `references/supplementary.md` | `src/lingtai/core/knowledge/__init__.py` | `tests/test_knowledge.py::test_legacy_knowledge_json_migrates_to_knowledge_md`, `tests/test_knowledge.py::test_legacy_codex_json_migrates_to_knowledge_md` |
| Manager-style lookup is exact: `knowledge` resolves and former names do not | `src/lingtai/agent.py` | `tests/test_knowledge.py::test_former_alias_capabilities_do_not_register_knowledge` |
| Catalog reads `<agent>/knowledge/<name>/KNOWLEDGE.md` and excludes `SKILL.md` entries | `src/lingtai/core/knowledge/__init__.py` | `tests/test_knowledge.py::test_knowledge_md_convention_distinct_from_skill_md` |
| Prompt catalog includes only `name`/`description`/`path` from frontmatter | `src/lingtai/core/knowledge/__init__.py` | `tests/test_knowledge.py::test_prompt_catalog_only_metadata_not_body` |
| Entries may carry references, scripts, and assets | filesystem convention | `tests/test_knowledge.py::test_entries_may_have_scripts_and_assets` |

## Verification matrix

| Invariant | Automated test | Manual check | Risk if broken |
|---|---|---|---|
| `knowledge(...)` is the only private-memory tool | `tests/test_knowledge.py` | Boot with `capabilities={"knowledge": {}}` and inspect tools | Old names linger and confuse the model |
| Former `library`/`codex` names do not register | alias-removal tests in `tests/test_knowledge.py` / `tests/test_skills.py` | Boot old manifests and inspect capability skip logs | Breaking rename is only half-applied |
| Skills do not depend on private knowledge | documented invariant; enforce by review | Check shared skill docs for private paths/ids | Shared skills become non-portable |
| Knowledge and skills use distinct manifest filenames | `tests/test_knowledge.py::test_knowledge_md_convention_distinct_from_skill_md` | Drop a `SKILL.md` into `<agent>/knowledge/foo/` and confirm it is not picked up | Physical separation collapses; private/public boundary blurs |
| Body content stays out of prompt catalog | `tests/test_knowledge.py::test_prompt_catalog_only_metadata_not_body` | Author an entry with a long body and inspect the prompt section | Prompt bloat / private detail leakage |

Run before merging knowledge changes:

```bash
python -m pytest tests/test_knowledge.py tests/test_skills.py tests/test_check_caps.py tests/test_daemon_preset_capabilities.py -q
```
