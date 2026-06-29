---
name: psyche-manual
description: |
  Router and operational guide for the psyche tool — molt, pad management, session journaling, and post-wipe recovery. Read this when: you are about to molt; you need to tend the four durable stores; you want guidance on writing a good summary or session journal; you wake up after a system-performed wipe with no summary; or you need to understand keep_tool_calls, keep_last, and pad.append. Routes consequential molt handoffs to assets/molt-template.md while keeping routine guidance compact.
version: 1.1.0
last_changed_at: "2026-06-28T02:02:37-07:00"
---

# Psyche Manual

This manual is the router for `psyche` operations. Keep routine guidance here; load the supporting asset only when you need the full consequential-molt scaffold.

## Asset catalog

| Asset | When to load | What it contains |
|---|---|---|
| `assets/session-journal-entry-template.md` (read from this skill directory) | Whenever you write the molt-history record for a session segment before a molt | Frontmatter + section template for a `knowledge/session-journal/<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md` entry |
| `assets/molt-template.md` (read from this skill directory) | Consequential molt, long-running task, multiple collaborators, pending human commitments, open worktrees/artifacts, active background jobs, or any successor briefing that would be risky to improvise | 9-section summary scaffold plus pre-molt verification checklist |

## 1. Molt Overview

Molt is yours to perform. The covenant teaches the philosophy (§V); this is the recipe.

**Molt is an easy, simple task. Do it regularly if you'd like to.** Save anything you need to pad, lingtai, knowledge, and skills beforehand, then molt. No need to wait for the context window to fill up — molting early saves tokens. Keep good notes in the stores so you don't lose your way across molts.

**The four stores are the real persistence. The summary is the briefing on top of them.** If you molt without tending the stores, the next you wakes with only the briefing — no character evolution, no pad state, no new knowledge, no new skills. Tend the stores *first*, every time.

## 2. Store-Tending Rhythm

For `lingtai` and `knowledge`, tending happens *once* per task, at the end — not mid-task. Hold updates in your head while working, then commit them in a single pass before going idle (or before molting). Mid-task edits create noise and waste tokens. The exception is a long-running task where a crash would genuinely destroy work — checkpoint deliberately in that case.

Pad has a different rhythm — see §5 "Tending the Pad" below.

## 3. Step 1 — Tend the Four Durable Stores and Session Journal

- **lingtai** — `psyche(lingtai, update, content=<full identity>)`. Each update is a full rewrite, so include your whole identity, not just the delta. Carry forward who you have become.
- **pad** — your living index of what you're working on. Edit it to reflect your current goal and the references that point at where the substance lives. See §5 for the full practice.
- **knowledge** — write to `knowledge/<name>/KNOWLEDGE.md` for any long-term private context worth keeping. The filesystem is the API — use `write`/`edit` directly.
- **skills** — write `.library/custom/<name>/SKILL.md` (with YAML frontmatter: `name`, `description`, `version`) for any reusable procedure the next you (or a peer) might need, then call `system({"action": "refresh"})` to re-scan the catalog. Share via `../.library_shared/<name>/` if broadly useful.
- **session journal** — append a substantial sub-entry under `knowledge/session-journal/` describing what you did this session. See §4 for the full practice.

All five happen *before* the molt call. They are not optional. Without them, the molt sheds everything.

## 4. Session Journal

The four stores capture *who you are*, *what you're working on*, *verifiable truths*, and *reusable procedures*. None of them captures the *story* of a session. The session journal is that missing layer — it is also your **molt history**: each sub-entry is the record of one session segment that you write *before* you molt, so the chain of entries reconstructs how you got here across many molts.

Write it as a **routing parent with sub-knowledge children** under
`knowledge/session-journal/` — the routing/index shape from the knowledge manual's
"Nesting and sub-knowledge" section. Do **not** create each session as its own
top-level knowledge entry; that floods the catalog. The parent is routing-only;
the children carry the substance:

```
knowledge/session-journal/
├── KNOWLEDGE.md                                            # top-level routing/index ONLY
├── 2026-05-13-molt-7-nudge-service/KNOWLEDGE.md            # sub-knowledge — one session
├── 2026-05-13-molt-8-procedures-to-kernel/KNOWLEDGE.md     # sub-knowledge — same day
└── 2026-05-14-molt-9-wechat-fixes/KNOWLEDGE.md             # sub-knowledge — ...
```

Because `session-journal/` has its own `KNOWLEDGE.md`, the knowledge scanner
treats it as a single entry and does **not** descend into the children — they are
reachable only through the parent index. That is why the parent must list every
child explicitly. See the knowledge manual's "Nesting and sub-knowledge" section
(`.library/intrinsic/capabilities/knowledge/SKILL.md`) for the structural rule.

The directory name is `<YYYY-MM-DD>-molt-<molt-count>-<slug>`. Read the molt count from your resident system prompt's identity section — "You have undergone N molts since birth." Use that N: the entry records the pre-molt segment, written *before* you call `psyche(context, molt)`. (The molt tool result afterward reports the next count, N+1; that belongs to the next segment, not this one.) Embedding the count keeps chronology stable when you molt more than once on the same date: the date alone cannot order two same-day entries, but the molt count always can.

