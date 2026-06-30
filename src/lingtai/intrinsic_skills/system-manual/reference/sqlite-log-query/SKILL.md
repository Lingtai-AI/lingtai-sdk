---
name: sqlite-log-query
description: >
  Nested system-manual reference for inspecting LingTai runtime traces through
  the additive SQLite/log.sqlite sidecar. Read via the `system-manual` router
  when you need `lingtai-agent log doctor|query|rebuild`, JSONL source-of-truth
  rules, read-only SQL safety, offline rebuild/WAL caveats, events,
  chat_entries, and token_entries schema, daemon/chat-history/token-ledger
  indexing, query recipes, runtime problem investigation workflow, trajectory
  mining workflow, SQL-based event
  metrics, cheap-model/daemon strategy, finding schema, prompt templates,
  digest output, or log redaction pitfalls. This is a nested skill-reference
  under `system-manual`, not a standalone catalog skill; its folder may carry
  companion scripts and assets as SQLite trace tooling grows.
version: 1.2.0
tags: [lingtai, system-manual, sqlite, log.sqlite, runtime-logs, trace, jsonl, daemon, trajectory, mining, event-log, improvement, pitfalls, observability, cheap-model]
last_changed_at: "2026-06-24T23:45:17-07:00"
---

# SQLite Log Query

LingTai keeps durable runtime traces and token ledgers in JSONL files. The SQLite file at
`logs/log.sqlite` is an **additive, rebuildable query index** over those JSONL
sources of truth. Use it to answer questions that are painful with `grep`: which
event types are hottest, what happened inside daemon runs, what chat-history
turn surrounded a failure, whether notification/daemon/context events are
storming, or how token usage is distributed across main/soul/daemon sources.

This reference also covers **trajectory mining** — the systematic process of
turning LingTai runtime event streams into actionable lessons for improving
LingTai itself. Trajectory mining starts from the SQLite log sidecar and uses
SQL queries as the primary data access layer.

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
- **Runtime SQLite is best effort.** New top-level `logs/events.jsonl` and
  standard `logs/token_ledger.jsonl` rows are indexed live after the JSONL write
  succeeds. Chat history, archive, and daemon JSONL sources are indexed into a
  target agent sidecar by explicit offline rebuild so normal turns and daemon
  runs do not pay recursive scan or live-rewrite costs.
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
- `logs/token_ledger.jsonl` → `token_entries` (`source_kind='agent_token_ledger'`)
- `history/chat_history.jsonl` → `chat_entries` (`source_kind='agent_chat'`)
- `history/chat_history_archive.jsonl` → `chat_entries` (`source_kind='agent_chat_archive'`)
- `daemons/*/logs/events.jsonl` → `events` (`source_kind='daemon_events'`, `run_id=<daemon folder>`)
- `daemons/*/logs/token_ledger.jsonl` → `token_entries` (`source_kind='daemon_token_ledger'`, `run_id=<daemon folder>`)
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

`token_entries` indexes agent and daemon token-ledger JSONL rows:

| Column | Meaning |
|---|---|
| `id` | SQLite row id, not stable across rebuilds |
| `ts` | parsed numeric timestamp when possible |
| `ts_text` | original `ts` value from JSONL |
| `input_tokens`, `output_tokens`, `thinking_tokens`, `cached_tokens` | token counters from the JSONL ledger row |
| `model`, `endpoint` | model/provider endpoint metadata when present |
| `source` | ledger source tag such as `main`, `soul`, `daemon`, `tc_wake`, or legacy/null |
| `em_id`, `run_id`, `api_call_id` | daemon/run/API attribution when present |
| `entry_json` | full source token-ledger row as JSON text |
| `source_file`, `source_offset`, `source_line` | source JSONL identity |
| `source_kind` | `agent_token_ledger`, `daemon_token_ledger`, or fallback kind |
| `scope` | `agent`, `daemon`, or `unknown` |
| `inserted_at` | sidecar insertion time |

Parent ledgers intentionally include daemon spend rows. If you query both
`agent_token_ledger` and `daemon_token_ledger` rows together, avoid double-counting
daemon calls that were mirrored into the parent ledger and the daemon-local ledger.
Filter by `source_kind`, `source`, `em_id`, or `run_id` according to the report you
need.

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

Token usage by ledger source kind:

