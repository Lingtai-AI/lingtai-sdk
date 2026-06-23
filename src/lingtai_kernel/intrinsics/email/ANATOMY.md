# intrinsics/email

Filesystem-based email system — mailbox I/O, composition, search, contacts, recurring schedules, and delivery. The agent's primary inter-process communication channel.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — Package surface. Re-exports the full public API of the former monolithic `email.py` for backward compatibility: all primitives, schema functions, and `EmailManager`. Registers the `email` generic-dismiss guard at import (`__init__.py:27-32`) because `.notification/email.json` mirrors durable unread state. Contains the module-level `handle()` dispatcher (`__init__.py:80-93`) and idempotent `boot()` hook (`__init__.py:98-113`); `boot()` stops any prior manager's scheduler before wiring the fresh manager. External callers import `handle`, `boot`, `get_schema`, `get_description`, `EmailManager`, `_new_mailbox_id`, `mode_field` from this package.

- `primitives.py` — Mailbox I/O and display helpers. Module-level functions operating on the agent's `mailbox/` directory tree.
  - ID and path helpers: `_new_mailbox_id` (`primitives.py:22-26`), `mode_field` (`primitives.py:29-34`), `_mailbox_dir` / `_inbox_dir` / `_outbox_dir` / `_sent_dir` (`primitives.py:37-50`).
  - Inbox I/O: `_load_message` (`primitives.py:56-61`), `_list_inbox` (`primitives.py:64-82`).
  - Read tracking: `_read_ids` (`primitives.py:89-98`), `_save_read_ids` (`primitives.py:101-106`), `_mark_read` (`primitives.py:109-113`).
  - Display: `_summary_to_list` (`primitives.py:118-123`), `_message_summary` (`primitives.py:126-145`).
  - Delivery: `_is_self_send` (`primitives.py:150-159`), `_persist_to_inbox` (`primitives.py:162-173`), `_persist_to_outbox` (`primitives.py:176-188`), `_move_to_sent` (`primitives.py:191-207`), `_mailman` (`primitives.py:210-267`) — daemon thread that waits, dispatches, and archives.
  - Filtering helpers: `_coerce_address_list` (`primitives.py:274-286`), `_preview` (`primitives.py:289-293`), `_email_time` (`primitives.py:296-298`).

- `schema.py` — Tool registration. `get_description` (`schema.py:10-11`) and `get_schema` (`schema.py:14-132`) build the JSON Schema for the email tool. Imports `mode_field` from `primitives`.

- `manager.py` — `EmailManager` class (`manager.py:46-876`). The core filesystem-based email manager. Key sections:
  - Lifecycle: `__init__` (`manager.py:48-55`), `start_scheduler` (`manager.py:57-66`), `stop_scheduler` (`manager.py:68-72`).
  - Filesystem helpers: `_load_email` (`manager.py:81-107`), `_list_emails` (`manager.py:109-132`), `_email_summary` (`manager.py:134-156`), `_inject_identity` (`manager.py:158-175`).
  - Action dispatch: `handle` (`manager.py:180-209`).
  - Schedules: `_handle_schedule` (`manager.py:214-224`), `_schedule_create` / `_cancel` / `_reactivate` / `_list` (`manager.py:226-363`), schedule helpers (`manager.py:368-442`), `_scheduler_loop` / `_scheduler_tick` (`manager.py:444-543`).
  - Send: `_send` (`manager.py:548-650`). Dispatches via `_mailman` daemon threads.
  - CRUD: `_check` (`manager.py:657-699`), `_read`, `_dismiss`, `_reply`, `_reply_all`, `_search`, `_archive`, `_delete`. `_dismiss` is the lightweight cousin of `_read` — same effect on read state and notification but returns no email bodies; intended for the "I already saw it in the digest" path. All four read-state mutators (`_read`, `_dismiss`, `_archive`, `_delete`) call `EmailManager._rerender_unread_digest()` after the mutation so `.notification/email.json` mirrors the new state.
  - Reply routing: `_resolve_reply_target` picks ``(address, mode)`` for `_reply` / `_reply_all`. Preference order is (1) inbound `_return_route` (embedded by abs sends), (2) absolute-path `from`, (3) bare `from` in peer mode. An ambiguity guard refuses to send when a peer-mode bare `from` would resolve to the responder's own workdir while the original message's `identity.agent_id` differs from the responder's own agent id — the live failure mode from issue #145 where two `.lingtai/` networks both host an agent with the same short name (e.g. both have "mimo-1").
  - Notification refresh: `_rerender_unread_digest` (method on `EmailManager`) — lazy-imports the kernel-side helper from `base_agent/messaging.py` and runs it. Centralised here so all read-state mutators share one call site.
  - Contacts: `_contacts_path` / `_load_contacts` / `_save_contacts` / `_contacts` / `_add_contact` / `_remove_contact` / `_edit_contact` (`manager.py:801-876`).

## Connections