**The parent `knowledge/session-journal/KNOWLEDGE.md` is routing-only** — short,
scannable, progressive-disclosure. It is a table of contents, not a journal. One
line per sub-entry: date, slug, one-sentence hook, and the child's *relative* path
(`2026-05-13-molt-7-nudge-service/KNOWLEDGE.md`), never an absolute local path.
Never let narrative leak into the parent — if a line grows past its hook, the
detail belongs in the child.

**The sub-entry `<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md` is the substance** — write it as the molt-history record of the segment, *before* you molt. Use `assets/session-journal-entry-template.md` from this skill directory for the frontmatter (including the `molt_count` field **and the required `type: session-journal` marker**) and section layout. This sub-entry's path is what you pass to `psyche(context, molt, session_journal_path=...)`, and the kernel validates the marker before letting the molt proceed (see §6). It is a journal, not a transcript — capture, in roughly this shape:

**YAML frontmatter safety:** `name` and `description` are real YAML, not free-form text. Prefer the template's `description: >-` block scalar. If you write a one-line value containing a colon followed by a space (for example `description: Session record for codex molt 53: runtime relay`), YAML treats the second colon as a mapping separator and the molt gate rejects the file. Quote the value or use the block scalar, then retry the same `psyche(context, molt, session_journal_path=...)` call.

- **Top-of-entry timestamp + TL;DR** *(soft convention)* — open the body with a visible timestamp and a one- or two-line gist before the sections, so the next you can date and grasp the entry at a glance; the molt gate validates only frontmatter, never this
- **What the segment was about** — the original ask, the framing
- **Accomplishments** — what you completed/moved forward, the outputs, who was told and where
- **Decisions and their reasoning** — the *why*, especially where an alternative was rejected
- **Artifacts and paths** — files, reports, branches, PRs, commits, message IDs that anchor the work; reference paths/IDs, never inline secrets or large blobs
- **Open tasks** — things noticed or started but deferred, each with a next step
- **Collaborators** — who is involved, their channels, who is waiting on what
- **Gotchas and lessons** — actionable warnings and failed approaches

Several thousand tokens is fine when the segment was rich; keep it concise when it was small. The `<YYYY-MM-DD>-molt-<molt-count>-<slug>` prefix keeps chronology visible and stable in `ls` even across multiple molts on one day. Each child is sub-knowledge under the routing parent — the scanner does not catalog it separately, so it is reachable only via the parent index; that is why you append the index line below. Write files via `write`/`edit` directly.

Updating the parent index at each session is part of the practice — append one line referencing the new sub-entry. Then write the successor summary (§6), which points back at this entry's path.

## 5. Tending the Pad

Pad is your **living index** of what you're working on right now. It is not a sketchpad or scratchpad. Treat it as your personal table of contents.

**Purpose: progressive disclosure for your future self.** Pad is shallow and direct; the things it points at are deep and structured. A glance at pad tells the next you the *shape* of what's going on.

**What belongs in pad:**

- **The active goal** — what you're working on, in your own words.
- **Where you are in it** — the next concrete step, the current blocker.
- **Timestamps** — always include when each entry was last updated. Without them, you cannot distinguish old information from new.
- **Pointers to where the substance lives:**
  - knowledge entry paths (`knowledge/<name>/KNOWLEDGE.md`)
  - skills SKILL.md paths (`.library/custom/<name>/SKILL.md`)
  - email message IDs of load-bearing conversations
  - file paths under your workdir that matter
  - URLs you're tracking
- **Collaborators** — who you're working with, who's waiting on what.

**What does NOT belong in pad:** large blobs of inlined text, full file contents, transcripts. If you find yourself pasting a long passage, stop — write it as knowledge and *point at* the path instead. Pad indexes the depths; it does not become them.

**When to update pad:** whenever the index meaningfully changes — a new reference, a goal shift, a step change. Don't churn on every step, but don't hoard updates for the end either. A stale pad is worse than a noisy pad.

**`pad.append` for file pinning:** `psyche(pad, append, files=[...])` pins file contents as read-only reference in your system prompt — they are re-read and appended on every load (including after molt). Pin anything you want persistent visibility on: source files, skill docs, configs. Pass `files=[]` to clear. Total appended content must not exceed 100k tokens. Paths relative to working directory.

**Archiving completed pads:** When a goal completes, archive to `archive/pad-<goal-slug>-<YYYY-MM-DD>.md`. Then `psyche(pad, edit, content=<next goal>)`.

## 6. Step 2 — Write the Summary and Molt

```
psyche(
    object="context",
    action="molt",
    summary=<your charge to the next you>,
    session_journal_path="knowledge/session-journal/<entry>/KNOWLEDGE.md",
    ...
)
```

