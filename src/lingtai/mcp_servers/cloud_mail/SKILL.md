---
name: cloud-mail-mcp-manual
description: |
  Progressive-disclosure usage manual for the Cloud Mail REST email MCP tool.
  Read this when you need detail beyond the one-line action descriptions:
  check/search filters, the compound id (account:emailId) for read, send (needs
  user credentials), plain vs HTML bodies, accounts/add_user basics, and the
  external-email side-effect caveats. Pulled on demand via action='manual'; you
  do not need to call it before every send.
version: 1.0.0
last_changed_at: "2026-06-26T14:33:19-07:00"
---

# Cloud Mail MCP — usage manual (progressive disclosure)

This manual is pulled on demand via `action='manual'` so the per-action tool
schema can stay concise. Read it when you need detail beyond the one-line action
descriptions; you do not need to call it before every send.

Cloud Mail is a REST email client for a self-hosted Cloud Mail deployment
(Cloudflare Workers). Inbound mail also arrives automatically in your inbox via
per-account polling, so you don't have to poll `check` yourself.

## EMAIL IDS

- `read` takes a compound id `id='<account>:<emailId>'`, or `account` plus a
  numeric `email_id`. Use the ids returned by `check`/`search`; do not construct
  them by hand.

## READING: check / search / read

- `check`: list recent inbound emails (optional `limit`/`n`, plus the same
  filters as search).
- `search`: filter the public email list by `to_email`, `send_email`,
  `send_name`, `subject`, `content`, `time_sort` (`asc`/`desc`), and paginate
  with `num`/`size`. Filters are LIKE matches.
- `read`: fetch the full content of one email by compound `id`.

## SEND

- `send` requires user credentials in config (it logs in, then posts to
  `/email/send`). Provide `address` (recipient or list), and a body via
  `message`/`text` (plain) and/or `html`/`content_html` (HTML). Optional
  `subject`, `name` (sender display name), `send_account_id` (override sender).
- Attachments are NOT supported in this first pass.

## ACCOUNTS / ADD_USER

- `accounts`: redacted per-account status (no tokens/passwords).
- `add_user`: create a Cloud Mail user (`email`, `password`; optional
  `role_name`). Admin operation — use deliberately.

## SIDE EFFECTS & SAFETY

- `send` delivers real email to real recipients — an external, hard-to-undo side
  effect. Confirm the recipient(s) and body before sending unsolicited mail.
- `add_user` mutates the Cloud Mail deployment's user set; double-check before
  running it.
- Actions return `{'status': 'ok', ...}` on success or `{'status': 'error',
  'error': <message>}` on failure. Check the status and surface or act on errors
  rather than assuming delivery.