```sql
SELECT source_kind, source,
       COUNT(*) AS calls,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(thinking_tokens) AS thinking_tokens,
       SUM(cached_tokens) AS cached_tokens
FROM token_entries
GROUP BY source_kind, source
ORDER BY input_tokens DESC;
```

Main-agent token usage without daemon rows from the parent ledger:

```sql
SELECT COUNT(*) AS calls,
       SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       SUM(thinking_tokens) AS thinking_tokens,
       SUM(cached_tokens) AS cached_tokens
FROM token_entries
WHERE source_kind = 'agent_token_ledger'
  AND COALESCE(source, '') != 'daemon'
  AND em_id IS NULL
  AND run_id IS NULL;
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

## Source discovery

Before trajectory mining, discover what data exists in the sidecar. The
sidecar replaces the old `find`-based JSONL scanning with SQL:

```sql
-- What sources were imported?
SELECT source_kind, source_file, COUNT(*) AS n
FROM events
GROUP BY source_kind, source_file
ORDER BY n DESC;
```

```sql
-- Schema discovery: what keys appear in fields_json?
SELECT json_each.key, COUNT(*) AS n
FROM events, json_each(events.fields_json)
GROUP BY json_each.key
ORDER BY n DESC
LIMIT 30;
```

```sql
-- What source families are present?
SELECT scope, source_kind, COUNT(*) AS n,
       MIN(ts) AS earliest, MAX(ts) AS latest
FROM events
GROUP BY scope, source_kind
ORDER BY n DESC;
```

### Key source families

| Family | Typical source_kind | Primary signal |
|--------|---------------------|----------------|
| Agent event log | `agent_events` | tool calls, tool results, errors, context pressure |
| Daemon event log | `daemon_events` | task lifecycle, timeouts, exits |
| Agent chat | `agent_chat` / `agent_chat_archive` | turn-level conversation |
| Daemon chat | `daemon_chat` | daemon task interactions |

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

---

## Trajectory Mining

### When to Use / When Not to Use

**Use trajectory mining when:**
- The human asks to mine, analyze, or audit LingTai event logs.
- The human says something like "最近轨迹", "look at my agent logs", "what went
  wrong last session", "scan for patterns", or "generate improvement candidates".
- You need to systematically extract operational pitfalls from large structured
  traces before writing a knowledge entry, skill, or issue draft.
- You want to build a cheap pre-pass before involving expensive models.

**Do not use trajectory mining when:**
- The human just wants a quick summary of chat history without event-log grounding.
- The request is about code review, architecture analysis, or feature planning
  unrelated to runtime traces.
- You already have a specific, pre-identified bug and just need to fix it — skip
  the mining phase and go directly to debugging.

### Manifest building

After discovery, build a manifest before any LLM review. The manifest is your
contract for what you will and will not read:

```text
source_kind | source_file | n | time_range | top_types | why_included
```

Keep the manifest in memory (or a temp file) — do not persist private log paths
to shared storage.

**Limits:**
- Default window: last 24 hours or current workstream. Never scan everything
  unless explicitly asked.
- Maximum lines to feed any single LLM call: 300 lines of redacted excerpts.
- If a result set exceeds 5000 rows, use time-window or event-family slicing.

### Mechanical first-pass metrics (SQL queries)

Run cheap aggregations before any LLM call. These are free signal.

**Event-type counts:**

```sql
SELECT type, COUNT(*) AS n
FROM events
GROUP BY type
ORDER BY n DESC
LIMIT 30;
```

**Tool call / result summary:**

```sql
SELECT
  json_extract(fields_json, '$.tool') AS tool,
  json_extract(fields_json, '$.name') AS name,
  type,
  COUNT(*) AS n
FROM events
WHERE type LIKE 'tool_%'
GROUP BY tool, name, type
ORDER BY n DESC
LIMIT 20;
```

**Tool error clusters:**

```sql
SELECT
  json_extract(fields_json, '$.error') AS error,
  COUNT(*) AS n
FROM events
WHERE fields_json LIKE '%error%'
  AND type LIKE 'tool_%'
