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

  Covers: directory layout (`.library/{intrinsic,custom}/`), required vs
  optional frontmatter fields, name-collision discipline, when to author
  a skill and when not to, what makes a skill description trigger-friendly,
  the validator's failure modes, and the relationship between the kernel's
  intrinsic skill bundles and your editable `custom/` territory.

  Does NOT cover: the bundled skills themselves — their READMEs and
  SKILL.md files document them. This is meta — how the skills *system*
  works, not what's *inside* it.
version: 1.1.0
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
---

Full instructions in Markdown below...
```

Required frontmatter: `name`, `description`. Optional: `version`, `author`, `tags`.

`tags` is a list of lowercase, hyphenated strings that aid discoverability and (eventually) tier filtering. Three useful axes:

- **Language / runtime**: `python`, `fortran`, `bash`, `node`
- **Domain**: `physics`, `mhd`, `plasma`, `ml`, `web`
- **Type**: `solver`, `workflow`, `reference`, `cheatsheet`

Example: `tags: [python, physics, mhd, solver]`. Tags are best-effort metadata, not load-bearing — the catalog still finds skills without them.

After writing, call `system({"action": "refresh"})` so the skills capability rescans and re-injects the catalog.

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
