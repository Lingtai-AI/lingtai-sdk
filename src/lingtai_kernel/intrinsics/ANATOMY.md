# intrinsics

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues/mail/PR proposals; do not silently fix.

Kernel-built-in tools — the primitives every agent always has, never removable. Each is a sub-package with a uniform public shape: `get_schema(lang)`, `get_description(lang)`, `handle(agent, args)`, and (optionally) `boot(agent)`. `ALL_INTRINSICS` registers the modules consumed by `BaseAgent` (`intrinsics/__init__.py:8-16`). As of the notification-tool split there are **five**: `email`, `system`, `psyche`, `soul`, and `notification`.

This file is a navigation hub. Each sub-package has its own `ANATOMY.md` with concrete file:line references; descend into the relevant one rather than expecting full coverage here.

## Components

- `intrinsics/__init__.py` — registry. Imports the sub-packages and exposes `ALL_INTRINSICS = {"email": email, "system": system, "psyche": psyche, "soul": soul, "notification": notification}`. `BaseAgent._wire_intrinsics()` (`base_agent/__init__.py:580`) iterates this dict unconditionally and binds each module's `handle()` into the agent's tool surface — **membership in `ALL_INTRINSICS` is the mandatory-include mechanism**; there is no manifest gate. Adding `notification` here makes it a mandatory tool exactly like `system`.

- [`intrinsics/email/`](email/ANATOMY.md) — filesystem mailbox. Inbox/outbox/sent/archive folders, contacts, recurring schedules, mail delivery via daemon threads. `.notification/email.json` is a **live mirror** of the current unread set: any read-state mutation (`_read`, `_dismiss`, `_archive`, `_delete`) re-renders the digest so the wire's notification reflects the new state on the next heartbeat sync. The digest body inlines each entry's mailbox ID directly under the subject so the agent can pass it to `email_id` without a separate `check` call. Each entry preview is up to 200 chars; truncated previews end with `... (N more chars)`. The agent dismisses handled mails via `email(action="read", email_id=[...])` (returns bodies + clears) or `email(action="dismiss", email_id=[...])` (clears only, no bodies). On bounce, merges into `.notification/system.json` events list. Decomposed in `d229efe` from a 1,530-line `email.py` into a 5-module sub-package (`__init__.py`, `primitives.py`, `schema.py`, `manager.py`).

- [`intrinsics/psyche/`](psyche/ANATOMY.md) — durable self and context lifecycle. The "bare essentials" of agent identity: lingtai (canonical character), pad (working notes), context molt (shed-and-reload), name handling. Decomposed in `1195f55` from a 946-line `psyche.py` into `_lingtai.py`, `_pad.py`, `_snapshots.py`, `_molt.py`, plus `__init__.py` with explicit `_DISPATCH` table (replaced the old `globals().get()` pattern). Snapshot orphan-tool-call closure landed in `704731b`.

- [`intrinsics/soul/`](soul/ANATOMY.md) — inner voice and mechanical soul-flow. Four soul-domain actions (`inquiry`, `config`, `voice`, `flow`) plus a `dismiss` alias for the generic notification-dismiss path — `flow` also fires on a wall-clock timer. Decomposed earlier into `config.py`, `consultation.py`, `inquiry.py`, `flow.py`, `__init__.py`. The flow trunk (`flow.py`) owns the wall-clock timer and writes `.notification/soul.json` via `publish_notification`; the kernel's `_sync_notifications` picks up the fingerprint change and surfaces the voices inside the unified `notification(action="check")` wire pair.

- [`intrinsics/system/`](system/ANATOMY.md) — runtime, lifecycle, synchronization. Nap, refresh (preset hot-reload + authorization gate), karma-gated lifecycle actions on other agents (sleep, lull, suspend, cpr, interrupt, clear, nirvana), preset listing, and `summarize` (context-hygiene replacement of a prior tool result; a successful summarize auto-clears the matching `large_tool_result` reminder). **No notification verb** — `system(action="notification"|"dismiss")` were removed in the notification-tool split; the notification surface lives entirely on the `notification` tool. Still the **producer publish home** — re-exports `publish_notification` / `clear_notification` from the kernel-root `notifications.py` so any in-process producer submits through one canonical entry point. Decomposed in `e206dbc` from a 641-line `system.py` into `preset.py`, `karma.py`, `summarize.py`, `schema.py`, `__init__.py` with explicit dispatch table.

- [`intrinsics/notification/`](notification/ANATOMY.md) — the **standalone notification surface**, the only agent-callable home for the notification verbs (mandatory-included like `system`). `check` returns a placeholder dict the meta-block stamps the live payload onto; dismissal is **atomic** — `dismiss_channel` (whole channel), `dismiss_event` (one `system` event by `event_id`), `dismiss_ref` (by `ref_id`). All three delegate to the single canonical `notifications.dismiss_channel` with `invoked_by="notification"`; no notification logic is reimplemented. `summarize` is **not** here — it stays a `system` action. There are **no** `system` compatibility aliases. All #424 large-result-reminder guards live in `notifications.dismiss_channel` / `clear_large_result_reminders` and hold through the atomic verbs by construction; `force` is not a backdoor. Files: `__init__.py` (dispatch), `schema.py` (notification-owned `notification_tool.*` i18n strings).

