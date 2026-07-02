# Runtime session vs. agent session — explicit kernel objects

Status: **spec-first** (this branch ships the spec + a benchmark/prototype of the
optimized rebuild path; see "Implementation status" at the end for exactly what
is code vs. spec).

Date: 2026-07-02
Branch: `spec/runtime-agent-session-objects-20260702`

## Why this document exists

The kernel already tracks two different notions of "session" but has never named
them as first-class objects, and the naming that *does* exist is scattered across
token-accounting helpers on `SessionManager`. That ambiguity produced real bugs
(#679: the injected `token_usage.session` half was fed the since-refresh runtime
deltas instead of the since-molt cumulative totals, so a refresh zeroed a number
that must survive refresh).

Jason approved making the two notions **explicit named objects** with sharp
lifecycle boundaries, so kernel code stops confusing them:

- **runtime session** — the current runtime lifecycle segment.
- **agent session** — the agent mind segment bounded by `molt_count`.

This spec defines both objects, their ownership, their lifecycle boundaries, the
event/rebuild model for the agent session, the mandatory *optimized* rebuild
path (no full `events.jsonl` scans in the normal case), the benchmark
requirement, migration/back-compat, and the consumer API surface.

Code is truth. Every claim below cites the code it is grounded in
(`file:line`), read at branch base `origin/main` (`e53fb20`).

---

## 1. Definitions

### 1.1 Runtime session

> The current runtime lifecycle segment. It exists only to *name and hold* the
> current in-memory runtime segment so code does not confuse it with the agent
> session.

Properties (contract):

- **Boundary:** process start / `refresh` / agent restart. Each refresh/restart
  is simply a **new empty in-memory object**.
- **No id.** A runtime session is never assigned an identifier. It is "the
  current one" and nothing else.
- **Not rebuilt from events.** On refresh/restart it starts empty. It is *not*
  reconstructed from `events.jsonl` or any log.
- **No new product/UI behavior.** It must not grow features. It is a naming and
  containment device for the current runtime segment.

What plays this role today (implicitly): the since-refresh delta baselines on
`SessionManager` — `_session_baseline_input_tokens` / `_session_baseline_cached_tokens`
/ `_session_baseline_api_calls` (`src/lingtai_kernel/session.py:143-145`), read
out by `get_runtime_session_token_usage()`
(`src/lingtai_kernel/session.py:649-694`) as
`current_total - session_baseline`. These are re-anchored to the restored totals
on `restore_token_state` (`src/lingtai_kernel/session.py:798-816`) and on molt via
`reset_runtime_session_token_usage` (`src/lingtai_kernel/session.py:761-770`), i.e.
they already reset to "zero" at each runtime-session boundary. That is exactly
the runtime-session boundary this spec names.

### 1.2 Agent session

> The agent's mind segment bounded by `molt_count`. One agent session per molt
> generation. It survives refresh/restart — a refresh does not start a new agent
> session, it *rebuilds the current one*.

Properties (contract):

- **Identity = `molt_count`.** Do **not** introduce a new agent-session id. The
  existing `molt_count` *is* the agent-session key.
- **Boundary:** a successful molt. Each molt increments `molt_count`
  (`src/lingtai_kernel/intrinsics/psyche/_molt.py:356`,
  `src/lingtai_kernel/intrinsics/psyche/_molt.py:595`) and starts a new agent
  session.
- **Survives refresh/restart.** On refresh/restart the kernel **rebuilds** the
  current agent session object for the current `molt_count` from
  trajectory/events. It does not create a fresh empty one.
- **`events.jsonl` is the source of truth** for the rebuild (see §3), but the
  normal-case rebuild must **not full-scan** a giant events file (see §4).

`molt_count` today: initialized from the persisted manifest
(`self._molt_count = existing.get("molt_count", 0)`,
`src/lingtai_kernel/base_agent/__init__.py:427`), written back into the manifest
(`src/lingtai_kernel/base_agent/identity.py:81`), incremented only inside molt
(`src/lingtai_kernel/intrinsics/psyche/_molt.py:356,595`), and even read live off
disk by the Codex adapter for its per-molt cache key
(`src/lingtai/llm/openai/adapter.py:747 _read_molt_count`). It is already the de
facto agent-session key; this spec makes that role explicit.

### 1.3 The relationship, stated plainly

```
process lifetime:   [ runtime session A ][ runtime session B ][ runtime session C ] ...
                     ^start              ^refresh            ^restart

molt lifetime:      [ agent session (molt_count=7) .......... ][ agent session (molt_count=8) ...
                                          ^molt boundary (psyche_molt event, molt_count 7->8)
```

