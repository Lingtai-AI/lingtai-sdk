# intrinsics/notification

Standalone notification surface — the **only** agent-callable home for the
notification verbs, **mandatory-included** like `system`. It owns reading the
live notification surface (`check`) and clearing notification mirrors via three
**atomic** dismiss verbs (`dismiss_channel`, `dismiss_event`, `dismiss_ref`).
There is no kitchen-sink `dismiss`. The `system` tool exposes **no** notification
or dismiss verb — there are no compatibility aliases. `summarize` is **not** here:
it remains a `system` action (context hygiene, not a notification verb).

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — dispatch over four actions.
  - `get_description` / `get_schema` (re-exported from `schema.py`) — tool registration.
  - `handle()` (`__init__.py:148-162`) — dispatcher over `check`, `dismiss_channel`, `dismiss_event`, `dismiss_ref`. Unknown actions return a `status="error"` dict.
  - `_check()` (`__init__.py:67-72`) — voluntary read of the notification surface. Returns a placeholder dict (`_notification_placeholder: True` + message). The live payload (`_meta.notifications` + `_meta.notification_guidance`) is stamped onto this same result by `meta_block.attach_active_notifications`, which walks backward for the freshest *dict-shaped* tool result (`meta_block.py:244-258`) — tool-name-agnostic, so `notification(action=check)` receives the identical stamp the old `system(action="notification")` placeholder did.
  - `_dismiss_channel()` (`__init__.py:75-104`) — whole-channel clear. Rejects `event_id`/`ref_id` (those are atomic-event verbs). Delegates to `notifications.dismiss_channel(..., invoked_by="notification")`.
  - `_dismiss_event()` (`__init__.py:107-122`) — remove one `system` event by `event_id`; `channel` defaults to `system`. Delegates to the same helper with `event_id=...`.
  - `_dismiss_ref()` (`__init__.py:125-140`) — remove `system` event(s) by `ref_id`; `channel` defaults to `system`. Delegates with `ref_id=...`.
  - All three dismiss verbs route into the single canonical `notifications.dismiss_channel`. The decision logic (allowlist, `post-molt` ack-reason, protected channels, generic-dismiss guard, stale-channel-version refusal, **`large_tool_result` undismissable guard**, atomic `event_id`/`ref_id` removal) lives there; `invoked_by="notification"` only affects which provenance log line is emitted.

- `schema.py` — tool registration. Exposes `action` (`check`/`dismiss_channel`/`dismiss_event`/`dismiss_ref`) plus the params `channel`, `force`, `event_id`, `ref_id`, `reason`. All param descriptions use **notification-owned `notification_tool.*` i18n keys** (en/zh/wen). There is no `items` param and no `summarize` action — summarize lives on `system`.

## Connections

- `ALL_INTRINSICS["notification"]` (`intrinsics/__init__.py:8-16`) → `BaseAgent._wire_intrinsics()` (`base_agent/__init__.py:580`) binds `handle()` into every agent's tool surface. **Membership in `ALL_INTRINSICS` is the mandatory-include mechanism** — the wiring loop is unconditional, with no manifest gate, so this tool is always present like `system`.
- Delegates into the kernel-root `notifications.dismiss_channel` (`notifications.py:477`). All #424 guards therefore hold through this tool by construction.
- The live-payload stamp is performed by `meta_block.attach_active_notifications`, called from `base_agent/turn.py`; see the kernel-root `ANATOMY.md` "Notifications" section.
- **`summarize` is not delegated here.** It stays on `system(action="summarize")` (`intrinsics/system/summarize.py`), which is the only sanctioned discharge for a `large_tool_result` reminder — a successful summarize calls `notifications.clear_large_result_reminders` to auto-clear the matching event. Notification dismiss verbs cannot clear those reminders.

## Composition

- **Parent:** `src/lingtai_kernel/intrinsics/` (see `intrinsics/ANATOMY.md`).
- **Siblings:** `system/` (owns `summarize` and the producer `publish_notification`/`clear_notification` entry points), `email/`, `soul/`, `psyche/`.

## State

- This intrinsic writes no state of its own. Through delegation it mutates `.notification/system.json` (event removal on dismiss) and clears `.notification/<channel>.json` files. Producer-owned canonical state (mailbox read-state, etc.) is never touched — mirror operations only clear the notification surface.

## Notes

- **No `system` compatibility:** `system(action="notification"|"dismiss")` no longer exist. The notification tool is the sole agent-callable surface for these verbs. The kernel still *synthesizes* a notification delivery tool-call pair for IDLE/ASLEEP delivery — now shaped as `notification(action="check")` (`base_agent/__init__.py:1369-1382`), byte-shape-identical to a voluntary `check` so the LLM cannot tell a kernel-injected read from one it issued; the `_synthesized: true` body flag is the only marker. That synthesis is kernel plumbing, not an agent-callable operation.
- **Atomic, not aggregate:** dismissal is split by target (`channel` / `event_id` / `ref_id`) so the API states exactly what is being cleared. `dismiss_channel` refuses `event_id`/`ref_id`; `dismiss_event`/`dismiss_ref` require their target id.
- **Large-result escape hatch:** `large_tool_result` reminders are notification mirrors for context hygiene. A successful `system(action="summarize")` remains the preferred discharge because it digests the underlying result and clears the reminder. Atomic notification dismissals may instead acknowledge and remove the reminder surface (including stale/pre-molt refs) without deleting or mutating the original tool result; acknowledged refs are skipped by later rescans so the same reminder does not immediately reappear. Regression-anchored by `tests/test_notification_tool.py` and `tests/test_system_dismiss.py`.
