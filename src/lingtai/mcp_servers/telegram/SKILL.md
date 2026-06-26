---
name: telegram-mcp-manual
description: |
  Progressive-disclosure usage manual for the Telegram MCP tool. Read this when
  you need detail beyond the one-line action descriptions: media.type='document'
  vs 'photo' for charts/reports/generated artifacts, placeholder/progress
  messages, reply vs send, read/check/search, parse_mode/entities, chat_action,
  and error surfacing. Pulled on demand via action='manual'; you do not need to
  call it before every send.
version: 1.0.0
---

# Telegram MCP — usage manual (progressive disclosure)

This manual is pulled on demand via `action='manual'` so the per-action tool
schema can stay concise. Read it when you need detail beyond the one-line action
descriptions; you do not need to call it before every send.

## MEDIA: document vs photo

- Charts, plots, reports, HTML/SVG/PNG/PDF exports, CSVs, and any other
  generated artifact the user should open intact: send with
  `media.type='document'`. Documents arrive as a downloadable file, uncropped
  and uncompressed.
- `media.type='photo'` is for native inline photo previews only. Telegram may
  crop, compress, thumbnail, or otherwise degrade text-heavy graphics sent as a
  photo, so a chart can look cropped or unreadable.
- Do not paste a local file path into message text as a substitute for
  attaching the file; attach it with `media={type, path}`.

## PLACEHOLDER / PROGRESS

- For responses that take more than ~5s, send `action='send'` with
  `placeholder=true` (and your interim text). This fires a typing indicator and
  returns a compound `message_id`.
- Update it later with `action='edit'`, `message_id=<that id>`, `text=<final>`
  instead of sending a second message, so the user sees one evolving reply
  rather than silence followed by a wall of text.

## REPLY vs SEND

- `action='reply'` (`message_id` from read/check results, `text`) threads your
  response to a specific message and adds a ✅ reaction to it; prefer it when
  answering a particular incoming message.
- `action='send'` (`chat_id`, `text`) starts a fresh message in the chat; use it
  for unsolicited or standalone messages.

## READING: read / check / search

- `check`: list recent conversations with unread counts.
- `read`: read messages from one chat (`chat_id`; optional `limit`). Reading
  marks messages read and clears the wake notification mirror.
- `search`: regex search over message text/sender (`query`; optional `chat_id`,
  `account`).

## RICH TEXT: parse_mode / entities

- `parse_mode` accepts `'HTML'`, `'MarkdownV2'`, or `'Markdown'` for
  send/reply/edit and media captions; omit it or pass `''` for plain text.
- `entities` sets `MessageEntity[]` for message text; `caption_entities` does the
  same for media captions. If `caption_entities` is omitted on a media send,
  `entities` is reused as the caption entities.

## CHAT ACTION

- `chat_action` (`'typing'`, `'upload_photo'`, `'upload_document'`,
  `'upload_voice'`) on a send with no text/media sends just the indicator. It
  auto-expires after ~5s, so re-send periodically during long work. Pass `''`
  for no chat action.

## ERROR SURFACING

- Actions return `{'status': ...}` on success or `{'error': <message>}` on
  failure (e.g. missing `chat_id`, unreadable `media.path`, bad `parse_mode`).
  Check for the `'error'` key and surface or act on it rather than assuming the
  message was delivered.
- A duplicate identical send returns `{'status': 'blocked'}`; treat that as
  'already sent', not as a transient error to retry.
