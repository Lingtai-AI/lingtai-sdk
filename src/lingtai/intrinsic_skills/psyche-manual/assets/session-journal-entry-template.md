# Session-Journal / Molt-History Entry Template

Use this when writing the molt-history record for a session segment, *before* a
deliberate molt. Write it to
`knowledge/session-journal/<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md`
via `write`/`edit` (the kernel `knowledge` mechanic auto-discovers subdirectories
containing `KNOWLEDGE.md`). Read `<molt-count>` from your resident system
prompt's identity section — "You have undergone N molts since birth" — and use
that N: this entry records the pre-molt segment, written *before* you call
`psyche(context, molt)`. (The tool result afterward reports the next count,
N+1, which belongs to the next segment.) Including it keeps chronology stable
when you molt more than once on the same date: the date alone cannot order two
same-day entries, but the molt count always can.

It is a **journal, not a transcript**: capture what happened and why, point at
where the substance lives, and stop. Reference paths/PRs/message IDs — never
inline secrets or full file contents. After writing it, append one line to the
parent index at `knowledge/session-journal/KNOWLEDGE.md`.

The frontmatter below is the on-disk format; fill every section, writing `None`
rather than omitting one.

> **Required marker (the molt gate):** the frontmatter **must** include
> `type: session-journal` (or, equivalently, `session_journal: true`). The
> kernel molt gate rejects the molt unless this marker is present, the file
> lives at `knowledge/session-journal/<entry>/KNOWLEDGE.md` (a per-segment
> sub-entry, not the parent index), exists, is non-empty UTF-8, and has valid
> YAML frontmatter with `name` and `description`. You pass this file's path to
> `psyche(context, molt, session_journal_path=...)`.

```markdown
---
name: <YYYY-MM-DD>-molt-<molt-count>-<slug>
description: One-sentence hook — what this session segment did. Used in the parent index line.
date: <YYYY-MM-DD>
molt_count: <current molt count, before calling psyche(context, molt)>
type: session-journal
---

## What this segment was about
The original ask and the framing. Why this segment existed.

## Accomplishments
What you completed or moved forward, and the outputs. Who was told, on which channel.

## Decisions and reasoning
The choices made and *why* — especially where an alternative was rejected.

## Artifacts and paths
Files, reports, branches, worktrees, PRs, commits, message IDs that anchor the
work. Use repository paths / URLs / IDs; do not paste secrets or large blobs.

## Open tasks
Work noticed or started but not finished, with the next concrete step for each.

## Collaborators
People/agents involved, their channels, who is waiting on what.

## Gotchas and lessons
Actionable warnings, failed approaches, verification requirements — "run X
before Y" beats "be careful".
```
