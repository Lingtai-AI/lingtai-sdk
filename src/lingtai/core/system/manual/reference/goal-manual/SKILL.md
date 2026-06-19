---
name: goal-manual
description: >
  Goal notification manual: `.notification/goal.json` source-of-truth, fields,
  instructions, idle reminders, protected dismiss behavior, and cancellation or
  completion semantics.
version: 0.2.0
tags: [lingtai, goal, notifications, reminders]
---

# Goal Manual

A goal is represented by `.notification/goal.json`. This file is the protected
source of truth for the agent's current active goal. The short reminder event only
points the agent back to this file; it does not contain the goal details.


## Guided setup via `/goal`

`/goal` is the TUI entry point for human-guided goal creation. It does **not**
write `.notification/goal.json` directly. Instead, the TUI appends a system event
to `.notification/system.json` so the running agent can guide the human before
creating or changing the goal file:

```json
{
  "source": "goal.request",
  "ref_id": "goal.request:<timestamp>",
  "body": "Human wants to set or revise an active goal..."
}
```

When you receive a `source="goal.request"` event:

1. **Read this manual first.** The event is a request to guide goal creation, not
   permission to invent goal details.
2. **Explain the mechanism briefly to the human.** Tell them that an active goal
   lives in `.notification/goal.json`, idle reminders are short
   `goal.reminder` system events, and dismissing a reminder only hides the
   reminder.
3. **Ask for the missing goal fields.** At minimum, clarify:
   - objective: what should be accomplished;
   - criteria: how the human and agent will know it is done;
   - status/id: a stable `data.id` and active status if the goal should start
     now;
   - reminder cadence: optional `data.reminder_delay_seconds`;
   - constraints: deadlines, channels to report on, what not to do, or approval
     gates.
4. **Explain cancellation and completion before writing.** Canceling the goal
   means deleting `.notification/goal.json` or marking `data.status`
   `inactive`/`cancelled`/`canceled`. Completing or superseding it means marking
   status `done`/`complete`/`completed`/`superseded`, or deleting/replacing the
   file. Dismissing a `goal.reminder` does **not** cancel the goal.
5. **Write `.notification/goal.json` only after confirmation.** If the human gave
   a complete inline draft with `/goal <text>`, restate the structured goal and
   ask for confirmation unless the instruction explicitly and unambiguously says
   to create it now.
6. **Dismiss the request after handling it.** Use the event `ref_id` so other
   system events survive:

```text
system(action="dismiss", channel="system", ref_id="goal.request:<timestamp>")
```

If the human changes their mind during setup, dismiss the `goal.request` event
without creating `goal.json`. If a previous active goal exists, do not overwrite
it silently; explain the existing goal and ask whether to complete, cancel, or
replace it.

## Minimal goal file

```json
{
  "header": "Active goal: finish notification PR",
  "icon": "🎯",
  "priority": "high",
  "published_at": "2026-06-10T00:00:00Z",
  "instructions": "Current active goal. Read data.objective and data.criteria. This channel is protected: do not dismiss it. To cancel the goal, delete .notification/goal.json. See the goal manual under system-manual.",
  "data": {
    "id": "notification-pr",
    "status": "active",
    "objective": "Implement notification whitelist, atomic system-event dismiss, and goal reminders.",
    "criteria": ["tests pass", "manuals updated", "PR opened"],
    "reminder_delay_seconds": 120
  }
}
```

Recommended fields live under `data`:

- `id`: stable identifier used by reminders as `ref_id="goal:<id>"`.
- `status`: omit or use `active` for active work. `done`, `complete`,
  `completed`, `superseded`, `cancelled`, `canceled`, or `inactive` suppress
  reminders.
- `objective`: what to accomplish.
- `criteria`: what done means.
- `reminder_delay_seconds`: optional idle delay before a reminder. If absent, the
  runtime reuses the agent's soul delay; invalid values fall back safely.

The top-level `instructions` field should explicitly say to read the goal data,
that the `goal` channel is protected, and that this manual has the mechanism
details.

## Protected dismiss behavior

`goal` is not an ordinary dismissible notification mirror. Generic dismiss refuses:

```text
system(action="dismiss", channel="goal")  # refused
```

To cancel the goal, delete `.notification/goal.json`. To complete the goal, either
mark `data.status` as `done`/`superseded` or delete/replace the file. Agents are
trusted to decide when the source-of-truth file should change; the protection only
prevents misleading API semantics where dismissing a notification looks like
canceling a goal.

## Idle reminder behavior

The reminder trigger requires the source-of-truth file itself: if
`.notification/goal.json` is absent, no goal reminder is generated.

When `.notification/goal.json` exists, is active, and the agent remains IDLE for
the configured delay, the kernel publishes one short event into
`.notification/system.json`:

```json
{
  "source": "goal.reminder",
  "ref_id": "goal:<id>",
  "body": "Goal reminder: read .notification/goal.json and follow its instructions; see the goal manual under system-manual."
}
```

The reminder is intentionally brief. The actual goal and instructions stay in
`goal.json`. Dismissing the reminder clears only the system event:

```text
system(action="dismiss", channel="system", ref_id="goal:<id>")
```

Dismissing the reminder does not cancel the goal. If the goal remains active, a
fresh idle interval can generate another reminder. If `goal.json` is deleted or
marked inactive/done while a reminder is already present, the next IDLE goal
check clears that stale `goal.reminder` system event.

## Cross-reference

For notification channels, envelopes, allowlist behavior, and atomic system-event
dismiss, read `reference/notification-manual/SKILL.md`.
