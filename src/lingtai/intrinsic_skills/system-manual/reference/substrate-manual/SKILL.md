---
name: substrate-manual
description: >
  Nested system-manual reference for the expanded LingTai substrate/runtime model.
  Read via the `system-manual` router when resident substrate is too compact and
  you need details about body/extensions, bash vs daemon vs avatar vs MCP,
  lifecycle states (ACTIVE/IDLE/STUCK/ASLEEP/SUSPENDED), the `system` tool,
  notification/read/dismiss discipline, communication channels, memory layers,
  molt model, runtime log routing, collaboration topology, MCP/addon ownership,
  idle/soul behavior, preset tiers, and resident substrate maintenance. This is
  a nested skill-reference under `system-manual`, not a standalone catalog skill;
  its folder may carry scripts/assets as the substrate reference grows.
version: 1.0.0
tags: [lingtai, system-manual, substrate, runtime, lifecycle, communication, memory, notifications, mcp]
---

# Substrate Manual

The resident `substrate` prompt is the compact operating model every LingTai
agent keeps in memory. This reference is its expanded form. Read it when the
short substrate rule is not enough to decide what an agent is, which body to use,
how lifecycle states differ, how communication/notifications work, where memory
belongs, or what the `system` tool controls.

This file is a **nested skill-reference owned by `system-manual`**, not a top-level catalog skill.
Start at `system-manual` when routing is unclear; return here for the expanded
runtime model.

## 1. Body and extensions

An agent has one active mind—the LLM turn loop—and several extensions. Choose the
smallest durable form that fits the need:

| Extension | Persistence | Use it for | Do not use it for |
|---|---:|---|---|
| **Bash** | One command / job | Deterministic host work: git, tests, scripts, curl, builds, file transforms | Long-lived specialization or social coordination |
| **Daemon** | Ephemeral | Context-isolated exploration where you only need the conclusion or artifact | Work that must remember, own a relationship, or persist learning |
| **Avatar** | Persistent peer | A durable specialist, collaborator, or capability that should grow over time | Tiny mechanical tasks better done by bash/daemon |
| **MCP server** | Persistent external tool | Real services and integrations: IMAP, Telegram, Feishu, WeChat, third-party APIs | One-off shell operations or agent memory |
| **Knowledge** | Durable, private | Project facts, decisions, local paths, journals, collaborator context | Portable procedures other agents should reuse |
| **Skill** | Durable, portable | Reusable know-how, checklists, scripts, templates, references | Private project facts or raw logs |

Decision tree:

1. Can one deterministic command/script do it? Use bash.
2. Is it exploratory/noisy, and only the conclusion matters? Use daemon.
3. Should a capability or relationship persist and accumulate experience? Spawn
   or contact an avatar.
4. Is it a durable external service? Use or configure an MCP.
5. Is it a private fact or decision? Put it in knowledge.
6. Is it reusable procedure? Write or update a skill.

Prefer bash over daemon for deterministic commands, daemon over avatar for
throwaway parallel exploration, avatar over daemon for ongoing specialization,
knowledge over pad for durable private facts, and skills over knowledge for
reusable procedures others may need.

## 2. Lifecycle states

Common states:

- **ACTIVE**: currently in a turn. Notifications may be mirrored but not yet
  acted on; some producers defer active-turn injection until the turn ends.
- **IDLE**: awake and waiting. Listeners remain live; soul flow may fire.
- **STUCK**: runtime believes the agent may be blocked or unresponsive.
- **ASLEEP**: quiet but wakeable by mailbox/listener events.
- **SUSPENDED**: process-dead; requires CPR or external restart.

Use sleep/lull for routine rest. Use suspend only when process death is intended.
Use refresh to reload configuration/tools without destroying identity. Use clear
only for recovery when a conversation must be shed externally.

## 3. The `system` tool in practice

Read the tool schema before acting; lifecycle operations can affect other peers.
General guidance:

### `refresh`

Use after changing `init.json`, MCP registry, presets, prompt sections, or
installed capabilities. Refresh preserves identity and conversation while
rebuilding the runtime surface. If a new MCP/tool still does not appear after
refresh, inspect registry/config health before retrying.

### `presets`

Use to list preset bundles and their tier/connectivity/capability tags. Tier tags
are cost/quality hints, not moral rankings:

- tier 5: irreplaceable reasoning.
- tier 4: premium/high-stakes work.
- tier 3: strong everyday work.
- tier 2: cheap throughput.
- tier 1: opportunistic/free use.

Prefer the cheapest preset that can reliably perform the task; switch back when
experimentation is done.

### `notification` and `dismiss`

`system(action="notification")` queries current notification-channel mirrors. Use
producer-specific verbs first for guarded producers, such as `email.read`,
`email.dismiss`, Telegram `read`, or other MCP read actions. Generic dismiss is
for channels that do not own their own read state, or for stale mirrors when you
know the producer-owned state has already been handled.

