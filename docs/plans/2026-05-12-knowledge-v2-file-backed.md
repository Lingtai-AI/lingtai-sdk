# Knowledge v2: file-backed, isomorphic to skills, physically separate

**Status:** implemented in this PR
**Date:** 2026-05-12
**Builds on:** PR #95 (`docs-library-contract`) — established `knowledge` as the
canonical private durable-memory capability and removed `library` / `codex`
aliases.

## Problem

PR #95 cleaned up naming but kept the JSON-database substrate from the
`library`/`codex` era: a single `<agent>/knowledge/knowledge.json` storing
`{id, title, summary, content, supplementary}` records, manipulated through a
four-action `submit` / `view` / `consolidate` / `delete` tool.

That substrate has the wrong shape for private durable knowledge:

1. **Heavy where it should be light.** The skills capability — knowledge's
   public/portable sibling — is a pure presentation tool: it scans
   `<agent>/.library/{intrinsic,custom}/<name>/SKILL.md` and renders a catalog
   into the prompt. The agent authors skills with `write`/`edit` like any other
   file. Knowledge should work the same way; the JSON tool surface duplicates
   filesystem semantics behind a custom API.
2. **No supporting files.** A JSON record can carry `content` and
   `supplementary` strings, but it cannot host scripts, raw logs, diagrams,
   datasets, or attachments next to the prose. Entries that exist only as
   pointers to context lose half their value.
3. **Wrong authoring model.** Submitting via tool calls means every revision is
   a custom round-trip, and the agent never sees its own knowledge through the
   filesystem. Skills are revised by editing the file — knowledge should be the
   same.
4. **Structural divergence from skills.** Two related capabilities should look
   alike to the agent. Today they don't: skills lives on disk and is read by
   name; knowledge lives in JSON and is read by id.

## Goals

1. Make `knowledge` **structurally isomorphic to `skills`**: same scanner
   shape, same frontmatter conventions, same single-action `info` tool, same
   on-demand body loading via the regular `read` tool.
2. Keep `knowledge` and `skills` **physically separate**: distinct
   directories, distinct filenames, distinct handlers, distinct prompt sections.
3. Preserve the **private/public boundary**: knowledge entries may reference
   local paths, mail ids, and logs; skills must not.
4. Replace the JSON database with a filesystem catalog **in one focused
   patch**, with one narrow compatibility bridge: existing `knowledge.json`
   entries migrate once into `KNOWLEDGE.md` folders.

## Non-goals

- Sharing knowledge across agents. Knowledge is private, agent-owned memory.
- Search, query DSL, embedding indexes. Bodies are loaded by name via `read`.
- A revision/history layer (git transactions, branch-per-entry, etc.).
  Filesystem revisions through `write`/`edit` are enough; if history matters
  the agent can wrap its own working directory in git.
- A capacity limit. The filesystem is the limit. No `knowledge_limit`
  enforcement; the kwarg is accepted and silently ignored so old presets do
  not break.

## Design

### On-disk layout

```text
<agent>/knowledge/
  <entry-name>/
    KNOWLEDGE.md          # required — YAML frontmatter + body
    scripts/              # optional — supporting code
    assets/               # optional — diagrams, datasets, attachments
    notes/                # optional — long-form supplementary
    raw-log.json          # optional — anything else relevant
```

Each immediate subdirectory of `<agent>/knowledge/` that contains a
`KNOWLEDGE.md` is one entry. Folders without a `KNOWLEDGE.md` are recursed
into so nested namespaces are allowed; folders with loose files but no
`KNOWLEDGE.md` are reported as `problems` in the `info` snapshot.

`KNOWLEDGE.md` frontmatter:

```markdown
---
name: <routing handle — required>
description: <one or more sentences — required; prompt-visible>
version: <optional>
---

<body — read on demand via the `read` tool>
```

The body is free-form Markdown. It is **never** parsed by the capability and
**never** injected into the system prompt; the agent loads it through `read`
when the catalog entry looks relevant, exactly the same flow it uses for
skills.

### Tool surface

A single action, mirroring `skills`:

| Action | Required | Optional | Returns |
|---|---|---|---|
| `info` | — | — | `{status, knowledge_dir, catalog_size, problems}` |

Unknown actions return `{status: "error", message: ...}`. The historical
`submit` / `view` / `consolidate` / `delete` actions are gone — entries are
authored by writing files, viewed by reading files. The `info` call re-scans
the directory and refreshes the prompt section, so newly authored entries
appear without requiring a restart.

### Prompt injection

Same shape as the skills catalog, in its own protected section:

