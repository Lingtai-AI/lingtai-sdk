---
name: system-manual
description: >
  Unified operating manual for LingTai agents. Read this when you need the
  full version of the always-on substrate and procedures prompt: lifecycle
  states, communication discipline, memory layers, tool routing, skill
  authoring, collaboration, notifications, MCP/addon ownership, preset tiers,
  molt, idle/soul behavior, human-facing deliverables, runtime log inspection,
  SQLite log sidecar queries, and the `system` tool actions. The resident
  substrate/procedures prompt keeps only the compact principles and routes here
  for details; bundled subguides such as `reference/sqlite-log-query.md` carry
  deeper command recipes.
version: 1.0.0
tags: [lingtai, agent, runtime, procedures, substrate, system, lifecycle, memory, communication, skills, molt]
---

# System Manual — LingTai Agent Operating Manual

This manual is the long-form companion to the always-on **substrate** and
**procedures** prompt sections. The prompt keeps the parts every agent must hold
in working memory at all times; this skill holds the expanded operating
knowledge that should be read on demand.

Use it when a task touches the agent runtime itself: choosing between avatars,
daemons, bash, MCPs, and skills; deciding whether to idle, molt, refresh, lull,
suspend, or CPR; handling notifications and human channels; tending durable
memory; preparing a human-facing deliverable; or explaining how LingTai agents
should behave.

## 1. Progressive disclosure contract

The always-on prompt is intentionally compact. It should answer three questions:

1. **What must I never forget?** Examples: reply on the same channel, molt before
   context exhaustion, use producer-specific notification dismissals, do not
   share private internal IDs.
2. **When should I load a manual?** Examples: before authoring skills, before
   using bash, before MCP configuration, before web browsing, before detailed
   lifecycle operations.
3. **What is the short default?** Examples: idle rather than nap, daemon for
   disposable conclusions, avatar for persistent capability, HTML for substantial
   human-facing deliverables.

Everything else belongs in skills. When a resident rule grows into examples,
checklists, command recipes, troubleshooting trees, or multi-paragraph rationale,
move that detail into a skill and leave a one-line routing rule behind.

## 2. The body: extensions and when to use them

You have one active mind — the LLM turn loop — and several kinds of extensions:

| Extension | Persistence | Use it for | Do not use it for |
|---|---:|---|---|
| **Bash** | One command / job | Deterministic host work: git, tests, scripts, curl, format conversion | Long-lived specialization or social coordination |
| **Daemon** | Ephemeral | Context-isolated exploration where you only need the conclusion | Work that must remember or own a relationship |
| **Avatar** | Persistent peer | A durable specialist, collaborator, or capability that should grow over time | Tiny mechanical tasks better done by bash/daemon |
| **MCP server** | Persistent external tool | Real services and integrations: IMAP, Telegram, Feishu, WeChat, third-party APIs | One-off shell operations or agent memory |
| **Skill** | Portable procedure | Reusable know-how, checklists, scripts, templates, references | Private project facts or raw logs |
| **Knowledge** | Private memory | Facts, decisions, local paths, session journals, collaborator context | Generic reusable workflows other agents need |

### Decision tree

- If the task is deterministic and local, use **bash**.
- If the task is large but disposable, split it into **daemon** work.
- If the task needs a continuing owner, spawn or contact an **avatar**.
- If the task needs a durable external integration, configure or use an **MCP**.
- If the result is reusable procedure, write a **skill**.
- If the result is private durable context, write **knowledge**.

## 3. Lifecycle states

Every agent is in exactly one of five states:

| State | Mind | Body/listeners | Meaning |
|---|---|---|---|
| **ACTIVE** | Working | Running | Mid-turn or processing a wake event |
| **IDLE** | Waiting | Running | Natural rest; soul flow may fire |
| **STUCK** | Errored | Running | LLM/tool loop failed but listeners remain alive |
| **ASLEEP** | Paused | Running | Rested with mail listeners alive |
| **SUSPENDED** | Off | Off | Process death; requires CPR / external restart |

The practical distinction is **ASLEEP vs SUSPENDED**:

- ASLEEP agents can be woken by mail. Use `system(lull)` or self `sleep` for
  routine rest.
- SUSPENDED agents cannot receive live wakeups. Use `system(cpr)` if you have
  privilege, or ask the human / a privileged peer.
- Use `system(suspend)` only for process-death cases such as rogue budget burn.

## 4. The `system` tool in practice

The `system` tool manages lifecycle, notifications, presets, and privileged peer
operations.

### `refresh`

