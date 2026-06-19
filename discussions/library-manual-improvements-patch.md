# Patch — library-manual: progressive disclosure, README distinction, self-test, description quality

**Date:** 2026-05-01
**Status:** Awaiting human review and application
**Author:** Claude (Opus 4.7), at user's direction
**Origin:** GitHub issue [Lingtai-AI/lingtai#22](https://github.com/Lingtai-AI/lingtai/issues/22) — filed by an agent that authored two production skills (`laps` v6.0.0, `helmholtz` v1.0.0) and reported six concrete gaps in `library-manual`.
**Files touched:**
- `src/lingtai/core/library/manual/SKILL.md` (insert + edit)
- `src/lingtai/core/library/manual/assets/skill-template.md` (small additions: tags placeholder hint, decision-tree skeleton)

## Why

The current `library-manual` is mechanically correct but pedagogically thin. It tells agents *that* a skill should be focused (target under 500 lines) and *that* the description should be "trigger-optimized," but it doesn't show agents the structural pattern that makes those goals achievable, nor does it warn them about the failure modes the structural validator can't catch.

The agent that filed issue #22 hit four concrete gaps while authoring real skills:

1. **No structural pattern** — defaulted to a monolithic SKILL.md, then refactored to a routing-hub-plus-`reference/*.md` shape they had to invent themselves.
2. **No README.md mention** — forgot it entirely when publishing to GitHub.
3. **No content self-test** — the validator verified frontmatter and file references but did not catch fabricated paths, wrong API signatures, or stale method references. The agent caught those by walking through the skill as if it were a fresh agent.
4. **Description-field guidance too abstract** — "trigger-optimized" needed a concrete example.

Suggestions 5 and 6 (tags conventions, GitHub publishing) are smaller but worth folding into the same pass.

The fix is **additive** — no existing guidance is wrong, it's just incomplete. I propose:

- One new section: **"Recommended structure for complex skills"** (suggestion 1).
- One new section: **"SKILL.md vs README.md"** (suggestion 2).
- One new section: **"Self-test before publishing"** (suggestion 3).
- Expansion of the `description` bullet under "Writing a good skill" (suggestion 4) — replace one short bullet with a worked good/bad example.
- Expansion of the existing "Authoring a new skill" frontmatter block to mention `tags` conventions (suggestion 5).
- One new section: **"Publishing to GitHub"** (suggestion 6).
- Template (`skill-template.md`): minimal additions — a tags placeholder hint and an optional decision-tree skeleton in a comment.

Existing sections (on-disk layout, catalog, pinning, validator, name collision, health check, when to create a skill) are **unchanged**.

---

## Change shape — `src/lingtai/core/library/manual/SKILL.md`

### Edit 1 — frontmatter `description` (lines 3–33): no change

Frontmatter description is fine. The catalog entry already conveys the manual's scope.

### Edit 2 — extend "Authoring a new skill" frontmatter mention (line 94)

**Old:**

```
Required frontmatter: `name`, `description`. Optional: `version`, `author`, `tags`.
```

**New:**

```
Required frontmatter: `name`, `description`. Optional: `version`, `author`, `tags`.

For `tags`, prefer lowercase-hyphenated values along three axes that help agents and humans discover the skill: **language/runtime** (`python`, `fortran`, `bash`), **domain** (`physics`, `mhd`, `email`), and **type** (`solver`, `workflow`, `reference`, `manual`). Tags are not currently used for catalog filtering, but writing them now makes future filtering free.
```

### Edit 3 — insert new section "Recommended structure for complex skills" *before* "Validating before publishing" (between current line 108 and line 109)

Add this new subsection, immediately after the "Starting from the template" subsection (which currently ends at line 107 with `…there is a note at the top of the template reminding you of this.`):

```
### Recommended structure for complex skills

For skills that cover more than one topic — e.g. a domain library, a tool with installation + API + troubleshooting, or a reference manual — adopt a **two-level progressive disclosure** layout:

```
<skill-name>/
├── SKILL.md              ← Routing hub: decision tree + quick start + at-a-glance table
├── README.md             ← GitHub-facing description (see "SKILL.md vs README.md" below)
└── reference/
    ├── topic-a.md        ← Self-contained deep-dive, loaded on demand
    ├── topic-b.md
    └── ...
```

`SKILL.md` is a **decision tree** — typically 150–250 lines. It points at one of several `reference/*.md` files based on what the agent is actually trying to do. Each reference file covers one topic in depth (100–300 lines).

The decision-tree section itself is the load-bearing part. A working pattern from skills authored in the wild:

```markdown
## Quick Decision Tree

"What do I need?"
│
├─ 🆕 First time — what is X and why do I need it?
│  └─ Read: reference/algorithm.md
│
├─ 🔧 I want to use the tool — install, configure, run
│  ├─ Installation and first run
│  │  └─ Read: reference/getting-started.md
│  └─ Full API reference
│     └─ Read: reference/api-reference.md
│
└─ ❓ Something is wrong — diverging, NaN, slow, bad results
   └─ Read: reference/troubleshooting.md
```

**Why this works:** the agent loads `SKILL.md` (~150 lines) plus one `reference/*.md` (~150 lines) instead of a 1000-line monolith. Context cost stays bounded. Maintenance is local — fixing one topic doesn't touch the others.

**When NOT to use this pattern:** simple skills, single-API wrappers, linear checklists. A 50-line SKILL.md that *is* the procedure does not need a `reference/` directory. The split adds cognitive overhead that is only worth paying when the skill genuinely covers multiple distinguishable paths.

Reference implementations:
- https://github.com/huangzesen/laps-skill (LAPS — Hall-MHD simulation code, 7 reference files)
- https://github.com/huangzesen/helmholtz-skill (Helmholtz solver, 7 reference files)
```

### Edit 4 — insert new section "SKILL.md vs README.md" *immediately after* the new "Recommended structure for complex skills" section (i.e. before "Validating before publishing")

```
### SKILL.md vs README.md

When a skill is also a GitHub repository (or otherwise has a human-facing surface), it needs both files. They serve different audiences:

| File | Audience | Loaded by | Voice |
|------|----------|-----------|-------|
| `SKILL.md` | LingTai agents | `library` capability (system-prompt catalog) | Imperative, terse, decision-tree-first |
| `README.md` | Humans browsing GitHub | Not loaded by any agent capability | Descriptive, marketing-aware, screenshots welcome |

`SKILL.md` answers "I'm an agent — what do I do, right now?" — its first job is routing. `README.md` answers "I'm a human — what is this and is it for me?" — its first job is positioning. Don't try to merge them; the constraints pull in different directions.

Common oversight: forgetting `README.md` when publishing to GitHub. The skill works fine for agents but lands on the GitHub repo page with no description. If you intend to publish externally, write `README.md` at the same time you write `SKILL.md`.
```

### Edit 5 — replace the description bullet in "Writing a good skill" (current line 176)

The "Writing a good skill" section is currently 6 numbered items at lines 176–181. Replace item **1** with an expanded version:

**Old (line 176):**

```
1. **Trigger-optimized description.** The `description` is the only thing visible in the catalog without loading the file. Say what the skill does AND what it does not cover, so the agent knows when to reach for it and when to skip past.
```

**New:**

```
1. **Trigger-optimized description.** The `description` is the only thing visible in `<available_skills>` without loading the file. It must answer three questions in 2–4 sentences: *What does this skill do? What domain or technology is it for? When should an agent reach for it (vs. skip past)?*

   ✗ Bad: `description: "Helmholtz solver"`
     — what about it? When would I use it? An agent reading this in the catalog has no signal.

   ✓ Good: `description: "Python implementation of the Helmholtz algorithm — an iterative alternating-projection method for constructing divergence-free, constant-magnitude 3D vector fields. Used to generate SPAW initial conditions for MHD simulations. Progressive disclosure — start here for routing, drill into reference/ for depth."`
     — answers what (algorithm + implementation language), domain (MHD physics), and when (you need divergence-free initial conditions).

   Saying what the skill *does NOT* cover is also valuable: it teaches the agent when to skip past and look for a different skill instead.
```

### Edit 6 — insert new section "Self-test before publishing" *after* the existing "Validating before publishing" section (between current line 118 and line 119, just before "Publishing to the network-shared library")

```
## Self-test before publishing

The validator catches structural failures (missing frontmatter, broken file references, stale `[PLACEHOLDER]` slots) but it cannot catch **content errors**. The most common dangerous mistakes — the ones that turn a skill into a hallucination trap — are:

1. **Fictional file paths** — the skill claims a file exists (e.g. `helmholtz*.f90`) that isn't actually in the codebase.
2. **Wrong API signatures** — the skill documents parameter names, defaults, or return values that don't match what the code actually does.
3. **Stale method references** — the skill references functions, classes, or flags from a previous version of the code that have since been renamed or removed.

After writing, walk through your skill as if you were a fresh agent encountering it for the first time:

1. **Decision-tree test.** Start at `SKILL.md`'s decision tree. Follow each branch — does every reference file actually exist? Does the content the branch points at actually answer the question the branch poses?
2. **Assertion test.** For every concrete claim in your skill (file paths, function names, parameter names, default values, command flags), `grep` or `read` the actual source. Do NOT rely on memory of the codebase. Confirm each assertion against ground truth.
3. **Regression test.** Fix any discrepancies, then repeat step 2 — the fix may have introduced new claims that also need verification.

This three-step pass takes 5–10 minutes for a typical skill and catches errors that no validator can find.
```

### Edit 7 — insert new section "Publishing to GitHub" *after* the existing "Publishing to the network-shared library" section (between current line 128 and line 129, before "Admin curation of `.library_shared/`")

```
## Publishing to GitHub

If a skill is generally useful — not just to your network — publish it as a standalone GitHub repository so humans and other agents outside the network can discover it.

A working flow used by `laps-skill` and `helmholtz-skill`:

1. Author the skill in `.library/custom/<name>/` or `.library_shared/custom/<name>/`.
2. Copy the skill directory to a temp location (so the GitHub repo gets a clean tree without `.library` ancestry):
   ```
   bash({"command": "cp -r .library/custom/<name> /tmp/<name>-skill"})
   ```
3. Add a `README.md` at the temp location (see "SKILL.md vs README.md" above). The README is the human-facing pitch — what the skill is, who it's for, and a link to the source repository it documents (if any).
4. Initialize git, create the GitHub repo, and push:
   ```
   bash({"command": "cd /tmp/<name>-skill && git init && git add -A && git commit -m 'Initial release' && gh repo create <name>-skill --public --source=. --push"})
   ```
5. (Optional) Add the GitHub URL to the skill's frontmatter `description` so other agents discovering it via your `.library_shared/` know where to find updates.

The skill in your library and the GitHub repo are now two separate copies. Decide which is the source of truth and update the other when the canonical one changes.
```

---

## Change shape — `src/lingtai/core/library/manual/assets/skill-template.md`

Two small additions:

### Template edit 1 — replace the existing `tags` placeholder line (line 5)

**Old:**

```
tags: [[optional, tags]]      # Optional: search/categorization tags
```

**New:**

```
tags:                         # Optional: lowercase-hyphenated; axes = language/runtime, domain, type
  - [language-or-runtime]     # e.g. python, fortran, bash
  - [domain]                  # e.g. physics, mhd, email
  - [type]                    # e.g. solver, workflow, reference, manual
```

### Template edit 2 — append a new optional section to the template, between current "Procedure" (line 23) and "What to expect" (line 25)

Insert this block immediately after the "Procedure" section (the `[For code/executable skills...]` paragraph at line 23):

```markdown
## Quick Decision Tree

[For complex skills with multiple paths, replace this section with a decision tree
pointing at `reference/*.md` files. See library-manual.md → "Recommended structure
for complex skills" for the pattern. Delete this section entirely for simple skills.]

```
"What do I need?"
│
├─ 🆕 [First-time path]
│  └─ Read: reference/[topic-a].md
│
├─ 🔧 [Common usage path]
│  └─ Read: reference/[topic-b].md
│
└─ ❓ [Troubleshooting path]
   └─ Read: reference/[troubleshooting].md
```
```

---

## Verification checklist

Before applying:

1. The seven section anchors above (`Authoring a new skill`, `Starting from the template`, `Validating before publishing`, `Writing a good skill` item 1, `Publishing to the network-shared library`, `Admin curation of .library_shared/`) all exist in the current `SKILL.md`.
2. The template still parses as valid YAML in its frontmatter after the `tags` rewrite (the new form is a YAML list, which is valid; the old `[[optional, tags]]` is a placeholder, not real syntax).
3. The "Quick Decision Tree" section addition to the template is wrapped in clear "[delete for simple skills]" guidance so it doesn't confuse first-time skill authors.

After applying, smoke test:

```bash
~/.lingtai-tui/runtime/venv/bin/python -c "
import re
manual = open('src/lingtai/core/library/manual/SKILL.md').read()
template = open('src/lingtai/core/library/manual/assets/skill-template.md').read()

# Manual: all six new section markers present
for marker in (
    'Recommended structure for complex skills',
    'SKILL.md vs README.md',
    'Self-test before publishing',
    'Publishing to GitHub',
    'lowercase-hyphenated values along three axes',
    'Bad: \`description:',
):
    assert marker in manual, f'manual missing: {marker}'
print('manual: ok — all 6 markers present')

# Template: tags rewrite + decision tree
assert 'language-or-runtime' in template, 'template missing tags axes hint'
assert 'Quick Decision Tree' in template, 'template missing decision tree skeleton'
print('template: ok')
"
```

```bash
# Validator still passes on the bundled template (it should — we only added optional content):
~/.lingtai-tui/runtime/venv/bin/python \
  src/lingtai/core/library/manual/scripts/validate.py \
  src/lingtai/core/library/manual/
```

## Companion changes

None. Unlike the pad patch (which split between kernel i18n and lingtai procedures.md), this patch is fully self-contained in the kernel — `library-manual` *is* the agent-facing teaching surface for skill authoring, so improvements land there directly. No procedures.md mirror is needed.

After applying, the issue [Lingtai-AI/lingtai#22](https://github.com/Lingtai-AI/lingtai/issues/22) can be closed with a reference to the kernel commit.
