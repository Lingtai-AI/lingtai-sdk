<!-- Subguide of system-manual. Not a standalone skill catalog entry. -->

# SQLite Log Query

LingTai keeps the durable runtime event log in `logs/events.jsonl`. The SQLite
file at `logs/log.sqlite` is an **additive, rebuildable query index** over that
JSONL source of truth. Use it to answer questions that are painful with `grep`:
which event types are hottest, when a failure started, which tool calls emitted
errors, or whether notification/daemon/context events are storming.

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
- **Live queries are snapshots.** Runtime writes use SQLite WAL mode. The query
  path is intentionally non-mutating, so for a complete historical snapshot stop
  the agent and run `log rebuild` from `events.jsonl` before querying. For the
  newest live facts, inspect `logs/events.jsonl` directly.
- **Never paste secrets.** Logs can contain URLs, tokens, prompts, and user data.
  Redact before sharing.

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

`events` is the main table:

| Column | Meaning |
|---|---|
| `id` | SQLite row id, not a stable cross-rebuild event identifier |
| `ts` | event timestamp as a numeric epoch-like value |
| `type` | event type string |
| `agent_address` | event `address` field when present |
| `agent_name_snapshot` | event `agent_name` field when present |
| `fields_json` | the remaining event fields as JSON text |
| `source_file` | JSONL file imported from, usually `logs/events.jsonl` |
| `source_offset` | byte offset in the JSONL source; unique with `source_file` |
| `inserted_at` | sidecar insertion time |

Maintenance tables:

- `schema_migrations(version, name, applied_at)` records sidecar schema version.
- `import_cursors(source_file, byte_offset, line_no, updated_at)` records the last
  rebuild/import cursor.

## Query recipes

Recent events:

```sql
SELECT id, ts, type, substr(fields_json, 1, 300) AS fields
FROM events
ORDER BY ts DESC
LIMIT 50;
```

Event type counts:

```sql
SELECT type, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
GROUP BY type
ORDER BY n DESC
LIMIT 50;
```

Search for errors or failures:

```sql
SELECT id, ts, type, substr(fields_json, 1, 500) AS fields
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
2. Run `lingtai-agent log doctor "$AGENT_DIR"`.
3. If the sidecar is missing/stale and exact history matters, stop the target
   agent and run `lingtai-agent log rebuild "$AGENT_DIR"`.
4. Start broad: event type counts and recent rows.
5. Narrow by time/type/text. Keep queries read-only (`SELECT`, `WITH ... SELECT`, or `EXPLAIN`).
6. Cross-check surprising findings against `logs/events.jsonl`, `agent.log`, or
   daemon subdirectories before filing bugs or making claims.
7. When reporting, quote minimal evidence and redact secrets.

## Pitfalls

- Do not treat `log.sqlite` as a coordination database. It is an observability
  index, not agent state.
- Do not rebuild a live agent by bypassing the CLI lock; that risks racing the
  runtime logger.
- Do not share raw `fields_json` blindly; it may contain private content.
- Do not assume `id` survives rebuilds. Use `source_file/source_offset`, time,
  and surrounding context for durable references.
- If a query returns fewer rows than expected on a live agent, remember the WAL
  snapshot caveat; stop/rebuild or inspect JSONL.
