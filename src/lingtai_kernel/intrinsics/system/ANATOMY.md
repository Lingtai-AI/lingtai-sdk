# intrinsics/system

System intrinsic — runtime, lifecycle, and synchronization. Provides the agent with refresh (hot-reload config/presets), karma-gated lifecycle actions on other agents (sleep, lull, suspend, cpr, interrupt, clear, nirvana), preset listing, voluntary notification reads (`action="notification"`), and a generic notification dismiss (`dismiss`). The system module is also the **conceptual home** of the notification surface — it re-exports `publish_notification` / `clear_notification` from the kernel-root `notifications.py` so any in-process producer (intrinsic, capability, or wired-in MCP server) submits through one canonical entry point.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `__init__.py` — Package surface. Re-exports all public API for backward compatibility. Contains:
  - `get_description` / `get_schema` (re-exported from `schema.py`) — tool registration.
  - `_dismiss` (re-exported from `notification.py`) — agent-facing generic notification dismiss; routes `channel`/`force` through `notifications.dismiss_channel` and keeps a one-release legacy `ids=` soak path.
  - **`publish_notification` / `clear_notification`** (re-exported from `lingtai_kernel.notifications` as `submit` / `clear`) — canonical producer entry point. Importable as `from lingtai_kernel.intrinsics.system import publish_notification, clear_notification`. The system module owns the notification surface conceptually; the implementation lives at the kernel root so non-intrinsic callers (and external scripts) can import it without going through the intrinsic surface.
  - All handler functions re-exported from sub-modules for backward compatibility.
  - `handle()` (`__init__.py:82-109`) — main dispatcher with explicit dispatch table. The `notification` action takes a fast path that returns `collect_notifications(agent._working_dir)` directly without going through the dispatch table — voluntary reads of the agent's own `.notification/` state.