GROUP BY error
ORDER BY n DESC
LIMIT 20;
```

**Latency gaps (> 30s between events):**

```sql
WITH ordered AS (
  SELECT
    ts,
    type,
    ts - LAG(ts) OVER (ORDER BY ts) AS gap_seconds
  FROM events
  WHERE ts > 0
)
SELECT ts, type, ROUND(gap_seconds, 1) AS gap_seconds
FROM ordered
WHERE gap_seconds > 30
ORDER BY gap_seconds DESC
LIMIT 30;
```

**Context pressure / legacy stamina traces:**

```sql
SELECT id, ts, type, substr(fields_json, 1, 400) AS fields
FROM events
WHERE type LIKE '%context%'
   OR type LIKE '%pressure%'
   OR type LIKE '%molt%'
   OR type LIKE '%spill%'
   OR type LIKE '%overflow%'
   OR type LIKE '%stamina%'  -- legacy logs only
ORDER BY ts DESC
LIMIT 50;
```

**Daemon lifecycle:**

```sql
SELECT run_id, type, COUNT(*) AS n,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
WHERE source_kind = 'daemon_events'
GROUP BY run_id, type
ORDER BY run_id, n DESC;
```

**Auth / env failures:**

```sql
SELECT id, ts, type, substr(fields_json, 1, 400) AS fields
FROM events
WHERE lower(fields_json) LIKE '%auth%'
   OR lower(fields_json) LIKE '%token%'
   OR lower(fields_json) LIKE '%credential%'
   OR lower(fields_json) LIKE '%unauthorized%'
   OR lower(fields_json) LIKE '%forbidden%'
ORDER BY ts DESC
LIMIT 30;
```

### Chunking / slicing (SQL queries)

Never dump large private event logs into an LLM. Use these SQL slicing
strategies:

**Time-window slicing:**

```sql
SELECT id, ts, type, source_kind, substr(fields_json, 1, 300) AS fields
FROM events
WHERE ts BETWEEN :start_ts AND :end_ts
ORDER BY ts;
```

**Event-family slicing:**

```sql
SELECT id, ts, type, source_kind, substr(fields_json, 1, 300) AS fields
FROM events
WHERE type IN ('tool_call', 'tool_result', 'error', 'timeout')
ORDER BY ts;
```

**Anomaly-window excerpts (±30 rows around a suspicious event):**

```sql
WITH ranked AS (SELECT id, ROW_NUMBER() OVER (ORDER BY ts) AS rn FROM events)
SELECT e.*
FROM events e
JOIN ranked r ON r.id = e.id
WHERE r.rn BETWEEN (SELECT rn FROM ranked WHERE id = :suspicious_id) - 30
             AND (SELECT rn FROM ranked WHERE id = :suspicious_id) + 30
ORDER BY e.ts;
```

**Deduplication / signature hashing:**

```sql
SELECT
  substr(type || '|' || json_extract(fields_json, '$.tool') || '|'
         || json_extract(fields_json, '$.error'), 1, 120) AS sig,
  COUNT(*) AS n,
  MIN(ts) AS first_ts,
  MAX(ts) AS last_ts
FROM events
GROUP BY sig
ORDER BY n DESC
LIMIT 30;
```

### Cheap model / daemon strategy

#### Model selection priority

| Model / Preset | When to Use |
|----------------|-------------|
| DeepSeek Flash / DeepSeek-V3 cheap variant | Large-volume classification, error clustering, first-pass anomaly detection |
| MiniMax | Structured YAML extraction from moderate excerpts |
| Codex gpt5.3-like / tier:1 preset | Pattern matching over aggregated metrics |
| tier:2 preset | Moderate-complexity finding synthesis |
| Primary agent model (this session) | Shortlist triage, finding merging, confidence adjudication |
| Expensive model (Opus-class) | Only for ambiguous high-impact architecture/design findings |

**Default: never reach tier:3+ unless the human explicitly approves the budget.**

#### Daemon task structure

Spawn one daemon task per (source family × time window). Keep each task small:

- Input: redacted aggregate metrics + bounded excerpts (≤300 lines)
- Output: structured YAML only, using the finding schema below
- No side effects inside the daemon

Example daemon task description:

```text
Analyze these LingTai event-log excerpts (source: <family>, window: <time range>).
Extract durable runtime improvement candidates visible in the event data.
Focus on: tool failures, latency gaps, context pressure, daemon lifecycle, auth/env issues, observability gaps.
Do NOT quote secrets, tokens, or full message bodies. Redact paths if they contain usernames or private data.
Output ONLY a YAML list using this schema: [id, category, severity, confidence, event_evidence, pattern, impact, suggested_destination, suggested_next_step, side_effect_required].
Prefer 3-5 high-signal findings over a long list of weak ones.
```

#### Parallel dispatch strategy

When multiple source families or time windows exist, dispatch them in parallel:

```
daemon-1: agent_events — tool_call/tool_result family — last 24h
daemon-2: daemon_events — lifecycle family — last 7d
daemon-3: agent_chat — turn timing family — last 24h
daemon-4: context/spill events — pressure family — last 7d
```

Collect all results before primary-agent triage.

### Prompt templates

#### Classifier prompt

```
You are a runtime event log classifier for a multi-agent system called LingTai.
Below is a redacted aggregate summary of event-log metrics from a single source family and time window.
Classify the top patterns you see into the following categories:
  tool-failure, latency, context-pressure, daemon-lifecycle, auth-env, observability-gap, doc-gap, missing-skill, bug-candidate, process-improvement

