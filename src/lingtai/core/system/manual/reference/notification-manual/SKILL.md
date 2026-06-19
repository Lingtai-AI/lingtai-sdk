---
name: notification-manual
description: >
  Notification filesystem manual for LingTai kernel notifications: channel
  whitelist, `.notification/<channel>.json` files, envelope shape, instructions,
  generic/producer-specific dismiss, protected channels, and per-event system
  dismiss.
version: 0.1.0
tags: [lingtai, notifications, system, channels, dismiss]
---

# Notification Manual

LingTai notifications are a filesystem protocol. Producers write JSON files under
an agent's `.notification/` directory; the kernel reads allowlisted files and
syncs the current notification block into the agent's model context.

## Channel files and whitelist

A notification channel is the filename stem in `.notification/<channel>.json`.
For example:

- `.notification/email.json` → `notifications["email"]`
- `.notification/system.json` → `notifications["system"]`
- `.notification/mcp.telegram.json` → `notifications["mcp.telegram"]`
- `.notification/goal.json` → `notifications["goal"]`

The kernel uses an allowlist: built-in channels such as `email`, `system`,
`soul`, `nudge`, `post-molt`, `tool_loop_guard`, `bash`, `btw`, `cron`, `molt`,
and `goal` are accepted; MCP bridge channels are accepted by the `mcp.` prefix.
Unknown `.json` files are ignored by `collect_notifications()` and kernel helper
publish/dismiss calls reject non-allowlisted channel names.

## Envelope shape

Producer helpers write a standard envelope:

```json
{
  "header": "1 system notification",
  "icon": "🔔",
  "priority": "normal",
  "published_at": "2026-06-10T00:00:00Z",
  "instructions": "Optional agent-facing handling guidance.",
  "data": {"events": []}
}
```

`instructions` is a field inside a channel payload, not a channel name. It should
say what the agent should do with this notification and how to clear it.

## Model-visible notification block

`system(action="notification")` returns a placeholder; the kernel stamps the live
notification payload onto that tool result. The same payload is synthesized when
notifications arrive while the agent is IDLE or ASLEEP. The payload contains a
global `_notification_guidance` plus `notifications:{...}` keyed by channel.

## Dismiss semantics

Use producer-specific verbs for channels that mirror producer-owned state. For
example, email unread notifications should be cleared by `email(action="read"...)`
or `email(action="dismiss"...)`, not by generic `system.dismiss`.

Generic dismiss clears only the notification surface:

```text
system(action="dismiss", channel="nudge")
```

For `.notification/system.json`, the old whole-channel behavior remains when no
`event_id` or `ref_id` is supplied:

```text
system(action="dismiss", channel="system")
```

Atomic per-event dismiss is available for system events:

```text
system(action="dismiss", channel="system", event_id="evt_...")
system(action="dismiss", channel="system", ref_id="goal:current")
```

This removes only matching entries from `system.data.events`; if the last event
is removed, `.notification/system.json` is deleted.

## Protected channels

Some channels are source-of-truth files, not dismissible mirrors. `goal` is
protected: `system(action="dismiss", channel="goal")` refuses even with
`force=true`. To cancel or complete a goal, edit or delete `.notification/goal.json`
as described in the goal manual.

## Cross-reference

For active goal state and goal reminders, read `reference/goal-manual/SKILL.md`.
