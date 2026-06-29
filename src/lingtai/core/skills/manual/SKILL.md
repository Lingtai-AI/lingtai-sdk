---
name: skills-manual
description: >
  Operational guide for the `skills` capability — your skill catalog's
  on-disk layout, how the YAML skill catalog in your system prompt
  is built, and the full authoring/publishing workflow for new skills.

  Reach for this manual when:
    - You're authoring a new skill in `.library/custom/<name>/` and need
      the frontmatter schema, the bundled template, the validator, or the
      "do create a skill / do NOT create a skill" decision rules.
    - You want to publish a custom skill to the network-shared library
      (`.library_shared/`) and need the `cp -r` recipe plus the admin
      stewardship norms.
    - A skill you expect to see isn't showing up in the catalog — the
      health-check workflow (`skills({"action": "info"})`) and the
      `intrinsic` vs `custom` directory split tell you what's wrong.
    - You want to pin a skill's body into your pad (so it survives the
      next molt and stays in the cached prefix) — the `psyche` pinning
      recipe lives here.
    - You're adding a new skills path source (e.g. a project-specific
      utilities directory) by editing `init.json`'s
      `manifest.capabilities.skills.paths`.
    - You're turning a large manual into a progressive-disclosure router
      and need the nested skill/reference pattern.

  Covers: directory layout (`.library/{intrinsic,custom}/`), required vs
  optional frontmatter fields, name-collision discipline, when to author
  a skill and when not to, what makes a skill description trigger-friendly,
  nested skill/reference catalogs, the validator's failure modes, and the
  relationship between the kernel's intrinsic skill bundles and your
  editable `custom/` territory.

  Does NOT cover: the bundled skills themselves — their READMEs and
  SKILL.md files document them. This is meta — how the skills *system*
  works, not what's *inside* it.
version: 1.1.0
last_changed_at: "2026-06-29T08:11:47Z"
---

# The Skills Capability

This is the skills capability's own manual. It documents how the skill catalog works from your side: the on-disk layout, the YAML catalog, and the authoring/publishing workflow. The skills capability scans `.library/` plus any extra paths declared in `init.json`, builds the YAML skill catalog, and injects it into your system prompt.

## On-disk layout

Your skill catalog lives at `<agent>/.library/`:

```
<agent>/.library/
├── intrinsic/
│   ├── capabilities/
│   │   └── <cap>/<manual files>
│   └── addons/
│       └── <addon>/<manual files>
└── custom/
```

- `intrinsic/` — **CLI-managed.** Wiped and rewritten from kernel-shipped manual bundles on every `system({"action": "refresh"})`. Do not edit — your edits will be erased. Read-only territory.
- `intrinsic/capabilities/<cap>/` — manual for each loaded capability (e.g. `skills/`, `email/`, `psyche/`).
- `intrinsic/addons/<addon>/` — manual for each loaded addon (e.g. `imap/`, `telegram/`, `feishu/`).
- `custom/` — **your territory.** Authored skills live here. The CLI never touches this directory.

Additional paths come from `init.json` at `manifest.capabilities.skills.paths` — typically `../.library_shared/` (the network-shared library) and `~/.lingtai-tui/utilities/` (operational utilities shipped by the TUI).

If the skills capability is NOT loaded, the files still exist on disk — you just don't get a catalog in your prompt. You can still reach the manuals via `read`, `grep`, `ls`.

## How the catalog works