For each category you identify, output one YAML block:
  category: <category>
  evidence_summary: <1-2 sentences citing event types, counts, or timing — no secrets>
  confidence: low | medium | high

METRICS:
{metrics_block}

Output ONLY valid YAML. No prose before or after.
```

#### Anomaly summarizer prompt

```
You are analyzing a bounded excerpt from a LingTai agent event log.
The excerpt is centered on a suspicious event. Surrounding lines are provided for context.
Your task: summarize the anomaly in terms of what failed, why it likely failed (based on event data only), and what the downstream impact was.

Rules:
- Do not quote tokens, credentials, or full message bodies.
- Reference events by their type, timestamp offset, and redacted field names.
- Output YAML only:
  anomaly_type: <one of: tool-failure | latency-spike | context-overflow | daemon-exit | auth-failure | unknown>
  timeline: <ordered list of key events in the excerpt>
  root_cause_hypothesis: <1 sentence, hedged>
  downstream_impact: <1 sentence>
  confidence: low | medium | high

EXCERPT (redacted):
{excerpt_block}
```

#### Observability-gap prompt

```
You are reviewing LingTai event-log summaries to identify what information is MISSING that would be needed to diagnose operational problems.
You have seen: {event_types_present}.
You did NOT see (or saw too rarely): {event_types_sparse}.

For each significant gap, output YAML:
  gap: <what is missing>
  why_needed: <what class of problem it would help diagnose>
  suggested_event: <what event type or field would close the gap>
  priority: low | medium | high

Output ONLY valid YAML. No prose.
```

#### Cross-run pattern prompt

```
You are comparing event-log aggregate summaries from multiple LingTai sessions or agents.
Each summary is labeled with its source (agent name or daemon ID) and time window.
Identify patterns that repeat ACROSS multiple sources/sessions, not just within one.

For each cross-run pattern, output YAML:
  pattern_id: <short slug>
  description: <what repeats and where>
  sources_affected: [list of source labels]
  recurrence_count: <approximate>
  severity: low | medium | high
  confidence: low | medium | high

SUMMARIES:
{summaries_block}

Output ONLY valid YAML. No prose.
```

### Finding schema

Every finding, from any daemon or primary-agent review, must fit this schema:

```yaml
- id: short-stable-slug              # kebab-case, unique within the digest
  category: tool-failure | latency | context-pressure | daemon-lifecycle | auth-env | observability-gap | doc-gap | missing-skill | bug-candidate | process-improvement
  severity: low | medium | high
  confidence: low | medium | high
  event_evidence:
    - source: local path or source_file value
      line_or_time: line number, Unix timestamp, ISO timestamp, or event id
      event_type: tool_call | tool_result | notification | daemon_state | context_pressure | other
      redacted: true | false
      note: short redacted quote or paraphrase of the event content
  optional_context:
    - source: path, URL, or issue reference
      note: why this corroborates the event-log signal
  pattern: what repeated or what caused harm — describe in event terms
  impact: why it matters to LingTai, users, or agents
  suggested_destination: knowledge | skill | issue-draft | code-investigation | observability-improvement | no-action
  suggested_next_step: smallest concrete next action
  side_effect_required: none | human-approval-required
