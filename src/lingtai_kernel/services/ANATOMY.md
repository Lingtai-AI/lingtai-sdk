# services

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues (mail or `discussions/<name>-patch.md`); do not silently fix.

Kernel-side service ABCs and implementations. Services back cross-cutting kernel concerns without making intrinsics depend directly on one transport: filesystem mail for peer messages and JSONL logging for structured events.

## Components

- `services/mail.py` — message transport.
  - `MailService` is the ABC for `send()`, `listen()`, `stop()`, and `address` (`services/mail.py:29`).
  - `FilesystemMailService` implements directory-based delivery (`services/mail.py:81`); its constructor takes `working_dir`, `mailbox_rel`, and optional pseudo-agent subscriptions (`services/mail.py:94`).
  - `send()` resolves peer/absolute addresses, checks `is_agent`/`is_alive`, generates ids through `intrinsics.email._new_mailbox_id`, copies attachments, and writes `message.json` atomically (`services/mail.py:131`, `services/mail.py:165`, `services/mail.py:199-206`).
  - `listen()` starts a daemon polling thread (`services/mail.py:216`), snapshots existing inbox ids into `_seen` (`services/mail.py:224-227`), scans every 0.5s (`services/mail.py:256`), and also claims subscribed pseudo-agent outbox messages (`services/mail.py:250-253`, `services/mail.py:261-368`).
  - `stop()` sets the poll stop event and joins the thread (`services/mail.py:370-374`).
- `services/logging.py` — structured event log.
  - `LoggingService` is the ABC for `log(event)` and `close()` (`services/logging.py:18`).
  - `JSONLLoggingService` appends JSON lines with a lock and flush per write (`services/logging.py:33`, `services/logging.py:39`, `services/logging.py:47-52`).
  - `get_events()` re-reads the whole file for inspection (`services/logging.py:55-69`); `close()` closes the handle (`services/logging.py:72-74`).
- `services/__init__.py` is an empty package marker; callers import concrete modules directly.

## Connections

- `BaseAgent.__init__` always creates `JSONLLoggingService` and stores it as `_log_service` (`base_agent/__init__.py:214-221`).
- `BaseAgent` receives a `MailService | None` constructor argument (`base_agent/__init__.py:230`); missing mail service disables the email intrinsic (`base_agent/__init__.py:158`).
- Email boot wires `FilesystemMailService.listen(on_message=agent._on_mail)` through the email intrinsic (`base_agent/__init__.py:441-442`).
- `services/mail.py` imports `handshake.{is_agent,is_alive,resolve_address}` for routing/liveness (`services/mail.py:24`) and late-imports `_new_mailbox_id` from `intrinsics.email` inside `send()` to avoid a module-level cycle (`services/mail.py:165`).

## Composition

- **Parent:** `src/lingtai_kernel/` (see `ANATOMY.md`).
- **Subfolders:** none.
- **Sibling consumers:** `intrinsics/` owns mailbox tool behavior; `base_agent.py` owns logging lifecycle.

## State

- **Persistent mail:** `<workdir>/mailbox/{inbox,outbox,sent}/<uuid>/message.json`; optional `attachments/` subdir. `FilesystemMailService.send()` writes recipient inbox payloads atomically (`services/mail.py:199-206`).
- **Persistent log:** `<workdir>/logs/events.jsonl`; one JSON object per line, appended by `JSONLLoggingService.log()` (`services/logging.py:47-52`).
- **Ephemeral mail:** `_seen` is an in-memory set of delivered UUIDs rebuilt at listen start (`services/mail.py:224-227`); `_poll_thread` is a daemon thread joined by `stop()` (`services/mail.py:370-374`).
- **Ephemeral log:** `JSONLLoggingService` holds an open file handle and a thread lock (`services/logging.py:39-47`).

## Notes

- Pseudo-agent outbox claiming is optimistic concurrency: pollers copy to inbox, race on outbox→sent rename, and losers delete their speculative copy and clear `_seen` (`services/mail.py:326-354`).
- The services package has two ABCs but one implementation each today; the boundary exists so future transports can replace filesystem mail or JSONL logging without changing intrinsic call sites.
- `get_events()` favors simplicity over hot-path performance: it re-opens and parses the whole JSONL file each call (`services/logging.py:55-69`).
