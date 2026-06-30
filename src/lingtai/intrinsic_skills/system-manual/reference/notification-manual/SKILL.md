---
name: notification-manual
description: >
  Notification filesystem + standalone notification tool manual for LingTai:
  channel whitelist, `.notification/<channel>.json` files, envelope shape,
  the `notification` tool (check / dismiss_channel / dismiss_event / dismiss_ref),
  generic vs producer-specific dismiss, protected channels, stale-version and
  force semantics, and how large results are ranked via agent_meta
  (`current_tool_result_chars`) and summarized — with legacy-compatible
  dismiss for any persisted `large_tool_result` events.
version: 0.2.0
tags: [lingtai, notifications, channels, dismiss, large-result, force, stale, nudge]
last_changed_at: "2026-06-27T14:31:30-07:00"
---

# Notification Manual

LingTai notifications are a filesystem protocol. Producers write JSON files under
an agent's `.notification/` directory; the kernel reads allowlisted files and
syncs the current notification block into the agent's model context. The
agent-facing verbs for reading and clearing those channels live on the
standalone **`notification` tool** — `system` owns none of them.

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

The `nudge` channel is the formal surface for mechanical, throttled checks. For
example, kernel runtime/update checks publish `data.nudges[]` entries with
`kind: kernel_version`; interpret those with
`reference/runtime-update-checks/SKILL.md` before asking the human to update or
refresh.

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

## The `notification` tool

The notification verbs live **only** on the dedicated, always-available
`notification` tool. The `system` tool no longer accepts any notification or
dismiss action — there are no compatibility aliases.

| Action | What it does |
| --- | --- |
| `notification(action="check")` | Read the live notification payload (placeholder + kernel-stamped `notifications`). |
| `notification(action="dismiss_channel", channel=...)` | Clear one channel whole. |
| `notification(action="dismiss_event", event_id=..., channel="system")` | Remove one `system` event by `event_id`. |
| `notification(action="dismiss_ref", ref_id=..., channel="system")` | Remove `system` event(s) by `ref_id`. |

To compress a large tool result, use `system(action="summarize")` — `summarize`
is a context-hygiene operation owned by `system`, **not** a notification verb.

## Model-visible notification block

`notification(action="check")` returns a placeholder; the kernel stamps the live
notification payload onto that tool result. The same payload is synthesized when
notifications arrive while the agent is IDLE or ASLEEP. The payload contains a
`_meta.notification_guidance` plus `_meta.notifications:{...}` keyed by channel.
After handling a notification, dismiss it and end your turn — do not call
`check` voluntarily again.

## Atomic dismiss

Dismissal is **atomic**: each removal target has its own action, so the call
states exactly what is being cleared. There is no kitchen-sink `dismiss`.

Use producer-specific verbs for channels that mirror producer-owned state. For
example, email unread notifications should be cleared by `email(action="read"...)`
or `email(action="dismiss"...)`, not by a generic channel dismiss.

Clear a whole channel surface:

```text
notification(action="dismiss_channel", channel="nudge")
notification(action="dismiss_channel", channel="system")
```

`dismiss_channel` clears `.notification/<channel>.json` whole. It rejects
`event_id`/`ref_id` — use the atomic event verbs for targeted removal.

Remove a single `system` event without clearing the whole channel:

```text
notification(action="dismiss_event", event_id="evt_...")
notification(action="dismiss_ref", ref_id="goal:current")
```

`dismiss_event`/`dismiss_ref` default `channel` to `system` (the only channel
with per-event structure). They remove only matching entries from
`system.data.events`; if the last event is removed, `.notification/system.json`
is deleted.

## Stale-version and force semantics

Generic dismiss refuses to clear a channel whose on-disk version changed after
the delivered notification version, returning `reason="stale_channel_version"`.
Read the current state first, or pass `force=true` to knowingly clear a stale
mirror. `force=true` also bypasses a producer-registered generic-dismiss guard.
`force` never touches producer-owned state.

## Large results — ranking and summarization

