---
name: sqlite-log-query
description: >
  Nested system-manual reference for inspecting LingTai runtime traces through
  the additive SQLite/log.sqlite sidecar. Read via the `system-manual` router
  when you need `lingtai-agent log doctor|query|rebuild`, JSONL source-of-truth
  rules, read-only SQL safety, offline rebuild/WAL caveats, events and
  chat_entries schema, daemon/chat-history indexing, query recipes, runtime
  problem investigation workflow, or log redaction pitfalls. This is a nested
  skill-reference under `system-manual`, not a standalone catalog skill; its
  folder may carry scripts/assets as SQLite trace tooling grows.
version: 1.0.0
tags: [lingtai, system-manual, sqlite, log.sqlite, runtime-logs, trace, jsonl, daemon]
---

# SQLite Log Query

LingTai keeps durable runtime traces in JSONL files. The SQLite file at
`logs/log.sqlite` is an **additive, rebuildable query index** over those JSONL
sources of truth. Use it to answer questions that are painful with `grep`: which
event types are hottest, what happened inside daemon runs, what chat-history
turn surrounded a failure, or whether notification/daemon/context events are
storming.

## Safety contract

- **JSONL is authoritative.** `logs/log.sqlite` is derived; deleting it should not
  delete facts.
- **Prefer the CLI.** Use `lingtai-agent log ...` instead of opening the DB for
  writes yourself.
- **Queries are read-only.** `log query` accepts read-only `SELECT`, CTE (`WITH ... SELECT`), and
  `EXPLAIN` statements and opens the sidecar through the kernel read-only
  inspection path.
- **Rebuild is offline.** `log rebuild` requires the agent working-directory lock;
  if the agent is running, stop/sleep/lull/suspend it first as appropriate.
- **Runtime SQLite is best effort.** New top-level `logs/events.jsonl` rows are
  indexed live after the JSONL write succeeds. Chat history and daemon JSONL are
  indexed by explicit offline rebuild so normal turns and daemon runs do not pay
  recursive scan or live-rewrite costs.
- **Live queries are snapshots.** Runtime writes use SQLite WAL mode. The query
  path is intentionally non-mutating, so for a complete historical snapshot stop
  the agent and run `log rebuild` before querying.
- **Never paste secrets.** Logs and chat history can contain URLs, tokens,
  prompts, and user data. Redact before sharing.

## Commands

Set a variable for the target agent directory:

```bash
AGENT_DIR=/path/to/project/.lingtai/agent-name
```

Check whether the sidecar exists and is readable:

```bash
lingtai-agent log doctor "$AGENT_DIR"
```

If `doctor` reports `{"status":"missing"...}` or the sidecar is stale/corrupt,
rebuild **only while the target agent is stopped/offline**:

```bash
lingtai-agent log rebuild "$AGENT_DIR"
```

`log rebuild` scans the known JSONL trace surfaces under the target agent:

- `logs/events.jsonl` → `events` (`source_kind='agent_events'`)
- `history/chat_history.jsonl` → `chat_entries` (`source_kind='agent_chat'`)
- `history/chat_history_archive.jsonl` → `chat_entries` (`source_kind='agent_chat_archive'`)
- `daemons/*/logs/events.jsonl` → `events` (`source_kind='daemon_events'`, `run_id=<daemon folder>`)
- `daemons/*/history/chat_history.jsonl` → `chat_entries` (`source_kind='daemon_chat'`, `run_id=<daemon folder>`)

Run a read-only query:

```bash
lingtai-agent log query "$AGENT_DIR" \
  'SELECT id, ts, type, agent_address, substr(fields_json, 1, 240) AS fields
   FROM events
   ORDER BY ts DESC
   LIMIT 20'
```

The CLI prints JSON. Pipe to `jq` when available:

```bash
lingtai-agent log query "$AGENT_DIR" \
  'SELECT type, COUNT(*) AS n FROM events GROUP BY type ORDER BY n DESC LIMIT 20' \
  | jq .
```

## Schema quick reference

`events` indexes top-level agent runtime events and daemon run events:

| Column | Meaning |
|---|---|
| `id` | SQLite row id, not a stable cross-rebuild event identifier |
| `ts` | event timestamp as a numeric epoch-like value; ISO strings are parsed when possible |
| `type` | event `type` field, or daemon `event` field |
| `agent_address` | event `address` field when present |
| `agent_name_snapshot` | event `agent_name` field when present |
| `fields_json` | the remaining event fields as JSON text |
| `source_file` | JSONL file imported from |
| `source_offset` | byte offset in the JSONL source; unique with `source_file` |
| `source_line` | 1-based JSONL line number |
| `source_kind` | `agent_events`, `daemon_events`, or fallback kind |
| `scope` | `agent`, `daemon`, or `unknown` |
| `run_id` | daemon run folder name for daemon rows |
| `inserted_at` | sidecar insertion time |

`chat_entries` indexes agent and daemon chat-history JSONL rows:

