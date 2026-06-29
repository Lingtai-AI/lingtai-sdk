---
name: wechat-mcp-manual
description: |
  Progressive-disclosure usage manual for the WeChat MCP tool. Read this when you
  need detail beyond the one-line action descriptions: user_id targeting, send vs
  reply, check/read/search, media_path attachments (image/video/voice/file),
  contacts/accounts basics, and external-delivery side-effect caveats. Pulled on
  demand via action='manual'; you do not need to call it before every send.
version: 1.0.0
last_changed_at: "2026-06-26T14:33:19-07:00"
---

# WeChat MCP — usage manual (progressive disclosure)

This manual is pulled on demand via `action='manual'` so the per-action tool
schema can stay concise. Read it when you need detail beyond the one-line action
descriptions; you do not need to call it before every send.

## RECIPIENTS: user_id

- Messages target a WeChat user by `user_id` (e.g. `wxid_abc123@im.wechat`). Use
  the `user_id` returned by `check`/`read`/`contacts`; do not invent one.

## SEND vs REPLY

- `reply` (`message_id` from read results, `text`) threads your response to a
  specific incoming message; prefer it when answering a particular message.
- `send` (`user_id`, `text`) starts a fresh message; use it for unsolicited or
  standalone messages.

## MEDIA / ATTACHMENTS

- `send` with `media_path` (absolute path) attaches a file. Type is detected from
  the extension: `.jpg`/`.png` → image, `.mp4` → video, `.wav`/`.mp3` → voice,
  anything else → file.
- For charts, reports, and other artifacts the user should open intact, send them
  as a file/document rather than pasting a local path into the message text.

## INBOUND MEDIA / FILES

- Inbound media is rendered into message text as tags such as `[Image: path]`,
  `[Voice: "transcript" (audio: path)]`, `[File: name (path)]`, and
  `[Video: path]`. Use those paths as local artifacts, not as messages to paste
  back to the user.
- WeChat document downloads may be encrypted/cache placeholders rather than the
  real PDF/ZIP/etc. Before parsing a received file, validate its magic bytes
  (for example `%PDF-` for PDFs, `PK` for ZIP/DOCX). If the bytes do not match
  the claimed file type, ask the user to re-export with WeChat "Save As" or send
  a cloud/download link. This is an agent-side validation practice, not a
  guarantee from the MCP transport.
- Images and transcribed voice messages are usually more directly usable, but
  still verify file existence/readability before analysis.

## READING: check / read / search

- `check`: list recent conversations with unread counts; treat previews as
  hints, not complete context.
- `read`: read messages from one user (`user_id`; optional `limit`). The read
  view merges inbox and sent messages, which helps confirm whether you already
  replied.
- `search`: regex search over inbox messages (`query`; optional `user_id`). It is
  for locating inbound content, not proving that no sent reply exists.

## WAKE / REPLAY / DUPLICATE-REPLY DISCIPLINE

- `user_id` is the routing truth; aliases are convenience labels only. When in
  doubt, use the `user_id` returned by `read`/`check`, especially for replies.
- Reply once per inbound `message_id`. Before sending after a refresh, molt, or
  worker-hang recovery, use `read` to reconcile the merged inbox+sent view and
  avoid duplicate replies.
- If a wake notification is based on a preview and an immediate `read`/`check`
  seems blocked by idle/sleep recovery, acknowledge from the preview if safe,
  then retry the producer read once the agent is active. Avoid tight polling
  loops.
- Some runtimes deduplicate upstream inbound replay by provider `message_id` and
  cursor checkpoints; if investigating inflated unread counts, confirm the
  runtime version/state before assuming the MCP lost messages.

## CONTACTS / ACCOUNTS

- `contacts`: list saved contacts.
- `add_contact`: save a contact alias (`user_id`, `alias`).
- `remove_contact`: remove a contact (`alias` or `user_id`).
- `accounts`: list configured WeChat accounts.

## SIDE EFFECTS & ERROR SURFACING

- `send` and `reply` deliver to real users — external side effects. Confirm
  recipient and content before sending unsolicited messages.
- Actions return a result dict on success or `{'error': <message>}` on failure
  (e.g. missing `user_id`, unreadable `media_path`). Check for the `'error'` key
  and surface or act on it rather than assuming delivery.