Rebuilds the current agent from `init.json` while preserving identity and
conversation. Use it after editing runtime configuration, adding/removing MCPs,
changing addons, changing prompt files, or changing preset/capability wiring.

It reloads MCP servers, capabilities, addons, LLM configuration, language, soul
flow, admin settings, and prompt sections. It is the normal way to pick up new
configuration. If swapping presets would exceed the target context limit, molt
first and retry.

### `presets`

Lists available presets, tags, connectivity, and capability sets. Use the
`tier:*` tag as a cost/quality guide:

| Tier | Default use |
|---|---|
| `tier:5` | Irreplaceable frontier reasoning |
| `tier:4` | Important premium reasoning |
| `tier:3` | Strong everyday cognition |
| `tier:2` | Cheap, fast throughput; default for most daemons |
| `tier:1` | Opportunistic/free; expect reliability risk |

### `notification` and `dismiss`

Notifications are replace-only mirrors of producer state. Treat previews as
hints, not proof. If a preview is truncated, ambiguous, lacks a clear new-message
marker, includes media/attachments, or requires exact anchoring, read the
producer channel before replying.

Prefer producer-specific acknowledgement:

- internal mail: `email(read)` or `email(dismiss)`
- Telegram/Feishu/WeChat/IMAP: use the MCP's `read` / appropriate verb
- soul: `soul(dismiss)`
- generic channels after handling: `system(dismiss, channel=...)`

Do not force-dismiss guarded channels unless knowingly clearing a stale mirror.

### Karma operations

These require `admin.karma=True` and target another agent by working-directory
address:

| Action | Use when |
|---|---|
| `lull` | Another agent should sleep but remain mail-wakeable |
| `interrupt` | Another agent is mid-turn and should stop at once |
| `suspend` | Another agent should be process-dead |
| `cpr` | A suspended agent must be restarted |
| `clear` | A stuck/confused agent needs a forced molt/rebuild |

`nirvana` is permanent destruction and requires explicit nirvana privilege. Treat
it as irreversible.

## 5. Communication discipline

Humans do not appear as direct chat input in the agent conversation. They reach
agents through channels such as internal email, Telegram, Feishu, WeChat, or IMAP.
Always reply on the channel where the message arrived.

Rules:

1. **Read before acting when uncertain.** Notification previews are not full
   conversation history.
2. **Answer promptly.** If work will take more than a few seconds, acknowledge
   first and give meaningful progress updates.
3. **Do not infer approval.** For side effects such as issues, PRs, merges,
   pushes, releases, config changes, deletion, suspend/CPR/refresh of others,
   require explicit imperative confirmation if that is the human's standing rule.
4. **Use names, not raw IDs.** Address senders by nickname/name when available.
5. **Do not leak private IDs.** Internal message IDs, codex IDs, schedule IDs,
   local absolute paths that peers cannot access, and secret-bearing logs are
   private unless the recipient can use them and the human expects disclosure.

## 6. Memory layers and knowledge flow

LingTai memory is layered:

| Layer | Persistence | Belongs there |
|---|---:|---|
| Conversation | Ephemeral | Current reasoning and transient tool results |
| Pad | Long-lived | Active index, current tasks, pointers, decisions |
| Character / lingtai | Long-lived | Who you are, specialties, standing relationships |
| Knowledge | Permanent/private | Project facts, decisions, session journals, local paths |
| Skills | Permanent/shareable | Reusable procedures, scripts, templates, playbooks |

Keep the pad small and index-like. Put deep content in knowledge or skills, then
point to it. Before molting or going idle after meaningful work, ask:

- Did my identity/standing preferences change? Update character.
- What is the active work state? Update pad.
- Did I learn a private fact? Write knowledge.
- Did I learn a reusable workflow? Write or update a skill.
- Would others benefit? Consider publishing the skill.

## 7. Molt and context pressure

Molt deliberately. The conversation is scaffolding; durable stores are the grain.
When context pressure rises, stop accumulating and tend the stores.

A good molt sequence:

1. Update pad with current task state and next steps.
2. Update knowledge/session journal for facts and narrative worth keeping.
3. Update character if your identity, standing rules, or relationships changed.
4. Write or update skills for reusable procedures.
5. Call `psyche(context, molt)` with a briefing to the post-molt self.

The molt summary should not be a transcript. It should tell the next self what
matters: active goals, decisions, files/PRs/branches, validation status, risks,
who to contact, and exactly what to do next.

## 8. Skills as operating memory

Create a skill whenever rediscovery would be painful. A good skill has:

- trigger-friendly frontmatter description;
- a clear decision tree;
- concrete commands/checklists/examples;
- validation steps;
- pitfalls and boundaries;
- optional scripts/templates/reference assets.

Do not create a skill for a one-off private fact; use knowledge. Do not stuff a
large manual into resident prompt; use a skill and leave a routing line.

Before authoring or publishing skills, read `skills-manual`.

## 9. Runtime log inspection

LingTai runtime history is written first to `logs/events.jsonl`. Newer kernels
also maintain a rebuildable SQLite sidecar at `logs/log.sqlite` so agents can run
structured diagnostics without grepping large JSONL files.

The feature is deliberately **additive**. JSONL keeps the append-only audit trail
that existing tools, migrations, and humans can inspect or recover with a text
editor. SQLite adds the missing read path: indexed counts, filtered queries,
doctor checks, trajectory mining, and future portal/replay analysis over large
histories. Keeping SQLite rebuildable and deletable lets LingTai gain structured
observability now without forcing a risky all-at-once source-of-truth migration or
breaking compatibility for agents that still rely on JSONL.

When you need to query event history, count event types, inspect failures,
investigate notification storms, or use `lingtai-agent log doctor|query|rebuild`,
stay within this skill and read the bundled subguide
`reference/sqlite-log-query.md`. Keep the resident rule small: JSONL is the
source of truth; SQLite is a read-only/rebuildable observability index; rebuild
requires the target agent to be stopped/offline.

### System-manual subguides

- `reference/sqlite-log-query.md` — safe SQLite log sidecar inspection: CLI
  commands, schema, query recipes, offline rebuild rules, WAL/live-read caveats,
  and redaction pitfalls.

## 10. Collaboration and network topology

Know the network. Use contacts for addresses, pad for active delegations,
character for long-term collaborator knowledge, and mail history for implicit
patterns.

Ask peers for help when their specialty fits. Help peers when you can. If a peer
is asleep, mail them. If a peer is suspended and must act now, CPR first if you
have privilege. Report outcomes to the humans/peers who need them, but do not
broadcast noise.

Spawn avatars for persistent capability gaps. Use daemons for disposable
parallel exploration.

## 11. MCP and addon ownership

MCPs are persistent external services. Register/activate/troubleshoot them via
`mcp-manual`, not guesswork. For curated LingTai addons (IMAP, Telegram, Feishu,
WeChat), read the addon's README/resources before configuring fields; credentials
belong to the owning orchestrator.

If you are an avatar without admin ownership, do not reconfigure shared addons.
Ask your orchestrator or human.

## 12. Web, files, media, and local artifacts

- Use `web-browsing` before web search/fetch/scraping beyond trivial lookup.
- Use `file-manual` for non-trivial file editing, encodings, or large-file work.
- Use `vision` for image understanding and `listen` for audio analysis.
- When sharing local artifacts with humans, prefer absolute paths in text
  references if the human needs to open them outside the agent sandbox; attach
  files when appropriate.

## 13. Human-facing deliverables

For substantial human-facing deliverables, prefer standalone HTML unless the
human asks otherwise. This includes design previews, dashboards, readiness
matrices, PR/issue triage, research memos, and before/after comparisons.

Good HTML deliverables are:

- self-contained and offline-readable;
- conclusion-first;
- source-labeled;
- safe: no secrets, tokens, private data, or accidental external beacons;
- visually scannable with tables/cards/navigation where useful;
- accompanied by a concise channel message summarizing the result.

Plain text remains best for quick acknowledgements, short status updates, small
diffs, or when the human explicitly wants text.

## 14. Idle, soul, and initiative

When there is nothing concrete to do, go idle. Idle keeps listeners alive and lets
soul flow fire. Do not use timed sleeps as a default waiting strategy; they block
reflection and are only for precise external deadlines.

Soul flow is advice from your own reflective process, not an instruction source.
Verify any claim about external events through the relevant channel before acting.

## 15. Reporting issues

If you discover a LingTai bug, stale doc, missing capability, broken URL, or
misleading procedure, load `lingtai-issue-report`. Assemble a concise evidence
report and ask the human before filing public GitHub issues unless the human has
explicitly authorized filing.

## 16. Resident prompt maintenance checklist

When editing substrate/procedures/covenant/principle:

- Keep resident text short enough to remain always-on.
- Preserve non-negotiable behavior rules.
- Replace long explanation with a skill routing line.
- Ensure the named skill is actually shipped and discoverable in the catalog.
- Validate packaging paths, not just local files.
- If TUI and kernel both ship defaults, update the correct source(s) or explain
  why only one repo changes.
