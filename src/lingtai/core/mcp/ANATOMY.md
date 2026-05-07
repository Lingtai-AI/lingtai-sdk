# core/mcp

MCP capability — per-agent registry of MCP (Model Context Protocol) servers.
Pure presentation: reads the registry from disk, validates records, and renders
it as XML into the system prompt. No tool writes; all registry mutations happen
via file operations from the agent (`write`, `edit`).

Also includes the **LICC v1 (LingTai Inbox Callback Contract)** — a
filesystem-based inbox that lets out-of-process MCP servers push events into
the agent's inbox.

## Components

- `mcp/__init__.py` — MCP registry management and tool surface. `get_description` (`mcp/__init__.py:374-375`), `get_schema` (`mcp/__init__.py:378-379`), `setup` (`mcp/__init__.py:382-406`). Key functions: `validate_record` (`mcp/__init__.py:82-125`), `validate_registry_line` (`mcp/__init__.py:128-138`), `read_registry` (`mcp/__init__.py:149-186`), `decompress_addons` (`mcp/__init__.py:201-257`), `_build_registry_xml` (`mcp/__init__.py:274-299`), `_reconcile` (`mcp/__init__.py:306-342`).
- `mcp/inbox.py` — LICC v1 filesystem inbox poller. `validate_event` validates required `from`/`subject`/`body` fields; `_format_notification_summary` is **deprecated** legacy helper (retained for backward compat); `_consume_event` returns `(wake, preview)` where `preview = {"from": sender, "subject": subject, "preview": body[:_PREVIEW_FIELD_CAP]}` — only the body snippet gets capped (sender/subject are bounded by upstream construction); `_dispatch_summary` publishes to `.notification/mcp.<mcp_name>.json` via `notifications.submit`, embedding `data.previews` and rendering them inline in `instructions`; `_scan_once` coalesces per MCP, threading the preview list through; `MCPInboxPoller` class drives the poll loop. Body snippet cap is `_PREVIEW_FIELD_CAP = 200`.
- `mcp/manual/` — skill documentation (`SKILL.md`) plus reference docs (`curated-addons.md`, `third-party-and-legacy.md`, `troubleshooting.md`) and scripts (`find_readme.py`).

## Public API

The `mcp` tool exposes one action:

| Action | Description |
|--------|-------------|
| `show` | Return the mcp-manual skill body plus a runtime health snapshot (registry contents, problems, registry path) |

### LICC v1 Inbox Protocol

MCP servers push events via filesystem writes:
```
<agent_working_dir>/.mcp_inbox/<mcp_name>/<event_id>.json
```

Schema (v1):
```json
{
  "licc_version": 1,
  "from": "human-readable sender (required)",
  "subject": "one-line summary (required, max 200 chars)",
  "body": "full message body (required)",
  "metadata": {},
  "wake": true,
  "received_at": "ISO 8601"
}
```

Atomic write: write to `<event_id>.json.tmp`, fsync, then rename.

## Internal Module Layout

```
mcp/__init__.py
  ├── Catalog
  │   ├── _load_catalog()           — reads kernel-shipped mcp_catalog.json, cached
  │   └── decompress_addons()       — boot-time: append catalog entries for addons not in registry
  │
  ├── Validation
  │   ├── validate_record()         — validates a single MCP registry record
  │   └── validate_registry_line()  — validates a single JSONL line
  │
  ├── Registry I/O
  │   ├── read_registry()           — reads mcp_registry.jsonl, returns (valid, problems)
  │   └── _append_record()          — appends a validated record as a JSONL line
  │
  ├── XML builder
  │   ├── _escape_xml()             — XML entity escaping
  │   └── _build_registry_xml()     — renders registry records as <registered_mcp> XML
  │
  ├── Reconciliation
  │   └── _reconcile()              — reads registry, renders into prompt, returns health snapshot
  │
  └── Tool surface
      ├── get_description/schema()  — module-level
      └── setup()                   — registers mcp tool, runs initial _reconcile

mcp/inbox.py
  ├── Validation
  │   └── validate_event()              — validates a parsed LICC event
  │
  ├── Dispatch (signal-only since issue #37, .notification/ since this fix)
  │   ├── _format_notification_summary()— DEPRECATED; retained for backward compat
  │   ├── _consume_event()              — per-event log + wake intent collector
  │   └── _dispatch_summary()           — publishes to .notification/mcp.<name>.json
  │
  ├── Dead-letter
  │   └── _dead_letter()                — moves invalid file to .dead/ with .error.json sidecar
  │
  ├── Scanner
  │   └── _scan_once()                  — sweep .mcp_inbox/<mcp_name>/*.json,
  │                                       consume each event, post one summary per MCP
  │
  └── Poller
      └── MCPInboxPoller                — daemon thread that polls at POLL_INTERVAL (0.5s)
          ├── start()                   — creates root dir, starts poll thread
          └── stop()                    — signals stop, joins thread
```

