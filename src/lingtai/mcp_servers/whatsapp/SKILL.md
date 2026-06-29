---
name: whatsapp-mcp-manual
description: |
  Progressive-disclosure usage manual for the WhatsApp Cloud API MCP tool. Read
  this when you need detail beyond the one-line action descriptions: the 24-hour
  customer-service window and approved templates, send vs reply vs react,
  check/read/search, media attachments, contacts/accounts/status basics, and
  external-delivery side-effect caveats. Pulled on demand via action='manual'; you
  do not need to call it before every send.
version: 1.0.0
last_changed_at: "2026-06-26T14:33:19-07:00"
---

# WhatsApp MCP — usage manual (progressive disclosure)

This manual is pulled on demand via `action='manual'` so the per-action tool
schema can stay concise. Read it when you need detail beyond the one-line action
descriptions; you do not need to call it before every send.

This client uses the official Meta WhatsApp Cloud API only (no WhatsApp Web
bridge).

## 24-HOUR WINDOW / TEMPLATES

- WhatsApp Cloud API allows free-form business replies only inside the 24-hour
  customer-service window (24h since the user's last message). Outside that
  window you must send an approved message `template`, not free text.
- `templates`: list approved message templates. Use a template's `name` +
  `language.code` to send outside the window.

## RECIPIENTS

- Messages target a recipient by `to` (or `wa_id`) — the WhatsApp `wa_id`. Use
  ids returned by `check`/`read`/`contacts`.

## SEND / REPLY / REACT

- `send` (`to`/`wa_id`, plus `text`, `media`, or `template`) starts a message.
  `media` is an object with `type` (image/document/audio/video) and the media
  fields; `template` is an object requiring `name` and `language.code`.
- `reply` threads to a specific message (`message_id`, then `text`/`media`/
  `template`). `message_id` is the compound `account:wa_id:wamid` id.
- `react` adds an emoji reaction to a message (`message_id`, `emoji`).
- For text sends, `preview_url=true` enables link previews.

## READING: check / read / search

- `check`: list recent conversations.
- `read`: read messages from one conversation (`wa_id`; optional `limit`,
  `mark_read`).
- `search`: regex search over message text (`query`).

## CONTACTS / ACCOUNTS / STATUS

- `contacts`: list saved contacts. `add_contact`/`remove_contact` manage aliases.
- `accounts`: list configured WhatsApp accounts (redacted).
- `status`: connection/health status for an account.

## SIDE EFFECTS & ERROR SURFACING

- `send`, `reply`, and `react` deliver to real users — external side effects.
  Confirm recipient and content before sending unsolicited messages, and respect
  the 24-hour window rule above.
- Actions return `{'status': 'ok', ...}` on success or `{'status': 'error',
  'error': <message>, 'error_type': ...}` on failure (e.g. missing `to`, invalid
  template, outside-window free text). Check the status and surface or act on
  errors rather than assuming delivery.