## Connections

- `BaseAgent._wire_intrinsics()` imports `ALL_INTRINSICS` and binds each module's `handle()` callback. Boot hooks are special-cased: `BaseAgent` calls `psyche.boot(agent)` and `email.boot(agent)` during construction (the soul and system intrinsics have no boot hook).
- Cross-intrinsic flows worth knowing about:
  - **soul → psyche state**: `_run_consultation_batch` reads `history/snapshots/snapshot_*.json` written by `psyche._write_molt_snapshot` as past-self substrate.
  - **email → kernel notifications**: `_rerender_unread_digest` writes `.notification/email.json` via `system.publish_notification`; the same path is invoked from every read-state mutator (`_read`, `_dismiss`, `_archive`, `_delete`) so the digest mirrors current unread state live. Bounce events go through `_mailman` → `.notification/system.json` via `_enqueue_system_notification`. The unread-mail envelope carries an `instructions` field naming the read/dismiss contract directly in the payload — the kernel does not have to know about per-producer dismissal semantics.
  - **soul → kernel notifications**: `_run_consultation_fire` writes `.notification/soul.json` via `system.publish_notification` after every successful fire (or `clear_notification` when voices are empty). The envelope's `instructions` field defines `source='insights'` (current-self reflection) vs `source='snapshot:*'` (past-self from before a molt) and reaffirms that the human reaches the agent only through email — so a voice's narration of an external event must be verified before being acted on.
  - **psyche → kernel notifications**: the molt path (`_context_molt`, `context_forget` in `psyche/_molt.py`) resets `agent._notification_block_id` plus any legacy `_pending_notification_*` attributes (wire-level state) and resets `agent._notification_fp`, while preserving `.notification/*.json` files — notifications are system state, not conversation memory, so they survive molt.
  - **All in-process producers** call `from ..intrinsics.system import publish_notification, clear_notification` (or `from ...notifications import submit, clear` for code outside `intrinsics/`). External producers (MCP servers over SSH) write `<workdir>/.notification/mcp.<server>.json` directly with `tmp + rename`. See root `ANATOMY.md` "Notifications" for the contract.
- All five intrinsics use `i18n.t()` for localized descriptions and schemas.

## Composition

- **Parent:** `src/lingtai_kernel/` (see `src/lingtai_kernel/ANATOMY.md`).
- **Sub-packages:** all five intrinsics are packages (`email`/`psyche`/`soul`/`system` post-`d229efe`/`1195f55`/`e206dbc`; `notification` born as a package in the notification-tool split). There are no flat-file intrinsics remaining.
- **Siblings:** `llm/` for canonical block/session types, `services/` for mailbox/logging service implementations, `i18n/` for localized strings, `base_agent/` for the coordinator that wires intrinsics in.

## State

Detailed file/path lists belong in each sub-anatomy's State section. High-level summary:

- `email/` writes `mailbox/{inbox,outbox,sent,archive}/<id>/message.json`, `mailbox/read.json`, `mailbox/contacts.json`, `mailbox/schedules/<id>/schedule.json`.
- `psyche/` writes `system/lingtai.md`, `system/pad.md`, `system/pad_append.json`, `system/summaries/molt_<count>_<ts>.md`, and `history/snapshots/snapshot_<count>_<ts>.json`.
- `soul/` writes `logs/soul_flow.jsonl`, `logs/soul_inquiry.jsonl`, mutates `init.json` (manifest.soul.* for cadence/voice config), and writes token-ledger entries for soul LLM calls.
- `system/` mutates process/lifecycle state; karma actions write signal files (`.sleep`, `.suspend`, `.interrupt`, `.clear`) into target agent working directories; nirvana removes target working directories entirely.

## Notes

- **Intrinsics are kernel primitives, not optional capabilities.** Capabilities (in the wrapper layer at `lingtai/core/`) may wrap or override them via `BaseAgent.override_intrinsic()` (`base_agent/__init__.py:759`).
- **Uniform public shape**: every intrinsic exposes `get_schema(lang)`, `get_description(lang)`, `handle(agent, args)`. Boot hooks are optional (`psyche.boot`, `email.boot`).
- **`notification` action is agent-callable but does not return channel data directly**: `system.handle()` returns a placeholder dict (`_notification_placeholder: True` + explanatory message); the canonical live payload (`_meta.notifications` + `_meta.notification_guidance`) is stamped onto that same result by `attach_active_notifications` post-hook. Kernel-synthesized notification reads use the same canonical payload shape but additionally carry `_synthesized: True` in the JSON body and on the `ToolResultBlock.synthesized` flag. There is one and only one live notification payload in conversation history at any moment.
- **Decomposition rationale**: each of the four decomposed intrinsics (email, psyche, soul, system) hit a complexity threshold where its internal subsystems no longer fit cleanly in one file (mailbox I/O vs delivery vs scheduling for email; molt vs pad vs snapshot for psyche; flow vs consultation vs config for soul; nap vs preset vs karma vs deprecation shim for system). `notification` is a thin façade and stays small by design. Sub-anatomies document the per-package internal layout.