Large tool results are **not** pushed as notifications. The kernel does not
raise `source="large_tool_result"` system events. Instead, the largest formal
tool results currently in context are ranked under
`_meta.agent_meta.current_tool_result_chars`:

- `top_results` — up to the largest few in-context formal results
  (`id`, `tool_name`, `chars`; no preview), sorted by char count.
- `threshold` — the configured large-result hint size in chars.
- `over_threshold_count` — how many in-context formal results exceed it.

This ranking is the candidate list for proactive summarization. When a result
has served its purpose, digest it, then compact it:

```text
system(action="summarize", items=[{"tool_call_id": "toolu_...", "summary": "..."}])
```

Pick the `tool_call_id`(s) from `current_tool_result_chars.top_results`. A
successful summarize records a compact runtime-history replacement; the
provider-context replacement waits for delayed reconstruction. See the
`summarize-manual` for the full progressive-disclosure workflow.

**Legacy compatibility — pre-existing `large_tool_result` events.** New large
results never create notifications, but a `large_tool_result` system event may
still be present if it was persisted before this change or carried across a
molt. Such a stale event can be discharged two ways:

```text
system(action="summarize", items=[{"tool_call_id": "<id>", ...}])  # also clears a matching legacy reminder
notification(action="dismiss_ref", ref_id="large_tool_result:<tool_call_id>")
notification(action="dismiss_event", event_id="<event_id>")
```

A successful summarize of a still-accessible `tool_call_id` also auto-clears a
matching legacy `large_tool_result:<tool_call_id>` reminder if one exists.
Dismissal is the escape hatch for stale or pre-molt refs that can no longer be
summarized; it clears only the notification surface, leaving the original
result unchanged in chat history and `events.jsonl`. Whole-channel system
dismiss (`dismiss_channel channel="system"`) also covers any such legacy
events.

### Progressive disclosure — digesting tool results

Summarization is a general progressive-disclosure tool, not a large-result-only
cleanup path. A summarized tool result is usually the best long-lived form once
you have read and digested the raw payload: future context keeps the conclusion,
key evidence, paths/IDs, validation state, risks, and next steps, while the full
original remains recoverable from `events.jsonl`.

A result ranked high in `current_tool_result_chars.top_results` is a strong
prompt to summarize because the raw payload is already harming context hygiene.
Smaller results may also be summarized whenever the future agent no longer needs
the full raw output.

When digesting any tool result, write an index-style summary:

1. **Conclusion** — what the result says in one sentence.
2. **Key evidence** — the 3-5 most important facts, paths, IDs, or values.
3. **Validation status** — any errors, warnings, or unexpected findings.
4. **Risks / caveats** — what to watch out for.
5. **Next steps** — what to do with this information.

Once digested, call `system(action="summarize")` to replace the context-visible
copy with your summary, then continue your work. Do this for any result worth
compressing, not only for results that crossed the long-result threshold.

> **Timing note:** `system.summarize` can summarize only already-completed prior
> tool results — it cannot summarize the current result in the same tool batch
> before that result exists. On the **next step**, you may run
> `system(action="summarize")` in parallel with other independent work to digest
> a prior result.

## Protected channels

Some channels are source-of-truth files, not dismissible mirrors. `goal` is
protected: `notification(action="dismiss_channel", channel="goal")` refuses even
with `force=true`. To cancel or complete a goal, edit or delete
`.notification/goal.json` as described in the goal manual.

## post-molt acknowledgement

The kernel-owned `post-molt` continuation channel requires a `reason` to clear:

```text
notification(action="dismiss_channel", channel="post-molt", reason="continue: ...")
```

## Producer canonical state vs notification mirror

A channel dismiss clears only the notification *mirror* surface. Producer-owned
canonical state (mailbox read-state, goal source of truth, etc.) is never
touched by a generic dismiss — that is why producer-owned channels are guarded
and steer you to the producer's own verb.

## Cross-reference

For active goal state and goal reminders, read `reference/goal-manual/SKILL.md`.
For `summarize` (context hygiene and tool-result digestion), see the
substrate manual's system-operations section under `system-manual`.
