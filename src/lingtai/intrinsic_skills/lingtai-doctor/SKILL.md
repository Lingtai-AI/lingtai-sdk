---
name: lingtai-doctor
description: >
  Read-only health diagnostics for LingTai agents and bots. Use when an agent
  appears offline or unreachable, when a machine migration may have left stale
  MCP/addon command paths, when heartbeat/status/process/notification/log
  surfaces disagree, or before deciding whether to mail, refresh, CPR, or edit
  persistent configuration. Includes a bundled doctor.py script for layered
  local checks without exposing secrets.
version: 0.1.0
tags: [doctor, diagnostics, mcp, addons, heartbeat, migration, recovery]
---

# LingTai Doctor

`lingtai-doctor` is the first stop when a LingTai agent or bot looks dead but
the evidence is mixed: Telegram/Feishu/WeChat cannot reach it, the TUI says it
is offline, a heartbeat is fresh, MCP configuration points at an old runtime, or
logs/notifications/status files disagree.

The goal is **diagnosis before repair**. The bundled script is read-only. It
summarizes local evidence, redacts secrets, and suggests safe next steps; it does
not edit `init.json`, touch mailboxes, refresh agents, or kill processes.

## Quick use

From any shell with access to an agent workdir:

```bash
python3 src/lingtai/intrinsic_skills/lingtai-doctor/scripts/doctor.py \
  --agent-dir /path/to/project/.lingtai/mimo-1
```

From inside an agent process where `LINGTAI_AGENT_DIR` is set:

```bash
python3 .library/intrinsic/capabilities/lingtai-doctor/scripts/doctor.py
```

For machine-readable output:

```bash
python3 .../doctor.py --agent-dir /path/to/agent --json
```

For a packaging/sanity check:

```bash
python3 .../doctor.py --self-test
```

## What it checks

The script layers evidence so you do not confuse one broken surface with a dead
agent:

1. **Identity / lifecycle files** — `.agent.json`, `.status.json`, and
   `.agent.heartbeat` freshness.
2. **Process evidence** — best-effort `ps` scan for `lingtai run <agent-dir>`.
3. **Notifications and logs** — channel files, mtimes, sizes, and common log
   files such as `logs/events.jsonl`, `logs/agent.log`, and token ledgers.
4. **Internal mail footprint** — inbox/outbox counts without message bodies.
5. **MCP/addon configuration** — `init.json` top-level `mcp` entries and
   `mcp_registry.jsonl` stdio commands. Commands are checked for existence and
   executability (or `PATH` resolution). Environment values are redacted; only
   path-like existence facts are reported.
6. **Migration drift hints** — stale Linux `/home/...` paths on macOS-style
   hosts, stale macOS `/Users/...` paths on Linux-style hosts, and likely
   `~/.lingtai-tui/runtime/venv/bin/python` replacements.
7. **First-party MCP server imports** — if a configured stdio command points at a
   usable Python executable, the script can try importing configured LingTai
   curated MCP modules (`lingtai.mcp_servers.telegram`, `lingtai.mcp_servers.feishu`,
   `lingtai.mcp_servers.wechat`, `lingtai.mcp_servers.whatsapp`,
   `lingtai.mcp_servers.imap`) without reading credentials.

## Reading the result

The top-level severity is:

- **OK** — no obvious local mismatch was found.
- **WARN** — at least one surface looks stale, missing, or inconsistent.
- **FAIL** — a critical local file/config/path is missing or broken.

Suggested recovery order:

1. If `.agent.heartbeat` is fresh, the agent is probably alive. Internal email
   should wake it even if an external addon is broken.
2. If heartbeat/process are dead, CPR may be appropriate; if the process is
   alive but status/logs are stale, investigate before CPR.
3. If MCP stdio commands point at missing runtimes, back up `init.json` and
   `mcp_registry.jsonl`, replace stale command paths, then refresh the agent.
4. If notifications are stale while the agent is healthy, clear the producer
   channel after reading/handling it; do not use generic dismiss for producer
   state unless you know it is only a stale mirror.

## Scope and follow-ups

This intrinsic skill is the shared diagnostic foundation. TUI `/doctor` should
become a thin caller of these scripts instead of maintaining a separate copy of
logic. Kernel-level `mcp(action="show")` executable validation is also a natural
follow-up, but the doctor script remains useful because it checks the whole
agent footprint, not only MCP registry syntax.