- **Inbound:** `handle()` is called by the tool dispatcher (via `base_agent._dispatch_tool`). `boot()` is called during agent construction in `base_agent/__init__.py`.
- **Inbound (cross-module):** `_new_mailbox_id` is imported by `base_agent/messaging.py:28` and `services/mail.py:165` for ID generation.
- **Inbound (cross-module):** `EmailManager` is imported by `src/lingtai/__init__.py:19` for the wrapper re-export.
- **Outbound:** Depends on `..i18n` (translations), `..message` (message construction), `..time_veil` (timestamp scrubbing), `..token_counter` (budget checks in `_check`).
- **Outbound (unread-digest producer):** Mail arrival writes `.notification/email.json` via `publish_notification` (or deletes it via `clear_notification` when count hits 0). `base_agent/messaging.py:_on_normal_mail` calls `_rerender_unread_digest(agent)` (`base_agent/messaging.py:52`) which uses `primitives.py:_render_unread_digest` to build the digest prose, then `system.publish_notification(workdir, "email", header=…, icon="📧", data={count, newest_received_at, digest})`. The kernel's `_sync_notifications` poll picks up the fingerprint change on the next heartbeat tick and updates the wire's `notification(action="check")` block. See root `ANATOMY.md` "Notifications" for the full architecture.
- **Outbound (bounce notification):** `primitives.py:_mailman` calls `agent._enqueue_system_notification(source="email.bounce", ref_id=msg_id, body=...)` (`primitives.py:280`). The system events producer in `base_agent/messaging.py` merges the bounce into the events list inside `.notification/system.json` (capped at 20 newest) under a per-agent `threading.Lock`. Bounces share `system.json` with daemon notices, MCP-bridged events, and any future kernel events — they are NOT aggregated into the unread digest at `email.json`.
- **Data flow:** All state lives in the filesystem under `mailbox/` and `.notification/`. The `EmailManager` is stateless except for `_last_sent` (duplicate-send guard) and `_scheduler_thread` (background timer).

## Key invariants

- `_send(mode="abs")` embeds an explicit `_return_route` dict (`{"mode": "abs", "address": <sender abs workdir>, "sender_agent_id": <sender id>}`) into every dispatched payload AND the local `sent/{id}/message.json` record. This is the only safe return route across `.lingtai/` networks where short addresses can collide (issue #145). Recipients without the field — older messages — keep working through the existing absolute-`from` fallback in `_resolve_reply_target`.
- `_mailman` runs as a daemon thread per recipient. It waits until `deliver_at`, then dispatches. The outbox entry is written synchronously before the thread starts.
- `_mailman` with `skip_sent=True` (used by `_send`) deletes the outbox entry instead of moving it to `sent/`, because `_send` writes the `sent/` entry itself.
- Schedule status lifecycle: `active` → `inactive` (cancel) or `completed` (all sent). On startup, `_reconcile_schedules_on_startup` flips `active` → `inactive` so schedules don't fire until explicitly reactivated.
- `.notification/email.json` is a **live mirror** of the current unread set. Any action that mutates the read state — `_read`, `_dismiss`, `_archive`, `_delete` — calls `_rerender_unread_digest(agent)` (lazy import from `base_agent/messaging.py`) so the wire's notification updates on the next heartbeat sync. The earlier "snapshot at last arrival" semantics led to the unread digest carrying mails the agent had already replied to indefinitely.
- `_dismiss` is the lightweight "mark read without returning content" path — used when the agent already saw the body in the digest preview (≤200 chars renders inline) and just wants to clear the notification entry. Same effect on `read.json` and `.notification/email.json` as `_read`, but no email bodies in the response. Accepts a list (`email_id=[id1, id2, ...]`).
- The unread-mail notification envelope carries an ``instructions`` field (set by `_rerender_unread_digest`) telling the agent to call `email(action="read", ...)` or `email(action="dismiss", ...)` after handling a mail; until the agent does, the notification keeps reminding them. This is the producer-side directive — generic frontend code does not have to know about email's dismissal contract.
- Each digest entry exposes the mailbox ID directly (under "ID:" in en, "ID：" in zh, "编号：" in wen). The agent passes that ID verbatim to `email_id` when calling `read` or `dismiss`. Without this, the agent has to call `email(action="check")` first just to discover the IDs, defeating the point of the inline notification.
- Contact writes use atomic temp-file + `os.replace` to prevent corruption on crash.

## Notification format

When mail arrives, `base_agent/messaging.py:_on_normal_mail` calls
`_rerender_unread_digest(agent)` which builds a digest of all currently
unread mail using `primitives.py:_render_unread_digest`, then submits
the result via `system.publish_notification` to `.notification/email.json`.
The same path runs after `_read` / `_dismiss` / `_archive` / `_delete`
mutate the read state (each of those calls `EmailManager._rerender_unread_digest()`):

```json
{
  "header":       "3 unread emails",
  "icon":         "📧",
  "priority":     "normal",
  "published_at": "2026-05-05T03:42:11Z",
  "instructions": "Each entry above shows the sender, subject, and a preview of up to 200 characters. ... call email(action=\"read\", ...) or email(action=\"dismiss\", ...) ...",
  "data": {
    "count":               3,
    "newest_received_at":  "2026-05-05T03:42:09Z",
    "digest":              "[email] 3 unread message(s) — most recent ..."
  }
}
```

The ``instructions`` field is the producer-side directive that
replaces the static-prompt approach: it travels with the payload, so
each producer owns its own dismissal contract without the kernel
having to know about it.

The agent reads this through the kernel-injected `notification(action="check")`
wire pair (see root `ANATOMY.md` "Notifications"); the JSON dict appears
under the `email` key in the `notifications` map. There is no per-mail
"notification pair" anymore — the file IS the notification.

Digest prose format (en) is what lands in `data.digest`:
```
[email] {count} unread message(s) — most recent {recency}.

  1. From {name} ({address}) — {subject}
     Sent at: {sent_at}
     {preview}

  2. ...
(showing first {N_shown} of {N_total})    ← only if N_total > N_shown
```

- **Cap:** max 10 entries (newest-first), 200 chars preview each.
- **`recency`:** veiled timestamp of newest unread (uses `time_veil.veil()`).
- **Lifecycle:** `.notification/email.json` is rewritten on every arrival; deleted via `clear_notification` when count hits 0 (the kernel sync then strips the wire's notification block on the next tick). Reads/dismisses/archives/deletes also trigger rerender through `EmailManager._rerender_unread_digest()`, so the digest mirrors current unread state.
