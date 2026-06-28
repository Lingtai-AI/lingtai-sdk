---
name: imap-mcp-manual
description: |
  Progressive-disclosure usage manual for the IMAP/SMTP email MCP tool. Read this
  when you need detail beyond the one-line action descriptions: send vs reply,
  check/read/search over folders, the compound email_id (account:folder:uid),
  attachments, move/flag/delete/folders, contacts/accounts basics, and the
  important external-email side-effect caveats (real outbound mail — confirm
  before sending). Pulled on demand via action='manual'; you do not need to call
  it before every send.
version: 1.0.0
---

# IMAP/SMTP email MCP — usage manual (progressive disclosure)

This manual is pulled on demand via `action='manual'` so the per-action tool
schema can stay concise. Read it when you need detail beyond the one-line action
descriptions; you do not need to call it before every send.

## EMAIL IDS

- `email_id` is a compound key: `account:folder:uid` (e.g.
  `me@example.com:INBOX:1234`). `read`, `reply`, `delete`, `move`, and `flag`
  take one id or a list of ids. Use the ids returned by `check`/`search`; do not
  construct them by hand.

## READING: check / read / search

- `check`: list recent envelopes from a folder (optional `folder`, `n`). An
  empty or whitespace-only `folder` is treated as omitted and defaults to
  `INBOX`.
- `read`: fetch full email(s) by `email_id` (a single id or a list). You are
  encouraged to read multiple relevant — or even all unread — emails and think
  before acting.
- `search`: server-side IMAP search (`query`; optional `folder`, empty/whitespace-only defaults to `INBOX`). Queries use a server-side search DSL, e.g.
  `from:addr subject:text unseen since:YYYY-MM-DD`; supported fields depend on
  the IMAP addon, so prefer examples returned by this tool over raw RFC IMAP
  search syntax.
- `folders`: list available IMAP folders.

## SEND vs REPLY

- `send`: compose a new email (`address`, `message`; optional `subject`, `cc`,
  `bcc`, `attachments`). `address`/`cc`/`bcc` accept a single string or a list.
- `reply`: reply to an existing email (`email_id`, `message`; optional `cc`,
  `attachments`). Reply preserves threading/subject from the original.

## ATTACHMENTS

- `attachments` is a list of file paths (absolute or relative to the working
  dir) for `send`/`reply`. Attach generated artifacts (charts, reports, CSVs,
  PDFs) as files rather than pasting a path into the body.

## ORGANIZING: move / flag / delete

- `move`: move email(s) to another folder (`email_id`, `folder`=destination).
  The destination `folder` is required and must be non-empty — unlike
  check/search it is never defaulted to `INBOX`.
- `flag`: set/clear flags (`email_id`, `flags={"seen": true, "flagged": false}`).
  `flags` is required; e.g. `flags={"seen": true}` marks read. A missing or
  empty `flags` returns an error rather than silently doing nothing.
- `delete`: delete email(s) by `email_id`.

## CONTACTS / ACCOUNTS

- `contacts`: list all contacts.
- `add_contact`: add/update a contact (`address`, `name`; optional `note`).
- `edit_contact`: update contact fields (`address`; optional `name`, `note`).
- `remove_contact`: remove a contact (`address`).
- `accounts`: list configured IMAP accounts and connection status. Most actions
  accept an optional `account` (email address); it defaults to the primary
  account. An empty or whitespace-only `account` is treated as omitted and
  uses the default/sole account rather than failing with `Unknown account`.

## SIDE EFFECTS & SAFETY

- `send` and `reply` deliver real email to real recipients over SMTP — this is an
  external, hard-to-undo side effect. Confirm the recipient list (including
  `cc`/`bcc`) and the body before sending unsolicited mail.
- When replying to external addresses, follow the caller's standing reply
  policy. Unknown external senders require explicit guidance, or confirmation
  that the sender is the same human who contacted you through an internal
  channel, before sending a real reply.
- `delete` and `move` change server-side mailbox state; double-check the
  `email_id`/`folder` before running them.
- Actions return a result dict on success or one carrying an `'error'` key on
  failure (e.g. unknown account, bad `email_id`, unreadable attachment). Check
  for the error and surface or act on it rather than assuming delivery.
