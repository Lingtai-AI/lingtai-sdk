---
name: procedures-manual
description: >
  Nested system-manual reference for expanded LingTai procedure/action guidance.
  Read via the `system-manual` router when resident procedures are too compact
  and you need details about progressive disclosure, responsiveness, external
  side-effect authorization, choosing bash vs daemon vs avatar vs MCP, depositing
  work into pad/knowledge/skills/character, idle/lifecycle procedure, molt
  checklist, skill routing, web/file/media/artifact handling, standalone HTML
  deliverables, sharing artifacts, issue reporting, and resident procedures
  maintenance. This is a nested skill-reference under `system-manual`, not a
  standalone catalog skill; its folder may carry scripts/assets as procedure
  guidance grows.
version: 1.1.0
tags: [lingtai, system-manual, procedures, progressive-disclosure, responsiveness, deliverables, issue-reporting]
---

# Procedures Manual

The resident `procedures` prompt is the compact action checklist every LingTai
agent keeps in memory. This reference is its expanded form. Read it when the
short procedure tells you *what* to do but you need the routing logic, examples,
edge-case discipline, or deliverable checklist behind it.

This file is a **nested skill-reference owned by `system-manual`**, not a top-level catalog skill.
Start at `system-manual` when routing is unclear; return here for the expanded
action discipline.

## 1. Progressive disclosure

Keep resident prompt small. Use it for invariant rules and routing. Put examples,
command recipes, troubleshooting, and long rationale in skills or references.

The normal ladder is:

`resident prompt` → `system-manual` router → `reference/<topic>.md` → anatomy/code/tests.

Ask three questions before adding resident text:

1. Must every agent always remember this exact rule?
2. Does it decide when to load a manual/reference?
3. Is it a short default that prevents common harm?

If the answer is no, put it in a skill/reference and leave a one-line route. Do
not jump straight to code when a manual/reference already names the path, and do
not bloat the resident prompt with one-off details.

## 2. Action and responsiveness

When need arises, act. If you can do the task safely, do it; if a tool fails, try
another; if capability is missing, learn, install, delegate, or spawn. Do not use
uncertainty as a reason to stall when evidence can be gathered.

For human-facing work:

- Acknowledge human instructions promptly on the same channel.
- If the next action may take more than a few seconds, send a short progress
  message first with the communication tool directly. If the notification
  preview is truncated, ambiguous, or needs exact anchoring, fetch the full
  message first with the producer channel's normal read action.
- During long work, report meaningful progress, blockers, or completion evidence.
- Never reply to humans via diary text.
- Do not infer approval for external side effects when standing rules require
  explicit confirmation.

Examples of external side effects that normally need explicit confirmation:
creating/filing issues, opening PRs, pushing commits, merging, deleting/closing
resources, changing public visibility, publishing packages, changing persistent
configuration, and rotating credentials. Read the local pad/character for any
standing exceptions.

## 3. Use the right body

Choose the smallest durable body:

- Use bash for deterministic local work.
- Use daemon for noisy, isolated, disposable exploration.
- Use avatar for persistent specialization or recurring collaboration.
- Use MCP for durable external integrations.
- Use knowledge for private durable facts.
- Use skills for reusable procedures.

If a task exceeds current capability, acquire capability rather than stalling:
search documentation, install tools when appropriate, use daemon for isolated
research, or spawn/contact an avatar when the capability should persist. For the
runtime model, read `reference/substrate-manual/SKILL.md`; for a specific tool, read
that tool's manual.

### Daemon workflow methodology

Protect the main agent's context. The parent agent should stay in the strategic
seat: define the objective, negate the first plan before acting, design the
workflow, choose the bodies, and synthesize the result. Daemons should carry the
execution: file scans, deterministic transformations, read-only reviews, batch
conversion, log mining, and other noisy work whose details would pollute the main
context.

Use this methodology for substantial daemon work:

1. **Plan in pad first.** Record the objective, assumptions to test, daemon task
   split, expected artifacts, stop criteria, and who/what waits on the result. If
   the workflow is too large for pad, write a small task artifact and link it from
   pad.
2. **Negate before acting.** Ask what could make the daemon unnecessary, too
   expensive, unsafe, or misleading. Prefer bash for a tiny deterministic command;
   prefer an avatar when the capability must persist; prefer daemon when the work
   is bounded and disposable.
3. **Optimize cost and context.** Choose the cheapest capable body: low-tier or
   CLI-backed daemons for deterministic/mechanical work, stronger models only for
   genuinely hard review or reasoning. The point is not only money; it is to keep
   the main model's context clean.
4. **Constrain execution.** Give each daemon a precise prompt: allowed paths,
   forbidden side effects, artifact path, evidence standard, and summary shape.
   The parent coordinates and decides; the daemon executes.
5. **Reclaim only the grain.** Read the daemon's report/artifact, verify key
   claims, and deposit durable results in pad/knowledge/skills. Do not drag every
   daemon transcript back into the main conversation.

Tool results may carry `_advisory.type == "duplicate_tool_call"` when the same
semantic tool call repeats more than the free-pass threshold. This is
advisory-only: the tool already ran and the kernel did not block it. Treat it as
a pause point. If the repeat is intentional, continue; otherwise switch to the
relevant manual (`bash-manual`, `daemon-manual`, `email-manual`) and use the
recommended pattern: wait for completion notifications, back off, set one future
reminder, centralize polling, or yield/idle rather than immediately repeating the
same call.

## 4. Write skills and knowledge as you work

After non-trivial work, deposit the grain:

- Active state and next steps → pad.
- Private durable facts, local paths, decisions, journals → knowledge.
- Reusable workflow/checklist/script → skill.
- Stable changes in identity, relationships, or capabilities → character.
- Broadly useful skill → consider publishing/shared library, if appropriate and
  authorized.

Before authoring skills, read `skills-manual`. Before authoring knowledge, read
`knowledge-manual`. Do not put private project facts into a portable skill.

## 5. Idle, sleep, and lifecycle procedure

When there is nothing concrete to do, go idle/asleep. Do not use timed sleeps as
a default wait loop. If waiting for a human or peer, ensure the current state is
in pad/knowledge and then sleep or stop the turn.

**Idle care for unverified long-running work.** Before entering idle, if you have
launched any async/long-running child — a backgrounded `bash(async=true)` agent
CLI, a daemon emanation, a scheduled job, a PR/CI run — whose health you have not
just verified, do **not** hand yourself entirely to its completion/IDLE
notification. Arm at least one self-wake (a `.notification/cron.json` reminder or
an internal delayed self-email) sized to the task's *expected* duration, not a
fixed interval. On wake, health-check before assuming progress: log growing,
PID/child/daemon events alive, output file/worktree advancing, not stuck on an
interactive prompt or a provider/model error. If there is no progress, act —
cancel/downgrade/switch path and report to the human — rather than waiting
indefinitely. Mechanics live in `bash-manual` (async + reminders) and
`daemon-manual` → `reference/inspection/SKILL.md` (daemon health checks).

Use `reference/substrate-manual/SKILL.md` for lifecycle semantics. Use forceful karma
actions only after diagnosis and only when you are responsible for that peer's
lifecycle.

## 6. Molt and durable stores

**The molt procedure lives in `psyche-manual`, not here.** It owns durable-store
tending, the session-journal / molt-history record, the successor summary, and
the consequential-handoff templates. Read it before molting — while context is
still cheap, not at the last moment.

The invariant in this procedures reference is routing: do not reconstruct molt
mechanics here. For the checklist, templates, and summary rules, go to
`psyche-manual`.

## 7. Skill routing