- A refresh/restart cuts a new **runtime session** but keeps the **agent
  session** (same `molt_count`, rebuilt).
- A molt cuts a new **agent session** (`molt_count++`) and also re-anchors the
  runtime-session token baselines (§5).
- Therefore: one agent session spans ≥1 runtime sessions; one runtime session
  belongs to exactly one agent session (the current `molt_count`).

---

## 2. Object ownership

Both objects are owned by the kernel and hang off the session/lifecycle layer.
No wrapper (`src/lingtai/`) types — the kernel must not depend on the wrapper
(`src/lingtai_kernel/ANATOMY.md:116`).

| Object | Owner | Key | Lifetime | Rebuilt from events? |
|---|---|---|---|---|
| `RuntimeSession` | `SessionManager` (`src/lingtai_kernel/session.py`) | none | process start → refresh/restart | **No** — fresh empty each boundary |
| `AgentSession` | `SessionManager`, keyed by `BaseAgent._molt_count` | `molt_count` | molt → molt | **Yes** — rebuilt for current `molt_count` on start |

Rationale for keeping both on `SessionManager`: that is where the token
bookkeeping and the two existing accessors already live
(`get_runtime_session_token_usage`, `get_token_usage`), and where molt/refresh
already call `reset_session_token_usage` / `restore_token_state`. Putting the
named objects anywhere else would fork the source of these numbers.

---

## 3. Event / rebuild model (agent session)

### 3.1 The molt-boundary event

The canonical agent-session boundary marker in `events.jsonl` is the
**`psyche_molt`** event. Both molt paths emit it with `molt_count`:

- agent-initiated molt: `src/lingtai_kernel/intrinsics/psyche/_molt.py:434-441`
  (`type="psyche_molt"`, `molt_count`, `before_tokens`, `after_tokens`,
  `kept_tool_calls`, `kept_last`).
- system-forced molt (`context_forget`):
  `src/lingtai_kernel/intrinsics/psyche/_molt.py:702-711` (same `type`, plus
  `initiator="system"`, `source`).

The `molt_count` recorded on a `psyche_molt` event is the value **after** the
increment (the increment happens before the log at
`_molt.py:356`/`595`; the log reads `agent._molt_count`). So the newest
`psyche_molt` event whose `molt_count == current molt_count` marks the **start**
of the current agent session. If no such event exists (fresh agent, never
molted, `molt_count == 0`), the agent session starts at the beginning of the
trajectory.

### 3.2 What "rebuild the agent session" means

The current agent session object is derived state: its content is the aggregate
of the events that belong to the current molt generation. For the initial,
minimal object (§7) that content is:

- `molt_count` (identity),
- `started_at` (ts of the boundary `psyche_molt`, or agent creation for
  molt_count 0),
- `boundary_source` (`agent` / `system` / `boot`),
- the since-molt token aggregate (`api_calls`, `input_tokens`, `cached_tokens`,
  `output_tokens`, derived `cache_rate`, `cache_miss_tokens`),
- `boundary_offset` — the `events.jsonl`/sqlite anchor at which this session
  began, retained so a later incremental refresh does not rescan.

The token aggregate is intentionally the **same shape** the injected
`_meta.tool_meta.token_usage.session` half already reports (§6), so the injected
metadata can consume the object instead of recomputing.

### 3.3 Source-of-truth ordering

`events.jsonl` is the source of truth (`src/lingtai_kernel/services/logging.py:1-6`).
The SQLite index `log.sqlite` is an **additive, rebuildable sidecar** of that
JSONL (`services/logging.py:174-180`) — it is derived and safe to delete. The
rebuild therefore prefers the sidecar for speed but must **fall back to JSONL**
and must never treat a sidecar-only fact as truth.

---

## 4. Optimized rebuild — the caveat and the requirement

> **Requirement / caveat.** The agent-session rebuild MUST NOT full-scan a large
> `events.jsonl` in the normal case. It must use an indexed/bounded path, and it
> must be measured (§ benchmark). A full scan is permitted only as an explicit,
> logged last-resort fallback.

Three tiers, in preference order:

### Tier 1 — indexed SQLite query (normal case)

The kernel already wires a **live** `log.sqlite` sidecar into the composite
logger: `SQLiteEventIndex(log_dir / "log.sqlite", ...)` inside
`CompositeLoggingService` at
`src/lingtai_kernel/base_agent/__init__.py:317-319`, so every event written to
`events.jsonl` is also indexed in real time (best-effort, fail-open). The
`events` table is indexed on `(type, ts DESC)`
(`idx_events_type_ts`, `src/lingtai_kernel/services/logging.py:328`) and on
`ts DESC` (`idx_events_ts`, `services/logging.py:329`).