The `skills` section of your system prompt is a YAML list. Each skill is one `- name: <name>` block with a `location:` (absolute path to the skill's `SKILL.md`) and a `description:` block scalar. To read a skill's body, use `read` on the file at that `location`. That gives you the full Markdown for that one turn.

## Loading a skill into active working memory

If you plan to use a skill across many turns or need it to survive a molt, pin its `SKILL.md` into your pad:

```
psyche({"object": "pad", "action": "append", "files": ["<location>"]})
```

The body is appended to your pad's read-only reference section, which is part of the cached system-prompt prefix. To unpin, call the same action with a new `files` list that omits the path (or `files: []` to clear everything).

Pinning is cheap per-token over a session because the pad sits in the cached prefix — repeated `read`s of the same file do NOT benefit from that cache.

## Authoring a new skill

Create a folder under `<agent>/.library/custom/<skill-name>/` with a `SKILL.md` starting with YAML frontmatter:

```
---
name: <skill-name>
description: One-line description of what this skill does
version: 1.0.0
# Required for LingTai-maintained skills; optional for custom/external skills.
last_changed_at: "2026-06-29T08:00:00Z"
---

Full instructions in Markdown below...
```

Required frontmatter for ordinary custom/external skills: `name`,
`description`. Optional for those skills: `version`, `author`, `tags`,
`last_changed_at`.

**LingTai-maintained skill metadata:** every skill bundle maintained inside a
LingTai repository must also include `last_changed_at` in its YAML frontmatter.
This applies to intrinsic capability manuals, standalone intrinsic skills, MCP
manual bundles, TUI preset utility skills, and their nested reference
`SKILL.md` files. Use an ISO 8601 timestamp with timezone, for example:

```yaml
last_changed_at: "2026-06-29T08:00:00Z"
```

For a historical backfill or metadata-only edit, derive the value from git
history, e.g. `git log -1 --format=%cI -- path/to/SKILL.md`, so the field points
to the latest meaningful skill-content change rather than the bookkeeping commit.
For any substantive future edit to the skill body, update `last_changed_at` in
the same commit.

`tags` is a list of lowercase, hyphenated strings that aid discoverability and (eventually) tier filtering. Three useful axes:

- **Language / runtime**: `python`, `fortran`, `bash`, `node`
- **Domain**: `physics`, `mhd`, `plasma`, `ml`, `web`
- **Type**: `solver`, `workflow`, `reference`, `cheatsheet`

Example: `tags: [python, physics, mhd, solver]`. Tags are best-effort metadata, not load-bearing — the catalog still finds skills without them.

After writing, call `system({"action": "refresh"})` so the skills capability rescans and re-injects the catalog.


### Cleanup / Footprint contract for tool manuals

Every tool/capability manual that owns persistent state must include a `Cleanup / Footprint` section. This is a contract, not a janitor: the section teaches agents what the tool leaves behind and how to audit it safely.

Minimum requirements:

- list concrete files/directories/caches/logs the tool creates;
- say what must never be deleted blindly;
- provide a read-only footprint check script or command;
- recommend an audit/cleanup cadence;
- require a dry-run report plus explicit user consent before destructive cleanup;
- append a cleanup/audit record to `logs/cleanup.jsonl` after the script runs;
- guide agents who read the manual for setup/troubleshooting/long-running work to self-audit the footprint.

The full template and consent rule live in `reference/cleanup-footprint-contract.md`.

### Starting from the template

If you'd rather not start from a blank file, copy the bundled template:

```
cp .library/intrinsic/capabilities/skills/assets/skill-template.md \
   .library/custom/<skill-name>/SKILL.md
```

The template has placeholder slots (`[SKILL_NAME]`, `[ONE_LINE_DESCRIPTION]`, etc.) and a soft skeleton of headings (`When this applies` / `Procedure` / `What to expect` / `Constraints` / `Scripts` / `Assets`). It works for code/executable skills out of the box; for reference-style skills (manuals, cheatsheets, chronicles) delete the `Procedure` section and write prose instead — there is a note at the top of the template reminding you of this.

### Validating before publishing

A bundled validator script catches the common failures before you ship:

```
python3 .library/intrinsic/capabilities/skills/scripts/validate.py \
   .library/custom/<skill-name>/
```

It checks: required frontmatter (`name`, `description`), unfilled `[PLACEHOLDER]` slots from the template, broken internal references (paths under `scripts/`, `assets/`, `references/` mentioned in `SKILL.md` that don't exist on disk), and `chmod +x` on Python scripts under `scripts/`. Exits 1 on any FAIL, 0 on PASS (warnings allowed). Run it after authoring and before `cp -r`'ing into `.library_shared/`.

For LingTai-maintained skill bundles, require the timestamp field too:

```
python3 .library/intrinsic/capabilities/skills/scripts/validate.py \
   --require-last-changed-at .library/custom/<skill-name>/
```

### Self-test before publishing

The validator catches structural issues but not content errors. After writing, walk through your skill as a fresh agent:

1. **Decision-tree test** — start at SKILL.md's first decision. Follow each branch. Does every reference file actually exist? Is the content reachable from the routing hub?
2. **Assertion test** — `grep` the actual codebase / file system for every claim in your skill: file paths, API signatures, parameter names, default values. Do NOT trust your memory of the code.
3. **Regression test** — fix any issues found, then repeat step 2.

Common errors this catches that the validator misses:

- Fictional file paths (e.g. referencing `helmholtz*.f90` that doesn't exist)
- API signatures from a previous code version
- Default parameter values that have since changed

Treat the self-test as mandatory for skills that document an external codebase — fabricated paths and stale signatures are the most damaging failure mode and the validator cannot see them.

### Recommended structure for complex knowledge skills

For skills that bundle non-trivial domain knowledge (multi-topic references, decision trees, ≥300 lines of total content), use a two-level progressive-disclosure structure:

```
<skill-name>/
├── SKILL.md              # Routing hub: decision tree + quick start + topic table
├── README.md             # GitHub-facing description (humans, not agents)
└── reference/
    ├── topic-a.md        # Self-contained deep-dive, loaded on demand
    ├── topic-b.md
    └── ...
```

`SKILL.md` is a **decision tree** (~150–180 lines): the agent picks a path, then does a single `read` on the right reference file. Each reference doc covers ONE topic (100–300 lines). The agent loads SKILL.md (~150 lines) plus one reference (~150 lines) instead of one 1000-line monolith.

When NOT to use this pattern: simple skills (single-API wrappers, linear checklists, prose-only references). A flat SKILL.md is correct when total content is under ~300 lines or there is only one path through it.

Reference implementations: `huangzesen/laps-skill`, `huangzesen/helmholtz-skill`.

### Nested skill/reference pattern for umbrella manuals

Use this pattern when a parent skill is itself a **router** and some child
references need to behave like mini-skills: their own frontmatter, trigger
summary, future `scripts/` or `assets/`, and a stable addressable folder. This is
for second-layer progressive disclosure inside one top-level catalog entry, not a
way to hide unrelated reusable skills.

```
<parent-skill>/
├── SKILL.md                         # Top-level cataloged router
└── reference/
    ├── topic-a/
    │   └── SKILL.md                 # Nested reference, loaded only via parent
    ├── topic-b/
    │   └── SKILL.md
    └── ...
```

Key rule: a nested `reference/<topic>/SKILL.md` is **not automatically promoted**
to the global skills catalog. The catalog scanner treats a directory that already
has `SKILL.md` as a skill boundary and does not descend into that folder for
additional catalog entries. Therefore the parent `SKILL.md` must inject the
children's routing metadata explicitly and keep the heavy procedural content in
the children.

Every parent-owned nested-reference router should contain **both**:

1. a `## Nested reference catalog` section with a fenced YAML list containing one
   item per child (`name`, `location`, `description`); and
2. a human-readable `## Routing table` (or equivalent decision table) that maps
   user/agent needs to the same child `location`s.

Preferred catalog shape:

```yaml
- name: parent-topic-a
  location: reference/topic-a/SKILL.md
  description: |
    Nested <parent-skill> reference for ... Read this after loading
    <parent-skill> when ...
- name: parent-topic-b
  location: reference/topic-b/SKILL.md
  description: |
    Nested <parent-skill> reference for ...
```

The YAML catalog is not just decoration: it is the machine-readable routing table
for the second layer. Keep it in sync with child frontmatter and with the human
routing table. Do not leave the parent as only a prose list of links.

Use nested references when all of these are true:

- callers should enter through one umbrella skill first;
- the child topic is substantial enough to deserve frontmatter and possibly its
  own `scripts/` or `assets/` later;
- exposing the child as a standalone top-level skill would clutter the catalog or
  bypass important routing context;
- the parent can clearly say when to read each child.

Do **not** use nested references for independent workflows that agents should
find directly from the top-level catalog. Those should be normal skills under
`.library/custom/<name>/`, `.library_shared/<name>/`, or an intrinsic skill root.

Nested child conventions:

- Each child `reference/<topic>/SKILL.md` must have normal skill frontmatter:
  `name` and `description` required; `version`/`tags` optional. If the parent
  skill is maintained inside a LingTai repository, the child must also carry
  `last_changed_at`. `name` should be unique within the parent and descriptive
  (`system-manual-sqlite-log-query`, `daily-reflection-data-collection`), even
  though it is not globally cataloged.
- The child `description` and the parent catalog `description` should start with
  the fact that it is nested, name the parent, and give the trigger condition:
  `Nested system-manual reference for ...`.
- `location` in the parent YAML catalog should be relative to the parent folder so
  it survives copy/install moves; agents reading the parent can resolve it next to
  the parent `SKILL.md`.
- The parent should remain the routing hub. Resident prompts and sibling skills
  should route to the parent first, then to the nested reference; do not bypass
  the parent unless the caller already has the parent context loaded.
- Tests should verify both levels: the parent body contains the YAML catalog,
  every child `name` and `location`, and the human routing table; the
  installed/copied skill tree contains each child `SKILL.md` with valid
  frontmatter and key content.
- The bundled validator checks one skill folder at a time. For nested children,
  validate the parent and then validate each nested child folder directly, e.g.
  `python3 .../validate.py reference/topic-a/` from the parent skill folder.

Reference implementations: `system-manual` is a top-level router with nested
`reference/substrate-manual/SKILL.md`, `reference/procedures-manual/SKILL.md`,
and `reference/sqlite-log-query/SKILL.md`, advertised through a `Nested reference
catalog` in the parent. Utility routers such as `swiss-knife`, `web-browsing`,
and `daily-reflection` use the same two-part shape: YAML child metadata first,
human routing table second.

### SKILL.md vs README.md

Skills published as standalone repos need both files — they serve different audiences.

| File         | Audience                      | Loaded by                            |
|--------------|-------------------------------|--------------------------------------|
| `SKILL.md`   | LingTai agents                | `skills` capability (system prompt) |
| `README.md`  | Humans browsing GitHub        | Not loaded by agents                 |

`SKILL.md` is the agent-facing routing hub (frontmatter + decision tree). `README.md` is the human-facing repo description (purpose, install, examples). They are NOT redundant — `README.md` carries information agents do not need (project status, license, contributor notes, screenshots), and `SKILL.md` carries fields humans do not parse (frontmatter `tags`, `version`, machine-readable description).

If you only ship inside `.library_shared/` and never publish to GitHub, you can omit `README.md`.

## Publishing to the network-shared library

If a custom skill is worth sharing with every agent in the network:

```
bash({"command": "cp -r .library/custom/<name> ../.library_shared/<name>"})
```

Then call `system({"action": "refresh"})`. Do **not** overwrite an existing entry in `.library_shared/` — if the name collides, rename your skill or consult the admin agent.

## Admin curation of `.library_shared/`

If you are the admin agent, you may edit, consolidate, rename, or remove entries in `.library_shared/` using `edit`/`write`/`rm` as needed.

If you are not the admin agent, **do not modify** `.library_shared/` beyond adding new entries with `cp`. Editing or removing existing entries is admin's stewardship. This is a norm, not a mechanical lock.

## Adding a new skills path

To expand your skill catalog with another source directory:

1. `edit` `init.json` under `manifest.capabilities.skills.paths`. Append your new path (absolute or relative to your working dir; `~/` expansion honored).
2. Call `system({"action": "refresh"})`.

`init.json` is the ground truth. There is no runtime state — whatever is in `paths` at setup time is the exact set scanned.

## Name collision discipline

Two skills with the same `name` in the catalog would collide. Before authoring a new skill in `custom/` or publishing to shared, grep the existing catalog:

```
bash({"command": "grep -rh '^name:' .library/ ../.library_shared/ ~/.lingtai-tui/utilities/ 2>/dev/null"})
```

If you hit a collision: rename, or adapt the existing skill instead of forking a second one.

## Health check

Call `skills({"action": "info"})` to verify your skill catalog is wired correctly. It returns this SKILL.md body plus a runtime snapshot: `library_dir`, `catalog_size`, resolved paths with exist/skill-count info, and any `problems` (invalid frontmatter, unreadable folders). If `status` is `"degraded"`, the error message tells you what needs fixing — typically a missing manual under `intrinsic/capabilities/skills/`, which means the initializer didn't install manuals correctly.

## When to create a skill

**Do create a skill when:**

- The task is repeatable with consistent steps.
- The procedure requires domain knowledge not reliably available without notes.
- A workflow involves multi-step orchestration or error handling worth codifying.
- You want to share expertise with other agents in the network.

**Do NOT create a skill when:**

- It's a one-off task with no reuse value.
- The task is just "call this one API endpoint" — pick it up at the call site.
- The instructions are personality or style preferences — those live in the covenant or your lingtai character, not here.

## Writing a good skill

1. **Trigger-optimized description.** The `description` is the only thing visible in the catalog without loading the file, so it has to answer: *what does this skill do, what domain is it for, and when should the agent reach for it vs skip?* Aim for 2–4 sentences.

   - Bad: `description: "Helmholtz solver"` — what about it? when would I use it?
   - Good: `description: "Python implementation of the Helmholtz algorithm — an iterative alternating-projection method for constructing divergence-free, constant-magnitude 3D vector fields. Used to generate SPAW initial conditions for MHD simulations."`

   Spell out what the skill does NOT cover too — that is what stops an agent from loading the file when the task only superficially matches.
2. **Numbered steps in imperative form.** "Extract the text...", not "You should extract...".
3. **Concrete templates in `assets/`** rather than prose descriptions of desired output format.
4. **Deterministic scripts in `scripts/`** for fragile or repetitive operations — a Python script that always produces the same result is better than prose the LLM has to re-derive every time.
5. **Keep `SKILL.md` focused.** Target under 500 lines. Offload dense content to `references/` and heavy logic to `scripts/`. The body is the procedure; supporting material is a `read` call away.
6. **Structure subdirectories conventionally.** `scripts/` for executables, `references/` for supplementary context (schemas, cheatsheets, worked examples), `assets/` for templates and static files.

## Publishing to GitHub

If a custom skill is worth sharing outside the network — with humans, external collaborators, or as a standalone resource — publish it as its own GitHub repo:

1. Author the skill in `<agent>/.library/custom/<name>/` as usual.
2. Copy to a temp directory: `cp -r .library/custom/<name> /tmp/<name>`.
3. Initialize: `cd /tmp/<name> && git init && git add . && git commit -m "Initial release"`.
4. Add `README.md` (human-facing — see "SKILL.md vs README.md" above).
5. Create the repo: `gh repo create <owner>/<name>-skill --public --source=. --push`.

Do NOT `git init` inside `.library/custom/` directly — it is a subtree of your agent working directory and you would entangle two repos. Always copy out first.

Once published, agents elsewhere can install it with `git clone` into their `.library/custom/` and call `system({"action": "refresh"})`.

## Cleanup / Footprint

Skills live under `.library/intrinsic/`, `.library/custom/`, network shared
skill paths, and any extra paths configured in `init.json`. Intrinsic skills are
runtime-owned; do not delete them. Custom/shared skills are portable procedure
memory: cleanup should usually mean validation, renaming, consolidation, or git
removal through a reviewed PR, not ad-hoc `rm`.

Footprint check (read-only, records the audit):

```bash
python3 - <<'PY'
import json, time
from pathlib import Path
agent = Path.cwd()
roots = [p for p in [agent / ".library" / "custom", agent / ".library" / "intrinsic", agent.parent / ".library_shared"] if p.exists()]
def size(p): return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
rows = [(p, size(p)) for p in roots]
total = sum(s for _, s in rows)
print(f"skill roots: {len(rows)}; bytes: {total}")
for p, s in rows: print(f"{s:>12}  {p}")
log = agent / "logs" / "cleanup.jsonl"; log.parent.mkdir(parents=True, exist_ok=True)
log.open("a", encoding="utf-8").write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": "skills", "dry_run": True, "candidates": len(rows), "bytes": total, "human_approved": False, "summary": "skills footprint audit"}) + "\n")
PY
```

Recommended cadence: after authoring/publishing skills, before recipe export,
and monthly for shared libraries. Destructive cleanup requires a dry-run report,
explicit user consent, and a git commit/PR when the skill root is tracked.
