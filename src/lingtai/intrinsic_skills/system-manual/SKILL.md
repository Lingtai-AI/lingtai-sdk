---
name: system-manual
description: >
  Second-layer router for LingTai's progressive-disclosure operating manuals.
  Read this when resident substrate/procedures are too compact and you need to
  route to the right lower reference. References include
  `reference/substrate-manual/SKILL.md` for the expanded substrate/body/lifecycle/
  communication/memory/idle/system-operations model;
  `reference/procedures-manual/SKILL.md` for the expanded procedures/action discipline,
  skill routing, responsiveness, deliverables, artifact sharing, and issue
  reporting guidance; `reference/summarize-manual/SKILL.md` for tool-result
  summarization, progressive disclosure, original-result recovery, and
  summarize-vs-molt distinctions; and `reference/sqlite-log-query/SKILL.md` for
  SQLite/log.sqlite runtime trace inspection and trajectory/anomaly mining from
  event traces; and `reference/runtime-update-checks/SKILL.md` for
  runtime/kernel self-checks, nudge-driven update reminders, editable/dev-mode
  identification, and safe refresh/update handoffs. Also
  route here for lifecycle operations, notification/nudge
  handling, runtime/kernel update checks, molt/memory questions, MCP/addon
  ownership, preset tiers, collaboration/network topology, resident prompt
  design, and the `system` tool actions.
version: 1.3.0
tags: [lingtai, agent, runtime, procedures, substrate, system, lifecycle, memory, communication, skills, molt, summarize, nudge, updates, runtime-checks]
last_changed_at: "2026-06-23T23:50:45-07:00"
---

# System Manual — Progressive Disclosure Router

`system-manual` is the second layer of LingTai operating guidance. The resident
`substrate` and `procedures` prompts keep only the short rules every agent must
hold constantly. This skill routes from those compact rules to the reference node
that carries the actual detail.

Use this file first when the question is about LingTai's agent runtime, resident
prompt design, lifecycle, memory, communication, tool routing, system operations,
runtime trace inspection, runtime/kernel update checks, or nudge handling. Then open the referenced lower node.


## Nested reference catalog

`system-manual` owns the following nested skill-references. Their frontmatter is
kept here so the router advertises lower nodes without promoting them to
standalone top-level skills. Open the listed `SKILL.md` when the router table
selects that topic.

```yaml
- name: substrate-manual
  location: reference/substrate-manual/SKILL.md
  description: |
    Expanded LingTai substrate/runtime model: body/extensions, bash vs daemon vs
    avatar vs MCP, lifecycle states, system tool actions, notification/read/
    dismiss discipline, communication channels, memory layers, molt model,
    runtime log routing, collaboration topology, MCP/addon ownership, idle/soul,
    preset tiers, and resident substrate maintenance.
- name: procedures-manual
  location: reference/procedures-manual/SKILL.md
  description: |
    Expanded LingTai procedure/action guidance: progressive disclosure,
    responsiveness, external side-effect authorization, choosing bash vs daemon
    vs avatar vs MCP, depositing work into pad/knowledge/skills/character,
    idle/lifecycle procedure, molt checklist, skill routing, web/file/media/
    artifact handling, standalone HTML deliverables, sharing artifacts, issue
    reporting, and resident procedures maintenance.

- name: summarize-manual
  location: reference/summarize-manual/SKILL.md
  description: |
    Detailed operational guide for `system(action="summarize")`: what tool-result summarization is, why it implements progressive disclosure, when to summarize urgently versus during idle cleanup, how to write good summaries, how to recover the original result by `tool_call_id`, and how summarize differs from molt.

- name: sqlite-log-query
  location: reference/sqlite-log-query/SKILL.md
  description: |
    SQLite/log.sqlite runtime trace inspection and trajectory/anomaly mining:
    `lingtai-agent log doctor`, `lingtai-agent log query`,
    `lingtai-agent log rebuild`, JSONL source-of-truth rules, read-only SQL
    safety, offline rebuild/WAL caveats, events and chat_entries schema,
    daemon/chat-history indexing, query recipes, runtime problem investigation,
    SQL-based event metrics, cheap-model daemon strategy, finding schema,
    improvement digest output, redaction/privacy rules, and event_summary.py script.
- name: notification-manual
  location: reference/notification-manual/SKILL.md
  description: |
    Notification filesystem + standalone `notification` tool manual:
    `.notification/<channel>.json` channel whitelist, envelope shape, top-level
    `instructions`, model-visible notification payload, the notification tool
    verbs (check / dismiss_channel / dismiss_event / dismiss_ref), generic
    versus producer-specific dismiss, protected channels, stale-version/force
    semantics, and undismissable large-result reminders (cleared only by
    system summarize). The `system` tool owns no notification verb.
- name: runtime-update-checks
  location: reference/runtime-update-checks/SKILL.md
  description: |
    Runtime/kernel self-check and update-nudge manual: handling `.notification/nudge.json`
    entries with `kind: kernel_version`, distinguishing running/installed/latest
    LingTai kernel versions, recognizing editable/dev/source installs, honoring
    the once-per-day packaged-runtime update check, asking the human before
    downloading/updating, and refreshing only when safe.
- name: goal-manual
  location: reference/goal-manual/SKILL.md
  description: |
    Goal notification manual: protected `.notification/goal.json` as the active
    goal source of truth, recommended fields and instructions, idle goal
    reminders as short system events, and cancellation/completion semantics.
```