The rebuild is then two indexed queries, no scan:

1. Find the boundary: the latest `psyche_molt` row for the current `molt_count`.
   `SELECT source_offset, ts, fields_json FROM events WHERE type='psyche_molt'
    ORDER BY ts DESC LIMIT 1` — served by `idx_events_type_ts`.
2. Aggregate the session: sum the `llm_response` token fields for events at/after
   that boundary (`WHERE type='llm_response' AND ts >= :boundary_ts`), served by
   `idx_events_type_ts`. `llm_response` carries `input_tokens`, `output_tokens`,
   `thinking_tokens`, `cached_tokens` (`src/lingtai_kernel/session.py:608-616`).

Both queries touch O(rows-of-that-type-since-boundary), not O(all events).
Read-only access already exists: `query_sqlite_event_index(agent_dir, sql)`
(`services/logging.py:974-983`) opens the sidecar read-only and only accepts
`SELECT`/`WITH`/`EXPLAIN` (`services/logging.py:574-577`).

### Tier 2 — reverse/tail scan of `events.jsonl` (sidecar absent/stale)

When the sidecar is missing or `disabled` (it fails open on any sqlite error,
`services/logging.py:522-526`), rebuild by scanning `events.jsonl` **backwards**
from EOF, stopping at the first `psyche_molt` with the current `molt_count`.
This reads only the tail (the current molt generation), not the whole file. The
offset iterator already exists (`_iter_jsonl_records_with_offsets`,
`services/logging.py:646-665`) and yields byte offsets, so a reverse reader can
seek rather than re-read.

### Tier 3 — full scan (explicit last resort only)

Full `events.jsonl` scan is allowed **only** when both Tier 1 and Tier 2 are
impossible (e.g. corrupt tail with no discoverable boundary), and it MUST log a
structured `agent_session_rebuild_fullscan` event with a reason so operators can
see it happened. It is never the default path.

### 4.1 Fallback beyond events: the token ledger

Note the *current* startup restore does **not** rebuild from `events.jsonl` at
all — it sums the entire `logs/token_ledger.jsonl` via
`sum_token_ledger(ledger_path)` and feeds it to `restore_token_state`
(`src/lingtai_kernel/base_agent/lifecycle.py:170-177`). That is a **full-file
scan of the ledger** and it restores *lifetime* totals, not since-molt totals.
The token ledger remains for compatibility/TUI (§5) but is **not** the
definition source for the agent session. The agent-session rebuild uses the
event path above; the ledger restore stays as-is for back-compat until a
follow-up migrates it.

---

## 5. Existing token ledger — compatibility, not definition

`logs/token_ledger.jsonl` (append-only per-call log,
`src/lingtai_kernel/token_ledger.py`) stays for compatibility and TUI surfaces.
It is a *mixed lifetime stream* (main + soul + involuntary + daemon rows) and its
`sum_token_ledger` is a lifetime aggregate (`token_ledger.py:194+`). This spec
does **not** make the ledger the definition source for either session object:

- The **runtime session** is defined by in-memory baselines (§1.1), not the
  ledger.
- The **agent session** is defined by the since-molt event aggregate (§3), not
  the ledger.

