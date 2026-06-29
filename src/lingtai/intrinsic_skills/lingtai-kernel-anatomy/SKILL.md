---
name: lingtai-kernel-anatomy
description: >
  The canonical convention for `ANATOMY.md` files in the LingTai kernel — what
  an anatomy is, why it exists, how to read it, how to write one, and how to
  maintain it across refactors. The kernel itself is mapped by a tree of
  `ANATOMY.md` files rooted at `src/lingtai_kernel/ANATOMY.md`. This skill is
  the convention; those files are the content.

  Reach for this skill when:
    - You are about to read kernel code and want to navigate by structure
      instead of grep — descend the tree starting at the kernel-root anatomy.
    - You are about to write or update an `ANATOMY.md` and need to know the
      template, the citation rules, and the maintenance discipline.
    - Something feels off about your own behavior and you want to compare it
      against what the code actually does — descend the tree to the relevant
      folder, then read the cited code.

  How to use:
    1. Read this file once — you are learning the convention.
    2. Open `src/lingtai_kernel/ANATOMY.md` — that is the kernel-root anatomy
       (which itself is just-an-anatomy of `src/lingtai_kernel/`). Use its
       Components and Composition sections to find the subfolder whose
       anatomy holds your question.
    3. Descend. At each layer the anatomy points either further down the
       tree or directly at code via `file:line` citations.
    4. Read the cited code. The anatomy is the navigation aid; the code is
       the truth.
    5. If anatomy disagreed with code, update the anatomy before you leave
       the file. Reading and maintaining are the same act.
version: 0.1.0
last_changed_at: "2026-06-26T14:07:06-07:00"
---

# LingTai Kernel Anatomy — the Convention

This skill is the canonical convention for `ANATOMY.md` files in the LingTai kernel. It is read by every agent — LingTai or coding — that wants to navigate or modify the kernel. It is not the content; the content lives next to the code in `ANATOMY.md` files distributed across the kernel source tree.

## What an `ANATOMY.md` is

An `ANATOMY.md` file is the **structural description of one folder of code**, written for an agent reader, sitting next to the code it describes.

It is **not**:

- A user manual or how-to guide (those are skills, manuals, tutorials).
- An API contract (those are tool schemas).
- A design or rationale document (those live in issues, PR descriptions, durable docs, or commit messages).
- A test specification (those are test files).

It **is**: a code-cited map of *what is in this folder, how the parts connect, and where state lives.* Every structural claim is grounded in a `file:line` reference into the code. If a claim cannot point at a line that says what it claims, the claim does not belong in anatomy.

A folder gets an `ANATOMY.md` when **a competent agent could do useful reasoning about the folder as a unit without first reading its siblings.** Trivial leaves (a single dataclass module, a one-function helper) do not. The kernel-root anatomy is the only file that holds a complete child enumeration; every other anatomy maps just its own folder.

## The 6-section template

Every `ANATOMY.md` — including the kernel-root anatomy — follows the same shape:

1. **What this is** — one paragraph naming the concept this folder embodies.
2. **Components** — files / functions / classes here, with `file:line` citations and one-line purposes.
3. **Connections** — what calls in, what this folder calls out, what data flows through.
4. **Composition** — parent folder, subfolders (each linked to its own `ANATOMY.md`), siblings if structurally relevant.
5. **State** — persistent state this folder writes (files, schema versions), ephemeral state it manages.
6. **Notes** — bounded section for rationale, history, gotchas not visible in code.

~80 lines is the cap; less is better. If a folder needs more, it is probably two folders.

## Use anatomy as navigator, not grep

You are an agent. Reading 200 lines of code is one tool call; greping a symbol gives you 50 hits each costing their own evaluation. For **structural** questions (what shape is this part of the kernel, where does behavior X live, what does Y connect to) descend `ANATOMY.md` files top-down — three reads will usually take you deeper than fifty grep hits. For **enumeration** questions (every callsite of this function, every file matching this pattern) grep is still right.

| Question type | Tool |
|---|---|
| Structural | Descend the anatomy tree |
| Enumeration | grep |

The descent: start at `src/lingtai_kernel/ANATOMY.md`, read its Components and Composition, pick the subfolder whose territory contains your question, open its anatomy, repeat. At each layer the anatomy will tell you whether to descend further or read the cited code directly.

## Writing checklist

When you write or update an `ANATOMY.md`, every one of these must be true before you commit. They exist because we have already seen each one fail in practice.