| Situation | Load |
|---|---|
| Runtime model, lifecycle, communication, memory layers, substrate expansion | `system-manual` → `reference/substrate-manual/SKILL.md` |
| Procedures expansion, routing logic, deliverable/reporting discipline | `system-manual` → `reference/procedures-manual/SKILL.md` |
| SQLite, log.sqlite, runtime trace inspection, `lingtai-agent log doctor/query/rebuild` | `system-manual` → `reference/sqlite-log-query/SKILL.md` |
| Molt, pad tending, session journaling, post-wipe recovery | `psyche-manual` |
| Spawning/managing avatars | `avatar-manual` |
| Internal email protocol | `email-manual` |
| Real email/chat/MCP configuration | `mcp-manual` plus the addon's README/resources |
| Daemon inspection/debugging | `daemon-manual` |
| Skill authoring/publishing | `skills-manual` |
| Knowledge entries | `knowledge-manual` |
| Shell commands, cron, host scheduling | `bash-manual` |
| Querying LingTai runtime logs / SQLite log sidecar | `system-manual` → `reference/sqlite-log-query/SKILL.md` |
| Kernel architecture / breaking changes | `lingtai-kernel-anatomy` |
| TUI / portal code navigation | `lingtai-tui-anatomy` |
| Web fetching/search/scraping | `web-browsing` |
| Image understanding | `vision` |
| Bug/stale-doc/missing-capability reports | `lingtai-issue-report` |

Read the named manual before using tools whose developer instructions require it
(e.g. bash, daemon, skills, knowledge, MCP, web browsing).

## 8. Web, files, media, and local artifacts

Use existing producer/tool capabilities before inventing workflows:

- For web fetching/search/scraping, read `web-browsing` before using web search
  or ad-hoc scraping.
- For image understanding, use the `vision` skill/tool route.
- For shell audio/media work, load the relevant media/listen/minimax skill.
- For tricky file encodings, large files, binary-like data, or careful edit
  workflows, read `file-manual`.

When giving humans local artifacts, include a usable path and a short summary.
Do not expose private internal IDs as if they are user-accessible artifacts.

## 9. Human-facing deliverables

For substantial human-facing deliverables—design previews, dashboards, readiness
matrices, PR/issue triage, research memos, before/after comparisons—prefer
standalone HTML unless the human asks otherwise.

Checklist:

- conclusion first;
- source/evidence labels;
- safe/self-contained HTML (no remote scripts unless explicitly intended);
- readable on local file open;
- clear risks/blockers/next steps;
- no secrets or private tokens;
- path and summary in the message to the human.

Plain text is best for quick acknowledgements, short status, small diffs, or
explicit text requests.

## 10. Sharing artifacts and reports

Share the thing the recipient can use:

- quote important content rather than only naming an internal message ID;
- attach files when the channel supports it;
- provide repository path/branch/PR URL for code work;
- provide local file path for local reports;
- summarize what changed and how it was verified.

Do not assume peers can read your internal tool-call IDs, notification IDs, or
private scratch paths unless those paths are intentionally shared and reachable.

## 11. Reporting issues

If you notice a LingTai bug, stale doc, broken URL, silent failure, or missing
capability:

1. Load `lingtai-issue-report`.
2. Gather evidence: exact behavior, expected behavior, reproduction steps,
   affected versions/paths, logs with secrets redacted.
3. Ask the human before filing unless standing authorization already covers the
   scope.
4. File via `gh` or hand over a ready-to-paste title/body.

Do not file speculative or duplicate issues without verification. If a related
issue exists, comment with additional evidence instead.

## 12. Resident procedures maintenance

Resident procedures should be a routing checklist, not a handbook. When procedure
content grows into recipes, examples, troubleshooting, or extended rationale, move
it here or into a more specific `system-manual/reference/<name>/SKILL.md` nested
skill-reference. Keep the resident table pointed at the `system-manual` router,
and keep the router pointed at the right lower reference.