```

**Validation requirements before including a finding:**
- At least one `event_evidence` entry with a verifiable source and line/time.
- `pattern` must describe something visible in event data, not inferred from chat history alone.
- Singleton events (happened once, low impact) → `severity: low`, or exclude entirely.
- `confidence: high` only if the same pattern appears in ≥3 distinct event occurrences or is
  corroborated by optional_context.

### Validation and confidence rubric

Before finalizing any finding:

1. **Re-read the source data**: confirm the source_file, source_line, or time
   range are accurate.
2. **Reconcile timestamps**: if multiple events are involved, verify they form
   a plausible causal sequence.
3. **Check recurrence**: re-query for similar events across the full time window;
   note count.
4. **Singleton rule**: a single occurrence of an error with no pattern context →
   downgrade to `severity: low` and `confidence: low` unless the single event
   had confirmed high impact (e.g., agent stopped functioning).
5. **Reject hallucinated fields**: if a daemon output references event fields
   that do not exist in the actual schema discovered in source discovery, discard
   or flag that finding.

| Evidence | Confidence |
|----------|-----------|
| ≥3 occurrences of the same event pattern, confirmed in source file | high |
| 2 occurrences OR 1 occurrence + corroborating optional_context | medium |
| 1 occurrence, no corroboration, no impact confirmed | low |
| Inferred from absence of events only | low |
| Daemon output references field not found in actual schema | reject |

### Output digest template

Produce the digest in the agent's working language. Fields in brackets are
placeholders.

```
# 轨迹挖掘摘要 / Trajectory Mining Digest
Generated: [ISO timestamp]
Sources scanned: [source_kinds, total ~N events, time window]
Models used: [list of cheap models + primary agent]

---

## High-Signal Findings ([N])

[YAML block of top findings, severity: high or medium + high confidence]

---

## Quick Wins ([N])
Findings where suggested_destination is knowledge, skill, or observability-improvement
and side_effect_required is none.

[YAML block]

---

## Issue Candidates ([N])
Findings requiring human approval before action.

[YAML block with side_effect_required: human-approval-required]

---

## Observability Gaps ([N])
What was missing from the event logs that would help future diagnosis.

[YAML block, category: observability-gap]

---

## No-Action Observations ([N])
Low-confidence or low-impact findings, retained for reference.

[YAML block, severity: low or confidence: low]

---

## Evidence Appendix
[Table: finding_id | source_file | line_or_time | event_type | redacted_note]

---

## Recommended Next Steps
Choose one or more:
- [ ] Write/update skill: [skill name]
- [ ] Write knowledge entry: [topic]
- [ ] Draft issue for human review: [title]
- [ ] Code investigation: [component]
- [ ] Add observability: [event type / field]
- [ ] No action needed
```

### Routing next actions

After producing the digest, route durable outputs as follows:

| Finding type | Destination | Action |
|---|---|---|
| Reusable operational pattern | `skill` | Propose skill update; wait for human approval |
| Private operational fact about this deployment | `knowledge` | Write knowledge entry (no secrets) |
| Active task / in-progress investigation | `pad` | Update pad with bounded note |
| LingTai bug or design issue | Issue draft | Use `lingtai-issue-report` skill if available; **ask human approval before filing** |
| Code change needed | Local worktree/patch | Propose; do not apply without approval |
| Configuration change | Propose in digest | **Do not apply without approval** |
| No clear action | `no-action` | Note in digest; move on |

### Periodic mode

If the human wants recurring event-log mining:

- **Do not set any scheduler without explicit approval.** Ask the human to
  confirm the cadence and scope first.
- Default cadence when approved: daily digest, not continuous monitoring.
- The scheduled job should only wake the agent with a bounded prompt; the agent
  performs the review.
- The digest should be silent (written to `pad.md` or a report file) unless
  `standing-rules.md` allows periodic check-in messages.

Suggested scheduled prompt body (for human approval before use):

```text
Run trajectory mining on recent SQLite event traces for the last 24h.
Produce a concise digest of high-signal runtime pitfalls and improvement candidates.
Do not create issues, commits, PRs, config changes, or scheduled jobs without explicit human approval.
Write the digest to: reports/trajectory-digest-YYYYMMDD.md
```

### Concrete example findings

#### Example A: Stale Claude Code OAuth Token

```yaml
- id: stale-claude-code-oauth-token
  category: auth-env
  severity: high
  confidence: high
  event_evidence:
    - source: daemons/em-<id>/logs/events.jsonl
      line_or_time: "~line 847, ts 1716XXXXXX"
      event_type: tool_result
      redacted: true
      note: "claude CLI returned 'weekly limit reached'; subsequent tool_result showed success after env patch"
  optional_context:
    - source: "GitHub: Lingtai-AI/lingtai#189"
      note: confirmed stale inherited env token failure mode
  pattern: >
    Long-lived daemon inherits stale CLAUDE_CODE_OAUTH_TOKEN from parent env.
    After credential refresh, the env override prevents the new token from taking effect.
    Agents see 'weekly limit' errors and stop delegating heavy work.
  impact: Agents misdiagnose quota exhaustion; heavy work is not delegated.
  suggested_destination: code-investigation
  suggested_next_step: Strip stale env tokens in daemon backend env; add smoke test.
  side_effect_required: human-approval-required