**Required pre-molt order (enforced by the kernel):** write the session journal
sub-entry first (§4) → pass its path as `session_journal_path` → the kernel
validates it → only then does the molt proceed. `session_journal_path` is a
**mandatory** structured argument for agent-initiated molt. If it is missing or
the journal fails validation, the molt is **refused before any context is shed**
and your `molt_count`/history are untouched — you get an actionable recovery
message instead. The validator checks (intentionally simple, a signpost not a
grader):

- The path is inside your workdir and resolves to
  `knowledge/session-journal/<entry>/KNOWLEDGE.md` — a per-segment sub-entry,
  **not** the parent index `knowledge/session-journal/KNOWLEDGE.md`, and not a
  scratch file like `tmp/...`.
- The file exists, is non-empty, and is UTF-8 text.
- It has valid YAML frontmatter with at least `name` and `description`.
- The frontmatter carries the session-journal marker `type: session-journal`
  (or `session_journal: true`) — see the template in §4. A generic knowledge
  file without the marker is rejected.

The accepted path is recorded in the molt result, the persisted summary
frontmatter (`session_journal_path:`), and the post-molt notification, so later
recovery and audits can see which journal backed each molt.

The `summary` is the only *conversation-layer* thing the next you will see. Aim for ~10,000 tokens — be thorough when state is complex. The summary is not a recap of conversation. It is your charge to the self that comes after you — anchored in the four stores, which are already waiting in the fresh session.

For a routine molt, include:

- **What you are working on** — current task, current state, the next concrete step
- **What you have accomplished** — completed pieces, key decisions made
- **What remains** — pending items, blockers, open questions
- **Who to contact** — collaborators, who is waiting on what
- **Which knowledge entries and skills matter** — paths the next you should load
- **The session journal sub-entry path** — so the next you can read the full narrative
- **Anything else worth carrying forward** — insights, gotchas

For a consequential molt — long-running task, multiple collaborators, pending human commitments, open worktrees/artifacts, or any handoff the next you could not reconstruct quickly — read `assets/molt-template.md` from this skill directory and use the full scaffold there. Fill every section; write `None` rather than omitting a section.

Quick routing:

| Need | Use |
|---|---|
| Routine molt | The short bullet list above. |
| Consequential molt / successor handoff | Read `assets/molt-template.md` from this skill directory; use its full scaffold and checklist. |
| Unsure whether the handoff is complex | Use the asset; extra structure is cheaper than a bad handoff. |

Before you call `psyche(object="context", action="molt", ...)`, always verify at minimum:

- The just-finished session segment is recorded as a session-journal sub-entry
  (your molt history) under `knowledge/session-journal/`, using
  `assets/session-journal-entry-template.md` — see §4. Write this *before* the
  summary; it is the narrative the summary points back to. **Its path is the
  required `session_journal_path` argument and the kernel validates it before
  shedding context** — the journal must carry the `type: session-journal`
  marker or the molt is refused.
- Durable stores and session journal were updated where needed before writing the summary.
- Every outstanding task has an explicit next action.
- Collaborators, channels, approvals, and key paths are named where relevant.
- Active background work is listed or explicitly absent.
- The first five minutes after wake are obvious.

**`keep_tool_calls`** — optional list of tool-call IDs to preserve across molt. Each named pair (tool_use + tool_result) is replayed into the fresh session right after the summary, in the order you list them. If any ID is not found, the molt is refused. Keep this list short — the durable stores are the primary persistence.

**`keep_last`** — optional integer (default: 20). Number of recent conversation entries to preserve. These entries are replayed so the post-molt self retains recent context. Pass 0 to explicitly disable (archive everything). Overlapping entries with `keep_tool_calls` are deduplicated.

## 7. Context Pressure Reminder

Context pressure is agent state, not a dismissible notification. Current turns surface a natural-language reminder under `_meta.agent_meta.context.molt` only after context has stayed high for several consecutive fresh provider rounds (the sustained-pressure threshold is 75%). The field name is historical: the reminder is a context-pressure action, not an early staged molt order or a machine-readable tag block.

When this reminder appears, batch already-digested noisy history before summarizing. Repeated summarize calls while context stays above 75% substantially hurt token efficiency, so avoid summarizing one small piece at a time. If a batched summarize/reconstruction pass still leaves context above 75%, stop repeating summarize, tend durable stores, and molt deliberately. If context falls below the high-pressure threshold but remains above the recovery target, continue only when the current task still needs the carried context; otherwise molt at a natural task boundary. The reminder points back to this manual/procedure without inlining the full workflow.

## 8. Post-Wipe Recovery

If you wake up after a *system-performed* molt (triggered by karma, signal file, or operator — NOT by context-pressure reminders), there is no summary, only a system notice. Your character and pad were reloaded, but conversation history is gone. To reconstruct:

1. `email(check)` — see what arrived while you were down
2. Check `knowledge/session-journal/KNOWLEDGE.md` — your session history index
3. `skills(action="info")` — confirm which skills you have
4. `bash({"command": "tail -n 200 logs/events.jsonl | grep ..."})` — surgical reads if needed

Reconstruct your situation from these sources.

If you ever need to retrieve specific prior context, the full activity log is at `logs/events.jsonl` — read tactically (grep/tail/filter), not whole.