- `preset.py` — Preset management and refresh.
  - `_preset_ref_in()` (`preset.py:9-36`) — normalized membership test for preset path strings (~/foo vs absolute).
  - `_check_context_fits()` (`preset.py:39-76`) — verify agent's current context fits within target preset's context_limit.
  - `_refresh()` (`preset.py:79-199`) — stop, reload config + MCP servers, restart. Handles preset swap (named or revert) with authorization gate and context-limit guard. **Empty-string normalization:** `args.get('preset')` returning `''` or whitespace-only is treated as absent (`preset_name = None`) before any conflict/swap logic; this protects against tool-call providers that serialize optional string fields as `""` instead of omitting them — without normalization, an empty string would fall into the allowed-list gate and surface as `"preset '' is not in this agent's allowed list"`. The `preset='' + revert_preset=True` combination is consequently treated as a plain revert (no conflict). **MCP retry hook (issue #34):** before calling `agent._perform_refresh()`, invokes `agent._retry_failed_mcps()` if the Agent subclass defines it. Failures are logged and swallowed so a flaky MCP cannot block refresh itself. Lets the documented "fix config → refresh" recovery path work in-process.
  - `_presets()` (`preset.py:202-282`) — list available presets with LLM connectivity probing.

- `karma.py` — Karma-gated lifecycle actions.
  - `_KARMA_ACTIONS` / `_NIRVANA_ACTIONS` (`karma.py:13-14`) — gate mapping sets.
  - `_check_karma_gate()` (`karma.py:15-33`) — authorization gate: validates karma/nirvana admin flags, resolves target address, rejects self-targeting.
  - `_sleep()` (`karma.py:36-91`) — self-sleep (no karma needed). Guards the ACTIVE→ASLEEP transition against a divergent `.notification/` fingerprint: if mail/system payloads arrived during the same turn (so `notification_fingerprint(workdir) != agent._notification_fp`) and `force` is not set, refuses sleep and returns a status-ok refusal message rather than transitioning state. `force=True` bypasses the guard. Fixes kernel#112.
  - `_lull()` (`karma.py:94-107`) — put another agent to sleep.
  - `_suspend()` (`karma.py:109-121`) — suspend another agent.
  - `_cpr()` (`karma.py:124-137`) — resuscitate a suspended agent.
  - `_interrupt()` (`karma.py:140-151`) — interrupt a running agent's current turn.
  - `_clear()` (`karma.py:154-174`) — force a full molt on another agent.
  - `_nirvana()` (`karma.py:177-198`) — permanently destroy an agent's working directory.

- `notification.py` — agent-facing generic dismiss. The `.notification/` filesystem-as-protocol uses one file per producer channel; `_dismiss()` (`notification.py:12-40`) accepts `channel`, optional `force`, and optional `reason`, then calls `notifications.dismiss_channel(agent, ...)`. Legacy `ids=` calls are accepted for one release, log `system_dismiss_legacy_ids_ignored`, and clear nothing. The kernel-owned `post-molt` channel requires a non-empty `reason` so clearing it is an explicit continue/defer/obsolete acknowledgement rather than an accidental sweep.
  - Producer-side notification submission lives in `notifications.py` at the kernel root and is re-exported by this package's `__init__.py` as `publish_notification` / `clear_notification`. See root `ANATOMY.md` "Notifications" for the full architecture and dismissal taxonomy.

- `schema.py` — Tool registration.
  - `get_description()` (`schema.py:5-7`) — returns localized tool description.
  - `get_schema()` (`schema.py:10-46`) — returns JSON schema for the system tool. Action enum includes `dismiss` plus `channel`/`force` parameters for generic notification clearing; legacy `ids` remains handler-only and is no longer taught in schema.

## Connections

- **Inbound:** `handle()` is called by the tool dispatcher (via `base_agent._dispatch_tool`).
- **Inbound (cross-module):** `publish_notification` is imported by `base_agent/messaging.py` (both `_rerender_unread_digest` and `_enqueue_system_notification`) and by `intrinsics/soul/flow.py:_run_consultation_fire`. `clear_notification` is imported by the same call sites for the empty-state path. `_dismiss` is no longer called from `email/manager.py` — email arrivals use the single-slot unread-digest pattern, and dismiss is a no-op shim regardless.
- **Outbound:** Depends on `...notifications` (canonical `submit`/`clear`/`collect_notifications`), `...i18n` (translations), `...handshake` (`resolve_address`, `is_agent`, `is_alive`), `...state` (`AgentState`), `...presets` (preset loading), `...preset_connectivity` (connectivity probing).
- **Data flow:** Karma actions write signal files (`.sleep`, `.suspend`, `.interrupt`, `.clear`) into target agent working directories. Preset swap reads/writes `init.json` manifest. The `notification` action reads `.notification/*.json` (read-only); `publish_notification` re-export writes them via `tmp + rename`.

## Key invariants

- `handle()` uses an explicit dispatch table (`dict.get()`) rather than `globals().get()`, so it works correctly across sub-modules.
- The `notification` action is agent-callable but does **not** return channel data directly. The handler returns a placeholder dict (`_notification_placeholder: True` + a short explanatory message); the canonical live payload (`notifications` + `_notification_guidance`) is then stamped onto that same result by the turn loop's `attach_active_notifications` post-hook — the same path that handles every other dict-shaped tool result. Kernel-synthesized notification reads use the same canonical payload shape but additionally carry `_synthesized: True` (in both the JSON body and the `ToolResultBlock.synthesized` flag) so the agent can tell the two apart in history. This unified path guarantees there is only one live notification payload in conversation history at any moment — voluntary calls never produce a duplicate representation (bare channel keys *and* a wrapped `notifications` key) on the same dict.
- Karma gate checks resolve addresses through `_check_karma_gate()` which validates admin flags before any filesystem mutation.
- `_dismiss` is a channel-level generic clear: guarded producer channels (currently email) refuse unless `force=true`; legacy `ids=` calls are ignored and logged for one release; `post-molt` refuses without a non-empty `reason` so continuation reminders cannot be cleared without an acknowledgement decision.
- Preset swap has two guards: authorization (allowed list) and context-fit (current tokens ≤ target context_limit).
- Producer notification writes (`publish_notification`) are atomic (`tmp + rename` inside `notifications.publish`) — readers never see a half-written file.