| Column | Meaning |
|---|---|
| `id` | SQLite row id, not stable across rebuilds |
| `ts` | parsed numeric timestamp when a row has `ts`/`timestamp`, else `0` |
| `ts_text` | original timestamp text/value as stored in JSONL |
| `role` | chat role (`user`, `assistant`, etc.) when present |
| `kind` | LingTai daemon user-entry kind (`task`, `tool_results`, `followup`) when present |
| `turn` | daemon turn number when present |
| `content_text` | best-effort extracted plain text from `text` or content blocks |
| `entry_json` | full source chat row as JSON text |
| `source_file`, `source_offset`, `source_line` | source JSONL identity |
| `source_kind` | `agent_chat`, `agent_chat_archive`, `daemon_chat`, or fallback kind |
| `scope` | `agent`, `daemon`, or `unknown` |
| `run_id` | daemon run folder name for daemon rows |
| `inserted_at` | sidecar insertion time |

Maintenance tables:

- `schema_migrations(version, name, applied_at)` records sidecar schema version.
- `import_cursors(source_file, byte_offset, line_no, updated_at)` records the last
  rebuild/import cursor for each JSONL source.

## Query recipes

Recent events:

```sql
SELECT id, ts, type, source_kind, run_id, substr(fields_json, 1, 300) AS fields
FROM events
ORDER BY ts DESC
LIMIT 50;
```

Event type counts across agent + daemon events:

```sql
SELECT source_kind, type, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
GROUP BY source_kind, type
ORDER BY n DESC
LIMIT 50;
```

Recent chat-history entries:

```sql
SELECT id, source_kind, run_id, role, kind, turn, substr(content_text, 1, 400) AS text
FROM chat_entries
ORDER BY id DESC
LIMIT 50;
```

Join daemon tool events with daemon chat rows by `run_id`:

```sql
SELECT e.run_id, e.ts, e.type, json_extract(e.fields_json, '$.name') AS tool,
       c.role, c.turn, substr(c.content_text, 1, 240) AS chat
FROM events e
LEFT JOIN chat_entries c ON c.run_id = e.run_id AND c.turn = json_extract(e.fields_json, '$.turn')
WHERE e.source_kind = 'daemon_events'
ORDER BY e.ts DESC
LIMIT 100;
```

Search for errors or failures:

```sql
SELECT id, ts, source_kind, run_id, type, substr(fields_json, 1, 500) AS fields
FROM events
WHERE lower(type) LIKE '%error%'
   OR lower(type) LIKE '%fail%'
   OR lower(fields_json) LIKE '%error%'
   OR lower(fields_json) LIKE '%traceback%'
ORDER BY ts DESC
LIMIT 100;
```

Look for notification storms:

```sql
SELECT type, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
WHERE type LIKE 'notification%'
   OR fields_json LIKE '%notification%'
GROUP BY type
ORDER BY n DESC;
```

Search chat-history text:

```sql
SELECT source_kind, run_id, role, turn, substr(content_text, 1, 500) AS text
FROM chat_entries
WHERE lower(content_text) LIKE '%sqlite%'
ORDER BY id DESC
LIMIT 100;
```

Inspect one event's full JSON payload:

```sql
SELECT id, type, fields_json
FROM events
WHERE id = 123;
```

Use SQLite JSON functions when available:

```sql
SELECT
  type,
  json_extract(fields_json, '$.tool') AS tool,
  json_extract(fields_json, '$.error') AS error
FROM events
WHERE fields_json LIKE '%error%'
ORDER BY ts DESC
LIMIT 50;
```

If JSON functions are unavailable in the local SQLite build, fall back to
`fields_json LIKE ...` and inspect the returned JSON text.

## Workflow: investigate a suspected runtime problem

1. Identify the agent directory. If unsure, use the `.lingtai/<agent>` directory
   shown in the agent's identity/pad or ask the orchestrator.
2. Stop the target agent if exact complete history matters, then run
   `lingtai-agent log rebuild "$AGENT_DIR"`. Otherwise begin with `doctor` and
   live event queries.
3. Start broad: event/source-kind counts and recent rows.
4. Narrow by time/type/text. Include `source_kind` and `run_id` in queries when
   daemon evidence matters.
5. Cross-check surprising findings against source JSONL (`logs/events.jsonl`,
   `history/chat_history*.jsonl`, daemon subdirectories) before filing bugs or
   making claims.
6. When reporting, quote minimal evidence and redact secrets.

## Pitfalls

- Do not treat `log.sqlite` as a coordination database. It is an observability
  index, not agent state.
- Do not rebuild a live agent by bypassing the CLI lock; that risks racing the
  runtime logger.
- Do not share raw `fields_json` or `entry_json` blindly; they may contain private
  content.
- Do not assume `id` survives rebuilds. Use `source_file/source_offset`, time,
  `run_id`, and surrounding context for durable references.
- If a query returns fewer rows than expected on a live agent, remember the WAL
  snapshot and explicit-rebuild caveats; stop/rebuild or inspect JSONL.