The ledger's `restore_token_state` at startup
(`lifecycle.py:170-177`) currently restores *lifetime* totals into the
`_total_*` counters, which the anatomy documents as "since last molt." That is a
known inconsistency the agent-session object is meant to eventually correct — but
correcting the startup restore is **out of scope for this branch** (it touches
the hot startup path and the #679-sensitive injected metadata). This spec names
the objects and ships the optimized rebuild primitive; the restore-path swap is a
listed follow-up (§8).

---

## 6. Consumer APIs

Consumers read the in-memory objects/accessors — never re-derive from logs on
the hot path.

### 6.1 New accessors (on `SessionManager`, surfaced via `BaseAgent`)

```python
# Runtime session — the current in-memory runtime segment. No id.
agent.runtime_session()      -> RuntimeSession        # current object
# already-existing numeric view is preserved:
agent.get_runtime_session_token_usage() -> dict       # since-refresh deltas

# Agent session — current molt generation, keyed by molt_count.
agent.agent_session()        -> AgentSession           # current object
agent.rebuild_agent_session()-> AgentSession           # (re)build for current molt_count
# already-existing numeric view is preserved:
agent.get_token_usage()      -> dict                   # since-molt cumulative totals
```

`RuntimeSession` / `AgentSession` are thin dataclasses (§7). The existing
numeric getters (`get_runtime_session_token_usage`, `get_token_usage`) are the
back-compat surface and keep their exact contracts
(`src/lingtai_kernel/session.py:649-694`, `session.py:625-647`).

### 6.2 `_meta.tool_meta.token_usage` should consume these objects

`meta_block.build_tool_meta_token_usage` builds the injected `current_call` and
`session` halves (`src/lingtai_kernel/meta_block.py:1250+`). Per the #679 fix,
the `session` half must read the **since-molt cumulative** totals
(`get_token_usage`), which survive refresh — *not* the since-refresh runtime
deltas. Once the `AgentSession` object exists, that half should read the object's
aggregate (same numbers, one owner) instead of re-reading `get_token_usage`
inline. This spec requires that consumer swap; the mechanical edit is a
follow-up so the object contract can be reviewed first (§8).

Later diagnostics (context-pressure reminders, cache-miss budget guard,
task-boundary molt heuristics that key on
`token_usage.session.api_calls > 100`) likewise read the `AgentSession`
aggregate rather than recomputing.

---

## 7. Minimal object shape (prototype in this branch)

```python
@dataclass
class RuntimeSession:
    """The current runtime lifecycle segment. No id; fresh empty each boundary."""
    started_at: str                 # process/refresh start (UTC ISO)
    # token deltas are read live from SessionManager baselines; the object
    # deliberately holds no id and grows no product state.

@dataclass
class AgentSession:
    """The agent mind segment bounded by molt_count. Rebuilt from events."""
    molt_count: int                 # identity — NOT a new id
    started_at: str | None          # ts of boundary psyche_molt, or boot
    boundary_source: str            # "agent" | "system" | "boot"
    boundary_offset: int | None     # events.jsonl byte offset of the boundary
    api_calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    @property
    def cache_miss_tokens(self) -> int: return max(self.input_tokens - self.cached_tokens, 0)
    @property
    def cache_rate(self) -> float: ...
```

The rebuild function (`rebuild_agent_session_from_events`) lives in the kernel
next to the log service and implements Tier 1 → Tier 2 → Tier 3 (§4). It takes
`agent_dir`, `molt_count`, and an optional injected sqlite query fn so it is unit
testable and benchmarkable offline.

---

## 8. Migration / back-compat

- **No schema change.** No new event type, no new sqlite column, no new manifest
  field. `molt_count` and `psyche_molt` already exist; `log.sqlite` already
  indexes them.
- **No id introduced** for either object (explicit constraint).
- **Existing accessors preserved** verbatim (`get_runtime_session_token_usage`,
  `get_token_usage`, `reset_session_token_usage`, `restore_token_state`).
- **Startup restore unchanged** in this branch (`lifecycle.py:170-177` still sums
  the ledger). Swapping it to the event-based agent-session rebuild is a listed
  follow-up, not part of this branch, because it is #679-adjacent.
- **Old agents with no `log.sqlite`** degrade to Tier 2 (tail scan) automatically;
  operators can `lingtai-agent log rebuild` (`services/logging.py:845`) to
  restore the fast path.

### Remaining implementation tasks (post-spec)

1. Land `RuntimeSession`/`AgentSession` dataclasses + accessors on
   `SessionManager`/`BaseAgent`.
2. Wire `rebuild_agent_session()` into `_start` so refresh/restart rebuilds the
   current agent session for `molt_count` (in addition to, then eventually
   instead of, the lifetime ledger restore).
3. Reset the runtime session object on `_perform_refresh` and re-anchor on molt
   (baselines already do this; the named object just needs to follow).
4. Point `meta_block.build_tool_meta_token_usage`'s `session` half at the
   `AgentSession` aggregate (§6.2), preserving the #679 "survives refresh"
   contract.
5. Migrate the startup token restore off the full ledger scan onto the
   event-based rebuild (the #679-sensitive change, done last with its own tests).

---

## Implementation status (this branch)

> Note: `docs/` is gitignored (`.gitignore:41`) but the existing reference docs
> under `docs/references/` are tracked; this spec follows that convention and was
> `git add -f`'d so it lands in the PR alongside its siblings.

- **Spec:** this document. ✅
- **Optimized rebuild primitive:** `rebuild_agent_session_from_events` +
  `RuntimeSession`/`AgentSession` dataclasses shipped as a self-contained kernel
  module (`src/lingtai_kernel/agent_session.py`). ✅ (prototype — not yet wired
  into `_start`; see follow-up #2.)
- **Benchmark:** `tools/bench_agent_session_rebuild.py` measures rebuild time and
  proves Tier 1 (indexed) vs. Tier 3 (full scan) on a synthetic events source.
  See the report for results and command. ✅
- **Wiring into lifecycle / meta_block / startup restore:** spec-only in this
  branch (follow-ups #2, #4, #5). ❌ (deliberately deferred — hot path + #679.)