- **Every named symbol in Components has a `file:line` citation.** "loads snapshots (`_load_snapshot_interface`)" is not enough; "loads snapshots (`consultation.py:147`)" is. Without citations, the next agent grepping for the symbol gains nothing from the anatomy.
- **Citations are line ranges, not paragraphs.** Prefer `file.py:123-156` over a vague "see file.py". Prefer single-line citations only for one-line landmarks (constants, single-line helpers).
- **Every citation has been verified.** Open the cited line. Confirm it still says what the anatomy claims. Citations rot fastest after refactors.
- **Cross-references between anatomies use kernel-root-relative paths.** `src/lingtai_kernel/intrinsics/soul/ANATOMY.md`, not `./ANATOMY.md` or `intrinsics/soul/ANATOMY.md`. The root is the only stable reference frame.
- **Cross-references are sparse and one-directional.** Cite parent and structural neighbors only — do not enumerate downstream callers (that's a grep question).
- **No leaf stubs.** Empty placeholder anatomies are clutter. A missing `ANATOMY.md` is an honest signal that the folder hasn't been mapped yet.
- **No paraphrase.** Anatomy adds shape and connections, not summary. If the code's good naming already says what you're about to write, don't write it.

## Maintenance is part of reading

Every agent that reads anatomy is also a maintainer. The contract:

- **Code matches anatomy:** read on, no action.
- **Code disagrees with anatomy:** the code is almost always right. Update the anatomy to match before you leave the file. If you believe the code itself is wrong, report the bug — and note that anatomy and code disagreed, because that disagreement is itself a clue.
- **Anatomy missing or empty:** if you understood the folder well enough to do your task, write the anatomy. Components, connections, state. Use the writing checklist above.

## When a code change requires anatomy updates

The same-commit rule is about structural drift, not busywork. Update relevant `ANATOMY.md` files when a change does any of these:

- Moves, renames, splits, merges, or deletes a file, function, class, or package cited by anatomy.
- Changes which module owns a behavior, which module calls another, or which folder is the right entry point for a structural question.
- Adds, removes, or changes persistent state: files written, schema versions, manifest fields, logs, snapshots, queues, locks.
- Changes lifecycle, message-flow, prompt-routing, tool-routing, or other cross-module connections that anatomy describes.
- Creates a new folder that a competent agent can reason about as a unit.

Usually no anatomy update is required for local implementation fixes, prompt wording changes, constant value tweaks, test-only edits, formatting, or comments — unless anatomy cites or describes that exact behavior. When unsure, search for citations of the touched filename and verify them; if the prose still points future agents to the right place, leave it alone.

## Who maintains anatomy

There are two kinds of agent that interact with this convention, and they have different obligations:

**Coding agents** (Claude Code, Codex CLI, any agent that edits files and creates commits): you MUST update the relevant `ANATOMY.md` files in the **same commit** as the code change. If a commit moves a function, renames a file, splits a module, or changes a connection between folders, the anatomies that cite that code are part of the diff. Do not split anatomy updates into a follow-up commit — drift starts the moment the code change lands without its anatomy update. Git history is the audit trail; anatomy files do not need their own version-history sections.

**LingTai agents** (the creatures running inside `.lingtai/`): you generally do NOT modify kernel source directly — you propose patches, the human applies them. Your role with anatomy is **to report drift as issues**. When you read anatomy and notice it disagrees with the code, mail the human, or open/write a structured issue or PR proposal naming the specific citation that rotted and the correct line. Do not silently fix anatomy in your own working copy without surfacing the drift, because the value of your read-pass is the signal that the drift exists.

The asymmetry exists because coding agents have commit authority and LingTai agents have observation authority. Both are needed; neither replaces the other.

## Citation rot during refactors

The most common drift mode is **citation rot after a refactor**. When code moves between files, anatomies that cite the old file rot silently — the prose still reads correctly, but the citations point at a line that no longer exists or contains different code.

The mechanical rule:

> After any commit that touches `git diff --name-only`, search every `ANATOMY.md` for citations of every touched filename and verify each one.

Concretely, if a commit moves `intrinsics/soul.py` → `intrinsics/soul/{config,consultation,inquiry}.py`, then before completing the commit:

1. `grep -rn "intrinsics/soul\.py:" src/lingtai_kernel/**/ANATOMY.md src/lingtai_kernel/ANATOMY.md`
2. Update every match to the new file location and verified line number.
3. Repeat for any other file the refactor moved.

For cheap mechanical checking, run the advisory drift checker before commit:

```bash
python tools/check_anatomy_drift.py            # report drift, exit 0
python tools/check_anatomy_drift.py --check    # exit 1 if any drift (CI/pre-commit)
```

`tools/check_anatomy_drift.py` scans every `ANATOMY.md` under `src/` and flags
**citation rot**: a `file.py:line` citation whose target is missing or whose
line is past the end of the file. It resolves cited paths by searching upward
from each anatomy's directory, so kernel-root-relative citations work.

This only catches missing files and out-of-range lines. It does **not** prove
semantic correctness — a citation can be in-range yet point at the wrong code —
so an agent still has to open the cited line and confirm the claim. The checker
is the cheap first pass, not the verification.

This is the discipline demonstrated by the historical soul-flow tool-refusal proposal and the soul package refactor (`ffe42d4`). The first round of citation rot happened because the rule was implicit; making it explicit here is part of the convention.

## The kernel-root anatomy is just an anatomy

The kernel-root `ANATOMY.md` (at `src/lingtai_kernel/ANATOMY.md`) follows the same 6-section template as every other anatomy. It happens to enumerate every direct child of the kernel root in its Components and Composition sections — that's a property of being at the top of the tree, not a special role. There are no "doorways" or "entrances": there is the convention (this skill) and there is the tree of anatomies. The kernel-root anatomy is the top of the tree. That is all.

## When the convention exposes structural pressure

If a single file is large enough to need its own anatomy, that is a refactor signal — not a license to write per-file anatomies. The convention's first useful side effect is that it reveals where a folder's organizational grain doesn't match its conceptual grain. The right response is "make the file smaller (split into a package)" not "invent a parallel doc system that summarizes a too-large file." This is how `intrinsics/soul.py` (1056 lines, four concerns) became `intrinsics/soul/` with four sub-modules and a sub-anatomy.

## Relationship to other skills

- **`lingtai-anatomy`** — describes the LingTai *system* as a user experiences it: TUI flows, presets, init.jsonc, runtime layout under `~/.lingtai-tui/`. Lives outside the kernel. If your question is "how does my init.jsonc get there," start there.
- **Per-tool manuals** (`daemon-manual`, `mcp-manual`, `skills-manual`, ...) — operational how-to for invoking specific tools. If your question is "how do I use X," start there.
- **`lingtai-kernel-anatomy` (this skill)** — the convention behind the kernel anatomy tree. If your question is "what is X actually doing inside me, where does it live in my code," read this once to know the convention, then descend the kernel anatomy tree.

The three skills are layered. Manuals tell you how to act. Umbrella anatomy tells you about the world you live in. Kernel anatomy tells you about yourself.