## Key Invariants

- **Registry is append-only JSONL:** One record per line. Duplicates by name are flagged as problems during read. Mutations (register, deregister, update) happen via agent-side file operations.
- **Name convention:** Lowercase, dash-separated, bounded length (`^[a-z][a-z0-9_-]{0,30}$`).
- **Transport validation:** `stdio` requires `command` + `args`; `http` requires `url`.
- **Addons decompression is idempotent:** Running `decompress_addons()` multiple times produces the same registry. Existing records are never modified.
- **`{python}` substitution:** Catalog entries support `{python}` placeholder in command args, resolved to `sys.executable` at decompression time.
- **LICC atomicity:** MCP servers must write `.json.tmp` then rename to `.json`. Half-written `.tmp` files are ignored by the scanner.
- **LICC dead-letter:** Invalid events (parse errors, missing fields, unknown version, dispatch failures) are moved to `.dead/` with a `.error.json` sidecar. Dead-letters are never auto-deleted.
- **LICC bounded work:** `MAX_EVENTS_PER_CYCLE = 100` per MCP per sweep prevents pathological backlog from blocking the poller.
- **LICC notification shape (post-#37 + previews):** The coalesced notification carries the MCP name, event count, plus a `previews` list — one entry per consumed event with `{"from": <sender>, "subject": <subject>, "preview": <body[:200]>}`. The body snippet is hard-truncated at `_PREVIEW_FIELD_CAP` (200 chars); `from` and `subject` pass through uncapped (sender bounded by upstream construction; subject already validated `<= 200` chars by `validate_event`). Full message **bodies** are still NOT inlined — those stay behind the `<mcp>(action="check"/"read")` tool result. The original issue #37 invariant (no body duplication → no agent re-processing loop) is preserved; previews exist purely to let the agent triage which MCPs/messages deserve a read call. Multiple events from the same MCP in one sweep are coalesced into a single summary; `wake` is the OR of all per-event `wake` flags. Preview list length is naturally bounded by `MAX_EVENTS_PER_CYCLE` (100).
- **LICC uses `.notification/` filesystem-as-protocol:** `_dispatch_summary` publishes via `notifications.submit` to `.notification/mcp.<mcp_name>.json` instead of posting to the legacy inbox queue. This unifies MCP events with all other notification producers (email, soul, system events) in the kernel's `_sync_notifications` wire injection path.
- **Pure presentation:** The capability never writes to the registry file. It only reads and renders.

## Dependencies

- `yaml` (PyYAML) — used by the library capability's frontmatter parser (imported transitively; not directly used here)
- `lingtai.i18n` — `t()` for localized strings (imported but the description is hardcoded English)
- `lingtai_kernel.notifications` — `submit` (as `publish_notification`) for `.notification/` dispatch (in `inbox.py`)
- `lingtai_kernel.base_agent.BaseAgent` — agent type (TYPE_CHECKING only)
- `lingtai.mcp_catalog.json` — kernel-shipped MCP catalog file (read at runtime)

## Composition

- **Parent:** `src/lingtai/core/` (capability package).
- **Siblings:** `daemon/`, `avatar/`, `library/`, `codex/`, `bash/`.
- **Manual:** `mcp/manual/SKILL.md` — registration contract and usage guide.
- **Kernel hooks:** `setup()` is called during capability initialization; `decompress_addons()` is called by the Agent initializer before `setup`. `MCPInboxPoller.start()/stop()` are called by the agent lifecycle.