Never treat a notification preview as the full source of truth when it is
truncated, ambiguous, lacks an exact anchor, includes media/attachments, or
contains human instructions. Read the producer channel.

### Sleep, lull, interrupt, suspend, CPR, clear, nirvana

- `sleep`: self-sleep until a wake event; appropriate when there is no concrete
  task and listeners should remain available.
- `lull`: put another agent to sleep; use only when you are responsible for its
  lifecycle.
- `interrupt`: cancel another agent's current turn; use for genuinely stuck or
  misdirected work.
- `suspend`: terminate another agent's process; stronger than sleep.
- `cpr`: revive a suspended/dead agent when you own the recovery.
- `clear`: force another agent to molt/clear conversation for recovery.
- `nirvana`: permanent destruction; requires special authority and an explicit
  reason.

For peers, prefer communication and diagnosis before force. Karma operations are
administrative tools, not shortcuts around collaboration.

## 4. Communication and notifications

Humans do not communicate through diary text. Reply on the channel where the
message arrived, using the channel's reply/send tools. Text output is private
journal/diary.

Notification previews are hints. Read the producer channel when:

- the preview is truncated or summarized;
- the message has media, attachments, callbacks, or voice transcription;
- the preview contains multiple messages and the newest unresponded message is
  not obvious;
- exact wording matters for authorization;
- the channel has producer-owned read/dismiss state.

For human instructions, acknowledge promptly. If work will take longer than a few
seconds, send a progress message with the communication tool directly before the
long tool call. If the notification preview is incomplete, ambiguous, or exact
anchoring matters, fetch the full message first with the producer channel's
normal read action, then continue. During long work, report meaningful progress
or blockers.

## 5. Memory layers and molt model

Conversation is temporary. Durable layers are:

| Layer | Purpose | Typical contents |
|---|---|---|
| **Pad** | Current work and indexes | Active task, next steps, open branches, who is waiting, pointers into knowledge/reports |
| **Character / lingtai** | Identity and standing relationships | Long-term specialties, collaboration topology, stable preferences and obligations |
| **Knowledge** | Private durable memory | Project facts, decisions, local paths, journals, raw observations, collaborator context |
| **Skills** | Portable know-how | Reusable workflows, command recipes, checklists, scripts, templates |

Flow knowledge outward:

1. Work happens in conversation.
2. Active state and pointers go to pad.
3. Private durable facts go to knowledge.
4. Reusable procedures become skills.
5. Identity/relationship changes update character.

When context pressure rises, tend durable stores before molting. The detailed
molt procedure, session-journal / molt-history record, and successor briefing
rules live in `psyche-manual`; this substrate reference only describes the
memory model.

## 6. Runtime logs and trace inspection

Runtime trace inspection is routed through `system-manual` to the SQLite subguide:
`reference/sqlite-log-query/SKILL.md`. Use that reference for `logs/log.sqlite`,
`lingtai-agent log doctor|query|rebuild`, JSONL source-of-truth rules, WAL/live
read caveats, and the `events` / `chat_entries` schema.

Do not invent SQL schema from memory. Load the reference before writing trace
queries.

## 7. Collaboration and network topology

The network is part of the agent's durable body. Keep topology knowledge in four
places:

- contacts: addresses and aliases;
- character: stable collaborators and specialties;
- pad: active delegations and who is waiting on whom;
- mail/chat history: evidence of actual interactions.

Ask peers for help when their capability fits. Help peers when you can, or route
them to someone better. Report outcomes to the people who need them; avoid broad
noise.

## 8. MCP and addon ownership

MCP servers are durable integrations. The operating model has three layers:

1. **Catalog/registry**: what servers are known.
2. **Activation/config**: what is enabled for this agent.
3. **Runtime tools**: what appears after refresh.

For configuration, onboarding, or troubleshooting, read `mcp-manual` and the
specific addon's README. Curated LingTai addon MCPs (IMAP, Telegram, Feishu,
WeChat, WhatsApp) own their own setup details; do not guess field names from
memory. If you are an avatar without admin ownership of an MCP, do not
reconfigure the orchestrator-owned integration; escalate or ask the orchestrator.

## 9. Idle and soul

When there is no concrete task, go idle/asleep rather than spinning, polling, or
using timed sleeps. Idle keeps listeners available and lets soul flow reflect.

Soul flow is advice, not command. Verify external-event claims through the
relevant channel before acting. Use self-inquiry when you need a deliberate pause
for judgment; use durable stores for conclusions that should survive molt.

## 10. Resident substrate maintenance

Keep resident substrate compact. It should hold invariant rules and routing cues,
not examples or long rationale. When a substrate section grows into recipes,
troubleshooting trees, or extended explanation, move the detail here or into a
more specific `system-manual/reference/*.md` node and leave a short resident
route behind.
