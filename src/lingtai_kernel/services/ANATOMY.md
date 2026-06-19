# services

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

Kernel-side service ABCs and implementations. Services back cross-cutting kernel concerns without making intrinsics depend directly on one transport: filesystem mail for peer messages and structured event logging with JSONL as source-of-truth plus a rebuildable SQLite query index.

## Components

- `services/mail.py` — message transport.
  - `MailService` is the ABC for `send()`, `listen()`, `stop()`, and `address` (`services/mail.py:29`).
  - `FilesystemMailService` implements directory-based delivery (`services/mail.py:81`); its constructor takes `working_dir`, `mailbox_rel`, and optional pseudo-agent subscriptions (`services/mail.py:94`).
  - `send()` resolves peer/absolute addresses, checks `is_agent`/`is_alive`, generates ids through `intrinsics.email._new_mailbox_id`, copies attachments, and writes `message.json` atomically (`services/mail.py:131`, `services/mail.py:165`, `services/mail.py:199-206`).
  - `listen()` starts a daemon polling thread (`services/mail.py:216`), snapshots existing inbox ids into `_seen` (`services/mail.py:224-227`), scans every 0.5s (`services/mail.py:256`), and also claims subscribed pseudo-agent outbox messages (`services/mail.py:250-253`, `services/mail.py:261-368`).
  - `stop()` sets the poll stop event and joins the thread (`services/mail.py:370-374`).
- `services/logging.py` — structured event log and additive SQLite trace query index.
  - `LoggingService` is the ABC for `log(event)` and `close()`; `log()` may return optional storage metadata such as JSONL offsets (`services/logging.py:76`, `services/logging.py:83-89`).
  - `JSONLLoggingService` appends UTF-8 JSON lines with a lock and flush per write, returning `(source_file, source_offset)` metadata (`services/logging.py:95`, `services/logging.py:104-111`, `services/logging.py:119-130`).
  - `SQLiteEventIndex` owns the derived `logs/log.sqlite` schema, fail-open runtime event writes, v1→v2 migration, and read-only inspection opens (`services/logging.py:143`, `services/logging.py:213-300`, `services/logging.py:383-456`).
  - `CompositeLoggingService` redacts the event via `trace_redaction.redact_for_trajectory()` before any durable write, writes the top-level `logs/events.jsonl` primary first, then best-effort inserts that same redacted event into SQLite with the JSONL source offset (`services/logging.py:489`, `services/logging.py:496-506`).
  - `rebuild_sqlite_event_index()`, `doctor_sqlite_event_index()`, and `query_sqlite_event_index()` back the CLI rebuild/doctor/query commands; rebuild scans agent events, chat history/archive, and daemon event/chat JSONL sources into one sidecar (`services/logging.py:629`, `services/logging.py:735`, `services/logging.py:747`).
- `services/__init__.py` is an empty package marker; callers import concrete modules directly.

## Connections

- `BaseAgent.__init__` creates a `CompositeLoggingService` over `logs/events.jsonl` plus additive `logs/log.sqlite` (`base_agent/__init__.py:273-285`).
- `BaseAgent` receives a `MailService | None` constructor argument (`base_agent/__init__.py:230`); missing mail service disables the email intrinsic (`base_agent/__init__.py:158`).
- Email boot wires `FilesystemMailService.listen(on_message=agent._on_mail)` through the email intrinsic (`base_agent/__init__.py:441-442`).
- `services/mail.py` imports `handshake.{is_agent,is_alive,resolve_address}` for routing/liveness (`services/mail.py:24`) and late-imports `_new_mailbox_id` from `intrinsics.email` inside `send()` to avoid a module-level cycle (`services/mail.py:165`).

## Composition

- **Parent:** `src/lingtai_kernel/` (see `ANATOMY.md`).
- **Subfolders:** none.
- **Sibling consumers:** `intrinsics/` owns mailbox tool behavior; `base_agent/` owns logging lifecycle; `src/lingtai/cli.py` exposes `lingtai-agent log {rebuild,doctor,query}` (`../lingtai/cli.py:294-305`).

## State

- **Persistent mail:** `<workdir>/mailbox/{inbox,outbox,sent}/<uuid>/message.json`; optional `attachments/` subdir. `FilesystemMailService.send()` writes recipient inbox payloads atomically (`services/mail.py:199-206`).
- **Persistent log source-of-truth:** `<workdir>/logs/events.jsonl`; one JSON object per line, appended by `JSONLLoggingService.log()` after the composite service has redacted high-confidence secrets (`services/logging.py:119-138`, `services/logging.py:496-506`). Chat-history and daemon traces remain authoritative in their own JSONL files (`history/chat_history*.jsonl`, `daemons/*/{logs/events.jsonl,history/chat_history.jsonl}`); live chat history is redacted by `BaseAgent._save_chat_history()` before `history/chat_history.jsonl` is written.
- **Persistent log sidecar:** `<workdir>/logs/log.sqlite`; rebuildable/deletable SQLite trace index with `schema_migrations`, `import_cursors`, `events`, and `chat_entries` tables. `events` and `chat_entries` keep `source_file/source_offset/source_line` provenance so JSONL replays are idempotent and traceable (`services/logging.py:213-300`).
- **Ephemeral mail:** `_seen` is an in-memory set of delivered UUIDs rebuilt at listen start (`services/mail.py:224-227`); `_poll_thread` is a daemon thread joined by `stop()` (`services/mail.py:370-374`).
- **Ephemeral log:** `JSONLLoggingService` holds an open file handle and a thread lock; `SQLiteEventIndex` holds an optional sqlite connection and disables itself after sqlite errors so agent turns fail open (`services/logging.py:99-104`, `services/logging.py:154-174`).

## Notes

- Pseudo-agent outbox claiming is optimistic concurrency: pollers copy to inbox, race on outbox→sent rename, and losers delete their speculative copy and clear `_seen` (`services/mail.py:326-354`).
- The services package has two ABCs but mail and logging now differ in shape: mail still has one filesystem implementation, while logging composes the JSONL primary with optional derived indexes.
- `get_events()` favors simplicity over hot-path performance: it re-opens and parses the whole JSONL file each call (`services/logging.py:133-148`).
- SQLite is intentionally additive: JSONL remains the durable source of truth; rebuild requires the agent working-directory lock (offline/stopped agent), uses a temporary database and atomic replace, and checkpoints WAL before replacing so the final artifact is self-contained (`services/logging.py:629-732`).
- Runtime writes only index the top-level `logs/events.jsonl` stream. Chat history, archive, and daemon JSONL are indexed during explicit rebuild, avoiding extra work on every chat-history rewrite or daemon append (`services/logging.py:472-482`, `services/logging.py:542-628`).
- Query helpers accept read-only `SELECT`/CTE/`EXPLAIN` statements, use `PRAGMA query_only=ON`, and choose immutable offline reads or WAL-aware read-only live reads to keep CLI inspection non-mutating while seeing WAL rows (`services/logging.py:181-201`, `services/logging.py:425-440`).
