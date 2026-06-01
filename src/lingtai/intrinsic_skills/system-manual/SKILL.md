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
  reporting guidance; and `reference/sqlite-log-query/SKILL.md` for SQLite/log.sqlite
  runtime trace inspection and trajectory/anomaly mining from event traces. Also
  route here for lifecycle operations, notification
  handling, molt/memory questions, MCP/addon ownership, preset tiers,
  collaboration/network topology, resident prompt design, and the `system` tool
  actions.
version: 1.2.0
tags: [lingtai, agent, runtime, procedures, substrate, system, lifecycle, memory, communication, skills, molt]
---

# System Manual — Progressive Disclosure Router

`system-manual` is the second layer of LingTai operating guidance. The resident
`substrate` and `procedures` prompts keep only the short rules every agent must
hold constantly. This skill routes from those compact rules to the reference node
that carries the actual detail.

Use this file first when the question is about LingTai's agent runtime, resident
prompt design, lifecycle, memory, communication, tool routing, system operations,
or runtime trace inspection. Then open the referenced lower node.


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
```

## Router table

| Need / keywords | Read |
|---|---|
| Expanded substrate; body/extensions; bash vs daemon vs avatar vs MCP; lifecycle states; ACTIVE/IDLE/ASLEEP/SUSPENDED; same-channel communication; notifications; memory layers; molt model; idle/soul; preset tiers; `system` operations | `reference/substrate-manual/SKILL.md` |
| Expanded procedures; progressive disclosure; writing skills/knowledge; action discipline; responsiveness; skill routing; HTML deliverables; artifact sharing; issue reporting; when to read which manual | `reference/procedures-manual/SKILL.md` |
| SQLite; `log.sqlite`; LingTai runtime logs; JSONL traces; `lingtai-agent log doctor`; `lingtai-agent log query`; `lingtai-agent log rebuild`; events/chat_entries schema; daemon/chat-history trace indexing; WAL/live-read caveats; SQL recipes; trajectory/anomaly mining; improvement digests; cheap-model strategy | `reference/sqlite-log-query/SKILL.md` |
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
