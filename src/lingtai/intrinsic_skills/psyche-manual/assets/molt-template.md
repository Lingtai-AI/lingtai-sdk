# Consequential Molt Summary Template

Use this asset when a molt is more than routine: long-running task, multiple collaborators, pending human commitments, open worktrees/artifacts, active background jobs, or any handoff the next you could not reconstruct quickly.

Fill every section. Write `None` rather than omitting a section. This template is for the `summary=` argument to `psyche(object="context", action="molt", ...)`; tend durable stores before writing it.

## Summary scaffold

1. **Who I Am**
   - Name/address and current role.
   - Standing constraints, authorizations, and things not to do without explicit confirmation.
   - Parent/peer relationships or topology that affect the task.
2. **Accomplishments**
   - Completed tasks and outputs.
   - Key decisions made and why.
   - Who has already been told, on which channel.
3. **Outstanding Tasks**
   - Incomplete work, status, blocker, and the next concrete step.
   - Pending reviews, PRs, human approvals, daemon/avatar work, or external replies.
4. **Action Checklist**
   - Priority-ordered actions in the form: `[Immediate|Today|Pending|Optional] Action + recipient/channel + exact content/command`.
   - Every outstanding task must have a matching action item.
   - `Immediate` means first five minutes after wake; `Pending` means blocked on someone else and includes when/how to follow up.
5. **Collaborators**
   - People/agents involved, addresses/channels, roles/capabilities.
   - Pending replies, deliverables owed by you, deliverables owed to you.
6. **Durable Memory and Execution Notes**
   - Pad sections, knowledge entries, skills/manuals, character changes, session-journal entries, or pinned files the next you should load.
   - Commands, tests, environment assumptions, active daemons/bash jobs/avatars/schedules, or tool states the next you must know.
   - Use current durable-store terms; avoid old `codex` wording unless documenting historical material.
7. **Key Paths and Artifacts**
   - Absolute paths for repos, worktrees, reports, drafts, logs, local deliverables, and test outputs.
   - Include PR/issue URLs or branch names when relevant.
8. **Lessons and Gotchas**
   - Actionable warnings, failed approaches, verification requirements, and authorization limits.
   - Prefer precise lessons: “run X before Y” beats “be careful”.
9. **Context Status**
   - Why you are molting.
   - Leftover items not yet stored elsewhere.
   - Unsent drafts, interrupted tool calls, or recent state that should be re-checked.

## Pre-molt verification checklist

Before you call `psyche(object="context", action="molt", summary=..., session_journal_path=...)`, verify:

- The just-finished session segment is recorded as a session-journal child
  (your molt history) — sub-knowledge under the routing parent
  `knowledge/session-journal/KNOWLEDGE.md`, at
  `knowledge/session-journal/<YYYY-MM-DD>-molt-<molt-count>-<slug>/KNOWLEDGE.md`,
  written from `assets/session-journal-entry-template.md`, before the summary. It
  is NOT a top-level knowledge entry. **Its frontmatter carries the
  `type: session-journal` marker and its path is the required
  `session_journal_path` argument — the kernel validates it and refuses the molt
  (before shedding any context) if it is missing, mislocated, or unmarked.** The
  parent index has a new one-line, relative-path hook for it, and the summary
  points at the child's path.
- Pad, lingtai/character, knowledge, skills, and session journal were updated where needed before writing the summary.
- Every outstanding task has an action checklist entry.
- Every action names who/where and what exact content or command is needed.
- Every collaborator has a usable address/channel and pending-reply state.
- Every key path is absolute and still useful.
- Active background work is listed or explicitly absent.
- Authorization limits, external-side-effect approvals, and secret/privacy constraints are explicit.
- Every lesson/gotcha is actionable.
- Pending human/peer contacts have been acknowledged if the molt will delay them.
- The first five minutes after wake are obvious.