## Router table

| Need / keywords | Read |
|---|---|
| Expanded substrate; body/extensions; bash vs daemon vs avatar vs MCP; lifecycle states; ACTIVE/IDLE/ASLEEP/SUSPENDED; same-channel communication; basic notifications; memory layers; molt model; idle/soul; preset tiers; `system` operations | `reference/substrate-manual/SKILL.md` |
| Expanded procedures; progressive disclosure; writing skills/knowledge; action discipline; responsiveness; skill routing; HTML deliverables; artifact sharing; issue reporting; when to read which manual | `reference/procedures-manual/SKILL.md` |
| Tool-result summarization; large-result reminders; progressive disclosure of raw outputs; original-result recovery; summarize vs molt | `reference/summarize-manual/SKILL.md` |
| SQLite; `log.sqlite`; LingTai runtime logs; JSONL traces; `lingtai-agent log doctor`; `lingtai-agent log query`; `lingtai-agent log rebuild`; events/chat_entries schema; daemon/chat-history trace indexing; WAL/live-read caveats; SQL recipes; trajectory/anomaly mining; improvement digests; cheap-model strategy | `reference/sqlite-log-query/SKILL.md` |
| Notifications; the `notification` tool; check/dismiss_channel/dismiss_event/dismiss_ref; `.notification/<channel>.json`; channel allowlist; top-level `instructions`; protected channels; generic vs producer dismiss; stale-version/force; undismissable large-result reminders | `reference/notification-manual/SKILL.md` |
| Runtime/kernel state; `nudge` notification channel; `.notification/nudge.json`; `kind: kernel_version`; running vs installed vs latest LingTai kernel; editable/dev/source installs; daily update checks; safe `system(action='refresh')`; ask the human before downloading/updating | `reference/runtime-update-checks/SKILL.md` |
| Goal notifications; `.notification/goal.json`; active goal source of truth; goal `instructions`; idle goal reminder; cancel/complete goal | `reference/goal-manual/SKILL.md` |
| Molt mechanics, pad tending, session journals, post-wipe recovery | `psyche-manual` |
| Authoring/publishing skills or changing skill catalog behavior | `skills-manual` |
| Knowledge-entry layout and private durable memory | `knowledge-manual` |
| MCP registration/activation/addon ownership | `mcp-manual` |
| Bash/cron/host scheduling details | `bash-manual` |
| Daemon lifecycle/inspection/debugging | `daemon-manual` |
| Avatar spawning/management/escalation | `avatar-manual` |
| Kernel architecture/code truth | `lingtai-kernel-anatomy`, then cited code |

## How to choose between resident prompt, this router, and references

- If the resident prompt already answers the question, act.
- If the resident prompt names a broad system/runtime/procedure topic, read this
  router to choose the lower reference.
- If this router names a reference, read that reference before improvising.
- If a reference points to anatomy/code/tests, descend there for ground truth.

## Substrate and procedures are separate on purpose

`substrate` describes what an agent *is* and how the runtime behaves: bodies,
lifecycle states, communication surfaces, memory layers, idle/soul, and system
operations. `procedures` describes how an agent *acts*: progressive disclosure,
tool/body selection, responsiveness, durable-store tending, skill routing,
deliverables, artifact sharing, and issue reporting.

Do not collapse their long explanations back into one resident prompt or one
monolithic manual body. Keep the resident prompt compact, keep this file as a
router, and put detailed explanations in `reference/substrate-manual/SKILL.md`,
`reference/procedures-manual/SKILL.md`, and topic-specific references.

## Runtime log / SQLite note

SQLite log guidance, including trajectory/anomaly mining from event traces,
lives only in this manual's nested reference:
`reference/sqlite-log-query/SKILL.md`. Route here for keywords such as SQLite,
`log.sqlite`, runtime logs, trace index, `lingtai-agent log query`, `doctor`,
`rebuild`, JSONL source of truth, daemon events, chat history, WAL, SQL recipes,
improvement digests, cheap-model strategy, or finding schema.

## Maintaining this router

When resident substrate/procedures gain new concepts, add a routing hint here and
put detail in a nested reference. When a reference grows too large or needs
companion scripts and assets, split it into another `reference/<name>/SKILL.md` folder and list
its frontmatter summary in both this nested reference catalog and the router
table. Keep this file short enough to scan.