```text
<knowledge>
  <entry>
    <name>tcp-retry</name>
    <description>How the mail service retries TCP — exponential backoff and failure modes.</description>
    <location>/path/to/agent/knowledge/tcp-retry/KNOWLEDGE.md</location>
  </entry>
  ...
</knowledge>
```

The preamble tells the agent how to load entries (`read` the `location` field)
and how to pin them into the pad (`psyche({object:'pad', action:'append',
files:[location]})`) for cross-turn persistence. Bodies and supporting files
never appear in the prompt.

### Knowledge vs skills — what stays distinct

| Aspect | `skills` | `knowledge` |
|---|---|---|
| Root | `<agent>/.library/{intrinsic,custom}/` | `<agent>/knowledge/` |
| Manifest filename | `SKILL.md` | `KNOWLEDGE.md` |
| Tool name | `skills` | `knowledge` |
| Prompt section | `skills` (`<available_skills>`) | `knowledge` (`<knowledge>`) |
| Extra path sources | `manifest.capabilities.skills.paths` | none |
| Intrinsic manual install | yes (`intrinsic/capabilities/skills/SKILL.md`) | no — knowledge is agent-authored |
| Visibility | portable / shareable | private / agent-owned |
| May reference local paths, mail ids, logs | no | yes |

Two separate modules. Two separate scanners. Two separate handlers. The code
is **isomorphic**, not shared — extracting a helper would couple the public
skills surface to the private knowledge semantics, which is exactly the
boundary we are protecting.

### What was removed

- `KnowledgeManager` class and all four CRUD actions
  (`submit` / `view` / `consolidate` / `delete`).
- Runtime JSON store at `<agent>/knowledge/knowledge.json`. If the legacy file
  exists, it is migrated once into folders and renamed to `knowledge.json.migrated`.
- Capacity limit (`DEFAULT_MAX_ENTRIES = 50`, `knowledge_limit=N`). The kwarg
  is accepted and ignored for preset compatibility.
- `_make_id`, deterministic 8-char content hashes, and `created_at`
  timestamps. Identity is the directory name; revision is the filesystem.
- Progressive-disclosure depth flags (`include_supplementary`). Disclosure is
  layered by file: frontmatter in prompt, body via `read`, supporting files
  via `read`/`bash`.
- All `knowledge.title` / `knowledge.summary` / `knowledge.content` /
  `knowledge.supplementary` / `knowledge.ids` / `knowledge.include_supplementary`
  i18n keys. Replaced with `knowledge.action_info` and `knowledge.preamble`.

### What was preserved

- Canonical capability name `knowledge`; former `library` / `codex` names
  remain unregistered. Same breaking-rename posture as PR #95.
- The knowledge-references-skill / skills-do-not-reference-knowledge
  directionality. Now enforced by physical separation as well as documentation:
  the knowledge tool only reads `<agent>/knowledge/`, the skills tool only
  reads `<agent>/.library/` plus declared paths, and the manifest filenames
  do not collide.

## Migration

The patch includes a one-time migration for previous JSON-backed stores. On
setup or `knowledge({action: "info"})`, if either
`<agent>/knowledge/knowledge.json` or the older `<agent>/codex/codex.json`
exists, the capability reads its `entries` array and converts each object:

- `title` becomes the filesystem slug seed and the optional frontmatter `title`.
- `summary` becomes frontmatter `description`.
- `content` becomes the main `KNOWLEDGE.md` body.
- `supplementary` becomes `references/supplementary.md` and a `## References`
  link in the main document.
- `id` is preserved as frontmatter `legacy_id`.

After at least one successful migrated entry, the source JSON is renamed to
`knowledge.json.migrated` or `codex.json.migrated` (or a numbered variant) so migration does not repeat.
Malformed JSON or malformed entries are reported as `problems` in the `info`
snapshot; the prompt catalog only includes successfully migrated/scanned
entries.

## Verification

The patch is reviewable in one pass:

- `src/lingtai/core/knowledge/__init__.py` — rewritten as a scanner.
- `src/lingtai/core/knowledge/CONTRACT.md` — updated to describe the
  filesystem catalog.
- `src/lingtai/core/knowledge/ANATOMY.md` — updated to match.
- `src/lingtai/i18n/{en,zh,wen}.json` — `knowledge.*` keys replaced.
- `tests/test_knowledge.py` — rewritten against the new contract; asserts
  catalog contains only metadata, asserts `KNOWLEDGE.md` and `SKILL.md`
  conventions are distinct, asserts entries may carry scripts/assets/private
  references.

Run:

```bash
python -m pytest tests/test_knowledge.py tests/test_skills.py tests/test_check_caps.py tests/test_daemon_preset_capabilities.py -q
```