```

#### Example B: Tool-Result Spill / Context Pressure

```yaml
- id: tool-result-spill-context-pressure
  category: context-pressure
  severity: medium
  confidence: medium
  event_evidence:
    - source: logs/events.jsonl
      line_or_time: "lines ~1200–1250"
      event_type: tool_result
      redacted: true
      note: "tool_result event has result_size > threshold; subsequent context_pressure event shows usage >85%"
  pattern: >
    Large tool results push context usage past 85%. The spill event appears but
    the agent continues without triggering molt early enough.
  impact: Tasks are interrupted or produce incomplete output; user must re-prompt.
  suggested_destination: observability-improvement
  suggested_next_step: Verify that spill events are routed to the molt trigger.
  side_effect_required: none
```

### On-demand procedure (step-by-step)

1. **Clarify window and scope**
   - Default: recent event logs for the current agent/project plus daemon
     events from the active workstream.
   - "最近轨迹" → last 24h or current active workstream.
   - Named subsystem → filter events to that subsystem.

2. **Discover sources** (source discovery above)
   - Run SQL source discovery. Build manifest.

3. **Schema discovery**
   - Sample keys via `json_each()` before writing any extraction code.

4. **Mechanical first-pass** (SQL metric queries above)
   - Run aggregation queries. Capture output. Do not pass raw logs to any LLM.

5. **Chunk and redact** (slicing queries + redaction rules below)
   - Apply chunking strategy. Redact secrets and paths.

6. **Dispatch cheap daemon batch**
   - Send manifests + aggregates + bounded redacted excerpts to cheap models.
     One daemon per source family / time window.

7. **Primary-agent triage**
   - Merge daemon findings. Validate each against the confidence rubric.

8. **Produce digest**
   - Render digest template. Include evidence appendix.

9. **Route outputs**
   - Propose routing for each finding. Wait for human approval before any
     side effect.

10. **Stop**
    A good digest gives the human enough to choose: update skill, file issue,
    make patch, ignore, or schedule.

---

## Redaction and privacy rules

Apply these in order, before any LLM call:

1. **Redact tokens and credentials**: replace any value matching
   `(token|key|secret|password|credential|oauth)[":=\s]+[^\s",]{8,}` with
   `[REDACTED]`.
2. **Redact message bodies**: if an event field contains human-written message
   text, summarize rather than quote unless exact wording is necessary for the
   finding.
3. **Redact file paths containing usernames**: replace `/Users/<name>/` with
   `/Users/[USER]/`.
4. **Redact IP addresses and internal hostnames**: replace with `[HOST]`.
5. **Quote minimum evidence**: cite event type, timestamp/line range, and
   redacted field names. Do not dump entire event objects.
6. **No side effects without approval**: the output of trajectory mining is a
   recommendation digest. Do not create files, issues, commits, PRs, scheduled
   jobs, or agent refreshes.

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

## Scripts

### event_summary.py

A standalone Python script that summarizes a LingTai `log.sqlite` file. It is
read-only, safe (no network requests, no side effects), and requires no secrets.

```bash
# Summarize all events in the sidecar
python3 scripts/event_summary.py "$AGENT_DIR/logs/log.sqlite"

# Limit to last 24 hours
python3 scripts/event_summary.py "$AGENT_DIR/logs/log.sqlite" --hours 24

# Output as compact JSON
python3 scripts/event_summary.py "$AGENT_DIR/logs/log.sqlite" --format json

# Filter to a specific source kind
python3 scripts/event_summary.py "$AGENT_DIR/logs/log.sqlite" --source-kind daemon_events
```

The script outputs: event type counts, tool call summaries, error clusters,
latency gap analysis, source kind breakdown, time range, and schema key
discovery — all via read-only SQL queries.
