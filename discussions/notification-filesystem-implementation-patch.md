# Implementation Patch: `.notification/` Filesystem Redesign (Phase 1 + Phase 2)

> **Status:** Phase 1 landed at commit `fadbabf`; Phase 2 landed alongside the test migrations.  Initial spec drafted 2026-05-04 by mimo-pro avatar.  Revised + applied 2026-05-05 (claude-opus-4-7).
> **Scope:** Phase 1 (kernel sync mechanism) + Phase 2 (producer migration). Phase 3 (adapter cleanup) explicitly excluded — `tc_inbox` and the `pre_request_hook` plumbing remain in place, dormant after Phase 2 because no producer enqueues anymore.
> **Design doc:** `notification-filesystem-redesign.md`
> **Patch format:** `soul-flow-tool-refusal-patch.md`

## Revisions (2026-05-05)

Six fixes applied to mimo-pro's initial draft after a verification pass against the actual codebase:

1. **`_synthesized` marker (item A from feedback round)**.  Reuses the existing `ToolResultBlock.synthesized: bool` field (already used by `close_pending_tool_calls` for heal-path placeholders) AND wraps the JSON body in `{"_synthesized": true, "notifications": {...}}`.  Same shape for IDLE pair injection (§2b) and ACTIVE meta stash (§2b ACTIVE branch) — agent sees one envelope.
2. **Fingerprint commit guard**.  `_inject_notification_pair` now returns bool; `_sync_notifications` only commits `_notification_fp` when injection succeeds.  Prevents notification drops when `has_pending_tool_calls()` blocks the append path.
3. **`_inject_notification_meta` walks all str-content blocks**, not break-on-first.  If the latest `ToolResultBlock` has dict content (MCP structured result), the helper walks backwards to find a string-content block.  If none exist, defers and retries on next `send()`.  Also extracts a shared `_strip_notification_prefix` helper for idempotent prefix removal.
4. **`_enqueue_system_notification` race lock**.  The read-modify-write merge into `system.json` is now wrapped in a lazily-created `threading.Lock` on the agent.  Only `system.json` needs this — `email.json` and `soul.json` recompute full state per publish so no merge is needed.
5. **`_on_normal_mail` consistency** — keep the `_wake_nap("mail_arrived")` nudge (sub-second sync latency vs. 1s heartbeat); remove only the dead `MSG_TC_WAKE` / `inbox.put` from the old tc_inbox path.  The old §9b had contradictory advice; resolved here.
6. **MSG_TC_WAKE state-transition discipline** — fires only on ASLEEP→IDLE (the wake transition).  IDLE→IDLE strip+reinject does NOT post a wake message.  Documented inline in `_sync_notifications` and confirms feedback-round item C.

---

## Table of contents

1. [New module: `notifications.py`](#1-new-module-notification)
2. [BaseAgent changes](#2-baseagent-changes)
3. [Heartbeat integration](#3-heartbeat-integration)
4. [ACTIVE-state injection (request-send time)](#4-active-state-injection)
5. [IDLE-state sync](#5-idle-state-sync)
6. [ASLEEP-state sync](#6-asleep-state-sync)
7. [`system(action="notification")` intrinsic handler](#7-systemactionnotification-intrinsic-handler)
8. [Tool description update](#8-tool-description-update)
9. [Producer migration: email](#9-producer-migration-email)
10. [Producer migration: soul flow](#10-producer-migration-soul-flow)
11. [Producer migration: system notifications](#11-producer-migration-system-notifications)
12. [Molt clearing](#12-molt-clearing)
13. [Test matrix](#13-test-matrix)
14. [Open questions for human](#14-open-questions-for-human)

---

## 1. New module: `notifications.py`

**File:** `src/lingtai_kernel/notifications.py` (new, ~60 lines)

Complete module:

```python
"""Notification filesystem — `.notification/` dropbox + sync primitives.

Producers write JSON files; the kernel reads them and syncs the agent's
wire context to match.  This module provides the file-level helpers
(fingerprint, collect, publish, clear).  The sync-loop logic (strip +
reinject into the wire) lives on BaseAgent.
"""
from __future__ import annotations

import json
from pathlib import Path


def notification_fingerprint(workdir: Path) -> tuple:
    """Compute a fingerprint of `.notification/*.json`.

    Returns a tuple of (name, mtime_ns, size) triples, sorted by name.
    Empty tuple if the directory is absent or empty.  Used to detect
    whether any producer file has changed since the last poll.
    """
    notif_dir = workdir / ".notification"
    if not notif_dir.is_dir():
        return ()
    return tuple(sorted(
        (f.name, f.stat().st_mtime_ns, f.stat().st_size)
        for f in notif_dir.iterdir()
        if f.is_file() and f.suffix == ".json"
    ))


def collect_notifications(workdir: Path) -> dict:
    """Read `.notification/*.json` and return as a dict keyed by stem.

    Keys are filenames without extension (``email``, ``soul``,
    ``mcp.telegram``, …).  Sorted iteration for deterministic ordering.
    Returns ``{}`` if the directory is absent, empty, or all files are
    unparseable.  Malformed files are silently skipped.
    """
    notif_dir = workdir / ".notification"
    if not notif_dir.is_dir():
        return {}
    out = {}
    for f in sorted(notif_dir.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_bytes())
        except (json.JSONDecodeError, OSError):
            continue
    return out


def publish(workdir: Path, tool_name: str, payload: dict) -> None:
    """Write a notification file atomically (tmp + rename).

    ``tool_name`` is the stem — ``email``, ``soul``, ``mcp.telegram``, etc.
    Overwrites any prior content for that source.
    """
    notif_dir = workdir / ".notification"
    notif_dir.mkdir(exist_ok=True)
    target = notif_dir / f"{tool_name}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.rename(target)


def clear(workdir: Path, tool_name: str) -> None:
    """Delete a producer's notification file.  Idempotent."""
    target = workdir / ".notification" / f"{tool_name}.json"
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
```

---

## 2. BaseAgent changes

**File:** `src/lingtai_kernel/base_agent/__init__.py`

### 2a. New instance attributes (add in `__init__`, after `_appendix_ids_by_source` ~line 353)

```python
# Notification sync state — tracks the last-seen .notification/ fingerprint
# and the entry-id of the currently-injected wire block (if any).
self._notification_fp: tuple = ()
self._notification_block_id: int | None = None
```

### 2b. Add `_sync_notifications` method

Insert after `_drain_tc_inbox_for_hook` (~line 722). This is the single sync mechanism:

```python
def _sync_notifications(self) -> None:
    """Sync `.notification/` state into the wire.

    Computes the current fingerprint; if unchanged, no-op.  On change:
    1. Strip the prior wire block (if any).
    2. If notifications are non-empty, inject a new block appropriate
       for the agent's current state (IDLE pair / ACTIVE meta-stash).
    3. For ASLEEP: transition to IDLE first, then inject as IDLE.
    """
    from ..notifications import notification_fingerprint, collect_notifications

    fp = notification_fingerprint(self._working_dir)
    if fp == self._notification_fp:
        return

    notifications = collect_notifications(self._working_dir)
    prior_block_id = self._notification_block_id

    # --- Strip prior block ---
    if prior_block_id is not None and self._chat is not None:
        try:
            self._chat.interface.remove_pair_by_call_id(prior_block_id)
        except Exception:
            pass
        self._notification_block_id = None

    if not notifications:
        # All cleared — wire now has zero notification blocks.  Commit fp.
        self._notification_fp = fp
        return

    # --- Inject new block based on current state ---
    from ..state import AgentState

    inject_ok = False

    if self._state == AgentState.ASLEEP:
        # Notification arrival wakes the agent, then inject as IDLE.
        # MSG_TC_WAKE only fires here — on the ASLEEP→IDLE state transition.
        # IDLE-state syncs (below) DO NOT re-fire wake; the agent is already
        # awake and will see the strip+reinject naturally on the next turn.
        self._asleep.clear()
        self._cancel_event.clear()
        self._set_state(AgentState.IDLE, reason="notification_arrival")
        self._reset_uptime()
        inject_ok = self._inject_notification_pair(notifications)
        if inject_ok:
            from ..message import _make_message, MSG_TC_WAKE
            try:
                wake_msg = _make_message(MSG_TC_WAKE, "system", "")
                self.inbox.put(wake_msg)
                self._wake_nap("notification_arrival")
            except Exception:
                pass

    elif self._state == AgentState.IDLE:
        # Already awake — strip + reinject only.  No MSG_TC_WAKE: that is
        # reserved for state transitions (ASLEEP→IDLE).  The agent will
        # observe the new pair on its next request-send cycle, which is
        # already imminent if the agent is IDLE on a normal cadence.
        inject_ok = self._inject_notification_pair(notifications)

    elif self._state == AgentState.ACTIVE:
        # Stash for injection at request-send time (meta on latest ToolResult).
        # The actual injection happens in SessionManager.send() via
        # _inject_notification_meta().  The stash carries the body in the
        # ``_synthesized: true`` envelope shape so the agent sees the same
        # signal whether the data arrives as IDLE pair or ACTIVE meta.
        body = {"_synthesized": True, "notifications": notifications}
        self._pending_notification_meta = json.dumps(
            body, indent=2, ensure_ascii=False
        )
        inject_ok = True
        self._log("notification_stashed_active", sources=list(notifications.keys()))

    # STUCK / SUSPENDED — no injection.  Notifications accumulate on disk
    # and will be picked up when the agent returns to a writable state.

    # --- Commit fingerprint only if injection succeeded ---
    # If inject_ok is False (e.g. pending tool_calls blocked the IDLE/ASLEEP
    # append), leave _notification_fp at its prior value so the next
    # heartbeat tick re-detects the change and retries.  STUCK/SUSPENDED
    # cases also commit the fp — the on-disk state is observed; we just
    # can't act on it until state recovers.
    if inject_ok or self._state not in (
        AgentState.IDLE, AgentState.ASLEEP
    ):
        self._notification_fp = fp


def _inject_notification_pair(self, notifications: dict) -> bool:
    """Inject a synthetic (call, result) pair for IDLE / ASLEEP states.

    Builds ``system(action="notification")`` / ``<JSON dict>`` and appends
    to the wire interface.  Records the entry id for later stripping.

    The synthesized ``ToolResultBlock`` is created with ``synthesized=True``
    (the existing flag the kernel already uses for heal-path placeholders).
    The result content also carries a top-level ``_synthesized: true`` field
    in its JSON body so the agent can distinguish kernel-injected reads
    from voluntary calls when reading conversation history.

    Returns True if injection succeeded, False if it had to abort (e.g.
    pending tool_calls block append).  When False is returned, the caller
    must NOT update ``_notification_fp`` — otherwise the change is dropped.
    """
    import secrets
    import json
    from ..llm.interface import ToolCallBlock, ToolResultBlock

    if self._chat is None:
        try:
            self._session.ensure_session()
        except Exception:
            return False

    iface = self._chat.interface
    # Ensure no pending tool_calls that would block appending.
    if iface.has_pending_tool_calls():
        return False  # Will retry on next heartbeat tick.

    call_id = f"notif_{int(time.time()*1000):x}_{secrets.token_hex(2)}"
    body = {"_synthesized": True, "notifications": notifications}
    content_json = json.dumps(body, indent=2, ensure_ascii=False)

    call_block = ToolCallBlock(
        id=call_id,
        name="system",
        args={"action": "notification"},
    )
    result_block = ToolResultBlock(
        id=call_id,
        name="system",
        content=content_json,
        synthesized=True,
    )

    iface.add_assistant_message(content=[call_block])
    iface.add_tool_results([result_block])
    self._notification_block_id = call_id  # ToolCallBlock.id (str)
    self._save_chat_history(ledger_source="notification_sync")
    self._log(
        "notification_pair_injected",
        call_id=call_id,
        sources=list(notifications.keys()),
    )
    return True
```

Also add `_pending_notification_meta` in `__init__` (near the other new attrs):

```python
self._pending_notification_meta: str | None = None  # ACTIVE-state stash
```

### 2c. Add `_inject_notification_meta` method (called from SessionManager.send)

```python
_NOTIF_PREFIX_LEAD = "notifications:\n"


def _strip_notification_prefix(content: str) -> str:
    """Remove a leading ``notifications:\\n…\\n\\n`` block if present.

    Idempotent.  Used both before reprepending fresh meta and on older
    blocks to maintain the single-slot invariant.
    """
    if not content.startswith(_NOTIF_PREFIX_LEAD):
        return content
    end = content.find("\n\n", len(_NOTIF_PREFIX_LEAD))
    if end < 0:
        return content
    return content[end + 2:]


def _inject_notification_meta(self, message) -> Any:
    """ACTIVE-state: prepend notification JSON to a recent str ToolResultBlock.

    Called from SessionManager.send() before the API call.  Walks the wire
    backwards looking for a ``ToolResultBlock`` whose ``.content`` is a
    string (dict-content blocks come from MCP structured results — those
    are skipped to avoid corrupting their schema).  Prepends the
    ``notifications:\\n<json>\\n\\n`` prefix to the most recent
    string-content result, stripping any stale prefix from older results.

    If no string-content ToolResultBlock exists (rare — agent's whole
    chain is structured), ``_pending_notification_meta`` is preserved
    and the next ``send()`` retries.

    Returns the (possibly unchanged) message.
    """
    if self._pending_notification_meta is None:
        return message
    if self._chat is None:
        return message

    iface = self._chat.interface
    notif_prefix = f"{_NOTIF_PREFIX_LEAD}{self._pending_notification_meta}\n\n"

    # Walk backwards to find the most recent user entry whose content
    # contains a *string-content* ToolResultBlock.
    target_entry = None
    target_block = None
    for entry in reversed(iface.entries):
        if entry.role != "user":
            continue
        for block in entry.content:
            if (
                isinstance(block, ToolResultBlock)
                and isinstance(block.content, str)
            ):
                target_entry = entry
                target_block = block
                break
        if target_block is not None:
            break

    if target_block is None:
        # All recent results are dict-typed (or there are none).  Keep
        # the pending meta; the next send() with a string result will
        # carry it.
        self._log(
            "notification_meta_deferred",
            reason="no_str_tool_result",
        )
        return message

    # Strip notification prefix from ALL OTHER user ToolResultBlocks
    # (str-content only — dict-content can't carry our prefix).
    for entry in iface.entries:
        if entry.role != "user":
            continue
        for block in entry.content:
            if block is target_block:
                continue
            if isinstance(block, ToolResultBlock) and isinstance(
                block.content, str
            ):
                block.content = _strip_notification_prefix(block.content)

    # Strip-and-reinject on the target.
    cleaned = _strip_notification_prefix(target_block.content)
    target_block.content = notif_prefix + cleaned

    self._pending_notification_meta = None
    self._log(
        "notification_meta_injected",
        entry_id=target_entry.id,
    )
    return message
```

**Import needed** at the top of `__init__.py` (for type annotation inside `_inject_notification_meta`):

```python
from ..llm.interface import ToolCallBlock, ToolResultBlock
```

---

## 3. Heartbeat integration

**File:** `src/lingtai_kernel/base_agent/lifecycle.py`

### 3a. Add notification poll to `_heartbeat_loop`

After the signal-file detection block (after `.rules` check, ~line 287), before stamina enforcement (~line 290):

```python
        # --- notification sync ---
        # Poll .notification/ directory for changes.  If fingerprint
        # differs from last-seen, run the sync mechanism which injects
        # / strips the wire block based on current state.
        try:
            agent._sync_notifications()
        except Exception as notif_err:
            from ..logging import get_logger
            get_logger().warning(
                f"[{agent.agent_name}] notification sync failed: {notif_err}"
            )
```

This is a ~8-line addition inside the existing `while` loop.  The `_sync_notifications` method itself handles all state-variant logic (IDLE pair, ACTIVE stash, ASLEEP wake).

---

## 4. ACTIVE-state injection (request-send time)

**File:** `src/lingtai_kernel/session.py`

### 4a. Add notification meta injection in `SessionManager.send()`

After `self._health_check(message)` (~line 219) and before the actual send, add:

```python
        # ACTIVE-state notification injection: prepend notification JSON
        # to the most recent ToolResultBlock.  The agent's
        # _inject_notification_meta strips old prefixes and prepends the
        # current snapshot.  Only fires if _pending_notification_meta
        # is set (meaning the heartbeat detected a .notification/ change
        # while the agent was ACTIVE).
        # The agent reference is available via _build_system_prompt_fn's closure.
        # We access it through the logger_fn closure or a stored ref.
```

**However**, `SessionManager` deliberately has no reference to `BaseAgent` (per its docstring: "so it has no reference to BaseAgent").  The cleanest way to bridge this is to add a callback:

**Option: inject via a pre-send callback on SessionManager.**

Add a new optional parameter to `SessionManager.__init__`:

```python
    def __init__(
        self,
        *,
        # ... existing params ...
        notification_inject_fn: Callable[[Any], Any] | None = None,
    ):
        # ... existing body ...
        self._notification_inject_fn = notification_inject_fn
```

Then in `send()`, after `self._health_check(message)` and before the actual send:

```python
        # ACTIVE-state notification meta injection.
        if self._notification_inject_fn is not None:
            message = self._notification_inject_fn(message)
```

**File:** `src/lingtai_kernel/base_agent/__init__.py` — update `SessionManager` construction (~line 392):

```python
        self._session = SessionManager(
            llm_service=service,
            config=self._config,
            agent_name=agent_name,
            streaming=streaming,
            build_system_prompt_fn=self._build_system_prompt,
            build_tool_schemas_fn=self._build_tool_schemas,
            logger_fn=self._log,
            build_system_batches_fn=self._build_system_prompt_batches,
            notification_inject_fn=self._inject_notification_meta,
        )
```

---

## 5. IDLE-state sync

Covered by `_sync_notifications` (§2b).  When agent is IDLE and fingerprint changes:

1. Strip prior wire block (by `_notification_block_id`).
2. Call `_inject_notification_pair(notifications)` to append a synthetic `system(action="notification")` / `<JSON>` pair.
3. Post `MSG_TC_WAKE` to nudge the run loop.

The `_handle_tc_wake` path in `turn.py` is **unchanged** — it still drains `_tc_inbox` (which remains empty after Phase 2 migration) and sends.  After Phase 3, `_handle_tc_wake` can be simplified, but that's out of scope.

---

## 6. ASLEEP-state sync

Covered by `_sync_notifications` (§2b).  When agent is ASLEEP and fingerprint changes:

1. Transition `ASLEEP → IDLE` (clear `_asleep`, reset uptime).
2. Call `_inject_notification_pair(notifications)`.
3. Post `MSG_TC_WAKE` + `_wake_nap`.

If fingerprint hasn't changed during ASLEEP, no spurious wakes.

---

## 7. `system(action="notification")` intrinsic handler

**File:** `src/lingtai_kernel/intrinsics/system/__init__.py`

### 7a. Change the `notification` action from rejection to dispatch

Currently (~line 79):

```python
    if action == "notification":
        return {
            "status": "error",
            "message": (
                "system(action='notification', ...) is reserved for kernel-"
                "synthesized notifications and cannot be invoked directly. ..."
            ),
        }
```

After: change to allow voluntary calls (agent can query current state):

```python
    if action == "notification":
        from ...notifications import collect_notifications
        return collect_notifications(agent._working_dir)
```

### 7b. Remove `dismiss` from the handler dispatch

Remove `"dismiss": _dismiss` from the handler dict (~line 100).  Also remove the import of `_dismiss` from `notification.py` at the top of the file (~line 46).

**Decision:** the design doc says "no dismiss" (§2.4, §4.1).  However, `dismiss` is still referenced in the tool schema and i18n.  For Phase 1+2, keep `dismiss` in the schema as a no-op that returns `{"status": "ok", "note": "dismiss is deprecated — producers manage their own state"}`.  Full removal is Phase 3.

So actually: **keep `dismiss` in the handler dict** for now, but change its implementation:

**File:** `src/lingtai_kernel/intrinsics/system/notification.py`

```python
def _dismiss(agent, args: dict) -> dict:
    """Deprecated — producers manage their own state.

    Returns a deprecation notice.  Previously dismissed notifications
    by notif_id from both tc_inbox and the wire chat.  Under the new
    .notification/ filesystem model, producers clear their files when
    state changes; the agent never needs to dismiss.
    """
    return {
        "status": "ok",
        "note": "dismiss is deprecated — producers manage their own state. "
                "Notifications update automatically when producers change "
                "their .notification/ files.",
    }
```

---

## 8. Tool description update

**File:** `src/lingtai_kernel/intrinsics/system/schema.py`

### 8a. Add `notification` action to the schema enum

```python
    "action": {
        "type": "string",
        "enum": ["nap", "refresh", "sleep", "lull", "interrupt", "suspend",
                 "cpr", "clear", "nirvana", "presets", "dismiss", "notification"],
        "description": t(lang, "system_tool.action_description"),
    },
```

### 8b. Update i18n strings

The tool description needs the new contract text from §2.3 of the design doc.  This touches i18n resource files:

**Keys to add/update** (per language file):

- `system_tool.description` — append notification explanation.
- `system_tool.action_description` — add `notification` to the list of actions.
- `system_tool.notification_description` — new key for the `notification` action's detailed doc.

**English (`en`) i18n additions:**

```
system_tool.notification_description = \
    Returns the current state of all notification channels as a JSON object \
    keyed by source. Sources include `email`, `soul`, and any other producer \
    that has published to `.notification/`. Each source's value is structured \
    data the producer wrote. The kernel may synthesize this call on your behalf: \
    (1) when idle and notifications arrive, the kernel wakes you with this call \
    already made; (2) when mid-tool-chain, the next tool result you receive may \
    carry a `notifications:` JSON block prepended to its content — same data, \
    surfaced alongside the result. The data is replace-only: each source has one \
    current state, not a history of events. There is no dismiss action — you read \
    what's currently published, and the producer updates its file when its \
    situation changes.
```

---

## 9. Producer migration: email

**File:** `src/lingtai_kernel/base_agent/messaging.py`

### 9a. Rewrite `_rerender_unread_digest`

Replace the current implementation (~lines 45–115) that enqueues on `tc_inbox`:

```python
def _rerender_unread_digest(agent) -> str | None:
    """Write the current-unread digest to `.notification/email.json`.

    Computes the unread set, renders the digest prose as structured data,
    and publishes it atomically.  When count drops to 0, clears the file.

    Returns the path stem if published, or None if nothing unread.
    """
    from ..notifications import publish, clear
    from ..intrinsics.email.primitives import _render_unread_digest

    body, count, newest_ts = _render_unread_digest(agent)

    if count == 0:
        clear(agent._working_dir, "email")
        agent._log("email_notification_cleared")
        return None

    # Build structured payload (matching the design doc's §2.1.2 shape).
    data = _build_email_notification_payload(agent, count, newest_ts, body)
    publish(agent._working_dir, "email", data)

    agent._log(
        "email_notification_published",
        count=count,
        newest_received_at=newest_ts,
    )
    return "email"


def _build_email_notification_payload(agent, count: int, newest_ts: str | None, body: str) -> dict:
    """Build the structured JSON payload for .notification/email.json.

    Includes the suggested envelope fields (header, icon, priority,
    published_at) for frontend rendering, plus a ``data`` field with
    the agent-readable content.
    """
    from datetime import datetime, timezone

    return {
        "header": f"{count} unread email{'s' if count != 1 else ''}",
        "icon": "📧",
        "priority": "normal",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {
            "count": count,
            "newest_received_at": newest_ts,
            "digest": body,
        },
    }
```

### 9b. Simplify `_on_normal_mail`

The function (~lines 21–42) currently calls `_rerender_unread_digest` and posts `MSG_TC_WAKE` + `_wake_nap`.  The wake message stays — the **arrival side** still wants sub-second wake latency (heartbeat is ~1s).  But the producer no longer enqueues on `tc_inbox`; it just writes the file.  The wake nudge causes the heartbeat loop to spin once early, where `_sync_notifications` will detect the new fingerprint and inject the pair.

**After Phase 2 migration:**

```python
def _on_normal_mail(agent, payload: dict) -> None:
    """Handle a normal mail — rerender the unread digest notification file
    and nudge the heartbeat for sub-second sync latency.
    """
    address = payload.get("from", "unknown")
    subject = payload.get("subject") or "(no subject)"

    agent._log("mail_received", address=address, subject=subject,
               message=payload.get("message", ""))

    _rerender_unread_digest(agent)
    # Nudge the heartbeat so notification sync runs within ~1 tick instead
    # of waiting for the next periodic poll.  No MSG_TC_WAKE here — the
    # sync mechanism owns wake transitions; this just shortens latency.
    agent._wake_nap("mail_arrived")
```

The `_make_message` / `MSG_TC_WAKE` / `inbox.put` block from the old tc_inbox path is removed.  Wake is owned by `_sync_notifications` (which fires `MSG_TC_WAKE` on ASLEEP→IDLE transitions only).

---

## 10. Producer migration: soul flow

**File:** `src/lingtai_kernel/intrinsics/soul/flow.py`

### 10a. Rewrite the tail of `_run_consultation_fire`

The current tail (~lines 226–256) builds `InvoluntaryToolCall` and enqueues on `tc_inbox`.  After:

```python
        # --- Write soul notification file ---
        if not voices:
            # Nothing to say — clear the file if it exists.
            from ...notifications import clear
            clear(agent._working_dir, "soul")
            agent._log("consultation_fire_empty", fire_id=fire_id)
            return

        voices_for_pair = [_flatten_v3_for_pair(agent, v) for v in voices]
        # Build the notification payload.
        from ...notifications import publish
        from datetime import datetime, timezone
        soul_payload = _build_soul_notification_payload(voices_for_pair, fire_id)
        publish(agent._working_dir, "soul", soul_payload)

        voices_inline = [
            {"source": v.get("source", "unknown"), "voice": v.get("voice", "")}
            for v in voices_for_pair
            if v.get("voice")
        ]
        agent._log(
            "consultation_fire",
            fire_id=fire_id,
            count=len(voices),
            sources=sources,
            voices=voices_inline,
        )
        # Note: wake is handled by the notification sync mechanism.
        # No need to post MSG_TC_WAKE here.
```

### 10b. Add `_build_soul_notification_payload` helper

In `flow.py`, after `_flatten_v3_for_pair`:

```python
def _build_soul_notification_payload(voices_for_pair: list[dict], fire_id: str) -> dict:
    """Build the structured JSON payload for .notification/soul.json.

    Each voice is a dict with ``source``, ``voice``, ``thinking`` keys
    (the v2-compatible flatten from _flatten_v3_for_pair).
    """
    from datetime import datetime, timezone

    voices_data = []
    for v in voices_for_pair:
        entry = {"source": v.get("source", "unknown")}
        if v.get("voice"):
            entry["voice"] = v["voice"]
        if v.get("thinking"):
            entry["thinking"] = v["thinking"]
        voices_data.append(entry)

    return {
        "header": "soul flow",
        "icon": "🌊",
        "priority": "normal",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {
            "fire_id": fire_id,
            "voices": voices_data,
        },
    }
```

### 10c. Remove `InvoluntaryToolCall` import and `tc_inbox` enqueue

Delete the import of `InvoluntaryToolCall` (~line 168) and the enqueue block (~lines 228–235).  The `_make_message` / `MSG_TC_WAKE` / `inbox.put` block (~lines 250–256) is also removed — the notification sync handles wake.

---

## 11. Producer migration: system notifications

**File:** `src/lingtai_kernel/base_agent/messaging.py`

### 11a. Rewrite `_enqueue_system_notification`

Replace the current implementation (~lines 118–181) that enqueues on `tc_inbox`:

```python
def _enqueue_system_notification(agent, *, source: str, ref_id: str, body: str) -> str:
    """Publish a system notification to `.notification/system.json`.

    Multiplexes event types inside the single file.  Each call updates
    the file in place — the latest state is always what the agent sees.

    Args:
        agent: The agent instance.
        source: "email", "email.bounce", "daemon", "mcp.<name>", etc.
        ref_id: External reference (mail_id for email arrival, etc.).
        body: The localized prose for the agent to read.

    Returns:
        An identifier for the event (for logging; not a notif_id).
    """
    import json
    import threading
    import time as _time
    from datetime import datetime, timezone
    from ..notifications import publish, collect_notifications

    event_id = f"evt_{int(_time.time()*1000):x}"

    # The merge is read-modify-write on the agent's `system.json`.  A burst
    # of arrivals (e.g. 5 mail bounces in 100ms) would race without a guard.
    # Use a per-agent lock; the lock is created lazily so ``BaseAgent``
    # doesn't need a new attribute declaration.
    lock = getattr(agent, "_system_notification_lock", None)
    if lock is None:
        lock = threading.Lock()
        agent._system_notification_lock = lock

    with lock:
        # Read current system.json to merge.
        current = collect_notifications(agent._working_dir).get("system", {})
        events = current.get("data", {}).get("events", [])

        events.append({
            "event_id": event_id,
            "source": source,
            "ref_id": ref_id,
            "body": body,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        # Cap at 20 most recent events to prevent unbounded growth.
        events = events[-20:]

        payload = {
            "header": f"{len(events)} system notification{'s' if len(events) != 1 else ''}",
            "icon": "🔔",
            "priority": "normal",
            "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": {
                "events": events,
            },
        }
        publish(agent._working_dir, "system", payload)

    agent._log(
        "system_notification_published",
        event_id=event_id,
        source=source,
        ref_id=ref_id,
    )
    return event_id
```

Note: only `system.json` needs read-modify-write (its data accumulates events).  The `email.json` and `soul.json` producers compute their full state from agent state on every publish — no merge needed, so no lock.

### 11b. Remove `tc_inbox` references from `_rerender_unread_digest` tail

Already covered in §9a — the `_make_message` / `MSG_TC_WAKE` / `inbox.put` block is removed.

---

## 12. Molt clearing

**File:** `src/lingtai_kernel/intrinsics/psyche/_molt.py`

### 12a. Add `.notification/` clearing alongside tc_inbox drain

In `_context_molt`, after the tc_inbox drain (~line 198):

```python
    # Clear notification files — post-molt state has no stale notifications.
    # The next heartbeat tick will re-sync whatever producers publish anew.
    import shutil
    notif_dir = agent._working_dir / ".notification"
    if notif_dir.is_dir():
        try:
            shutil.rmtree(notif_dir)
        except OSError:
            pass
    # Reset notification fingerprint so the next tick sees a clean slate.
    agent._notification_fp = ()
    agent._notification_block_id = None
    if hasattr(agent, "_pending_notification_meta"):
        agent._pending_notification_meta = None
```

### 12b. Same change in `context_forget`

After the tc_inbox drain (~line 374):

```python
    # Clear notification files.
    import shutil
    notif_dir = agent._working_dir / ".notification"
    if notif_dir.is_dir():
        try:
            shutil.rmtree(notif_dir)
        except OSError:
            pass
    agent._notification_fp = ()
    agent._notification_block_id = None
    if hasattr(agent, "_pending_notification_meta"):
        agent._pending_notification_meta = None
```

---

## 13. Test matrix

**File:** `tests/test_notification_sync.py` (new)

Covers §7.1 step 7 of the design doc.

### 13.1 Fingerprint + collection primitives

| Test | What it asserts |
|------|----------------|
| `test_fingerprint_empty_dir` | `notification_fingerprint(workdir)` returns `()` when `.notification/` doesn't exist. |
| `test_fingerprint_with_files` | Returns sorted `(name, mtime_ns, size)` triples matching the files on disk. |
| `test_fingerprint_mtime_ns_granularity` | Two writes to the same file within one second produce different fingerprints (mtime_ns vs mtime). |
| `test_collect_empty_dir` | `collect_notifications(workdir)` returns `{}`. |
| `test_collect_mixed_files` | Reads all `.json` files, keys by stem, skips non-`.json` files. |
| `test_collect_malformed_json` | Skips malformed files silently, returns what it can parse. |
| `test_publish_creates_dir` | `publish(workdir, "test", {...})` creates `.notification/` if missing. |
| `test_publish_atomic` | After `publish`, file exists and is well-formed JSON; `.tmp` file does not exist. |
| `test_clear_idempotent` | `clear(workdir, "test")` on a non-existent file doesn't raise. |
| `test_concurrent_publish_atomicity` | 10 threads × 100 iterations each, each thread publishes to its own source.  Every `collect_notifications()` snapshot returns parseable JSON for every source — never a partial-write read.  Asserts no `.tmp` file remains at end. |
| `test_system_notification_lock` | 10 threads concurrently call `_enqueue_system_notification`.  Final `system.json` contains all 10 events (no lost writes from RMW race).  Event ids are unique. |

### 13.2 IDLE-state sync

| Test | What it asserts |
|------|----------------|
| `test_idle_sync_injects_pair` | Agent is IDLE; write `.notification/email.json`; call `_sync_notifications`; wire has a `(system(notification), result)` pair appended. `_notification_block_id` is set. |
| `test_idle_sync_strips_prior_pair` | Agent is IDLE with a prior notification pair in the wire; write new content; call sync; old pair removed, new pair injected. |
| `test_idle_sync_empty_removes_pair` | Agent is IDLE with a prior pair; delete all `.notification/` files; call sync; pair is stripped, `_notification_block_id` is `None`. |
| `test_idle_sync_no_change_noop` | Call `_sync_notifications` twice without changing files; second call is a no-op (fingerprint matches). |
| `test_idle_sync_posts_wake` | After inject, `MSG_TC_WAKE` is in the agent's inbox. |

### 13.3 ACTIVE-state sync

| Test | What it asserts |
|------|----------------|
| `test_active_sync_stashes_meta` | Agent is ACTIVE (has a tool chain in progress); heartbeat detects fingerprint change; `_pending_notification_meta` is set (not `None`). |
| `test_active_inject_prepends_to_latest_result` | After stash, call `_inject_notification_meta(message)` with a mock interface containing two user entries with ToolResultBlocks; the most recent result's content starts with `notifications:\n{...}`. |
| `test_active_inject_strips_older_prefixes` | The older ToolResultBlock's content has the notification prefix stripped. |
| `test_active_no_tool_result_defers` | If the most recent assistant entry is text-only (no tool_calls), `_pending_notification_meta` remains set but no injection happens. It fires on the next tool result. |

### 13.4 ASLEEP-state sync

| Test | What it asserts |
|------|----------------|
| `test_asleep_sync_wakes` | Agent is ASLEEP; write `.notification/email.json`; call `_sync_notifications`; agent state is now IDLE, wire has a notification pair, MSG_TC_WAKE is in inbox. |
| `test_asleep_no_change_stays_asleep` | Agent is ASLEEP; no files written; call `_sync_notifications`; agent stays ASLEEP, no pair in wire. |

### 13.5 `system(action="notification")` voluntary call

| Test | What it asserts |
|------|----------------|
| `test_notification_action_returns_collect` | Voluntary `system(action="notification")` returns the `collect_notifications()` dict. Empty dir returns `{}`. |
| `test_notification_action_with_files` | With `.notification/email.json` and `.notification/soul.json` on disk, returns `{"email": {...}, "soul": {...}}`. |

### 13.6 Producer migrations

| Test | What it asserts |
|------|----------------|
| `test_email_publish_writes_file` | `_rerender_unread_digest` with 3 unread messages writes `.notification/email.json` with count=3. |
| `test_email_clear_on_zero` | `_rerender_unread_digest` with 0 unread deletes the file. |
| `test_soul_publish_writes_file` | Mock consultation fire with voices writes `.notification/soul.json`. |
| `test_soul_clear_on_empty` | Consultation fire with no voices deletes `.notification/soul.json`. |
| `test_system_publish_merges_events` | Two `_enqueue_system_notification` calls produce a single `.notification/system.json` with 2 events. |
| `test_system_publish_caps_at_20` | 25 sequential calls produce a file with only the 20 most recent events. |

### 13.7 Molt clearing

| Test | What it asserts |
|------|----------------|
| `test_molt_clears_notification_dir` | After `_context_molt`, `.notification/` directory is gone. `_notification_fp` is `()`. |
| `test_context_forget_clears_notification_dir` | Same for system-initiated molt. |

---

## 14. Open questions for human

### Q1: ACTIVE-state injection placement — `SessionManager.send()` vs adapter hooks

The design doc says ACTIVE injection happens "at request-send time, not per-result-block construction."  The natural place is `SessionManager.send()`, which is the single chokepoint before every API call.  However, `SessionManager` was deliberately designed to have no reference to `BaseAgent` (see `session.py` docstring).

**Proposed solution:** Add an optional `notification_inject_fn` callback to `SessionManager.__init__`, called from `send()`.  This preserves the SessionManager's agent-agnostic architecture while providing the hook point.

**Alternative considered:** Installing the injection as a second `pre_request_hook` callback.  Rejected — this would couple the new mechanism to the old hook infrastructure we're trying to remove in Phase 3.

**Question:** Is the callback approach acceptable, or should we take a different path?

### Q2: `_pending_mail_notifications` already removed?

The comment at `base_agent/__init__.py:355` says `_pending_mail_notifications removed`.  Grep confirms no active references in the email intrinsic.  **The design doc's step 13 ("Delete `_pending_mail_notifications` from mail intrinsic") appears to already be done.**  Confirm this is the case and we can skip step 13.

### Q3: `pre_request_hook` — actual adapter usage?

Grep shows `pre_request_hook` is only defined on `ChatSession` (`llm/base.py:143`) and set by `BaseAgent._install_drain_hook` (`__init__.py:697`).  No adapter `.py` files in `llm/` actually call `pre_request_hook`.  This suggests the hook fires through a shared send wrapper (likely `send_with_timeout` in `llm_utils.py`) rather than each adapter independently.

**Question:** Can you confirm the hook call site?  If it's in `llm_utils.py:send_with_timeout`, Phase 3 cleanup is simpler (one callsite, not four adapter files).  This doesn't affect Phase 1+2 but changes the Phase 3 estimate.

### Q4: Notification meta prefix format

The design doc says `notifications:\n<json>\n\n` prepended to `ToolResultBlock.content`.  This mutates the canonical interface entry that adapters serialize.  Two concerns:

1. **Interface mutation vs. adapter serialization.** Adapters like Anthropic's `to_anthropic_messages` walk `entry.content` blocks.  Prepending text to `ToolResultBlock.content` (which is `str | dict`) works if content is a string, but if it's already a dict (e.g. structured tool result from some MCP), the prefix would corrupt it.

2. **Persistence.** The prefixed content gets saved to `chat_history.jsonl`.  When the agent restores from disk, the old notification prefix is in the history.  This is actually fine — the notification was current when saved — but worth documenting.

**Question:** Should we restrict ACTIVE-state meta injection to `ToolResultBlock`s whose `content` is a `str`?  (If content is a dict, skip that block and try the next one.)

### Q5: `_inject_notification_pair` — entry-id tracking uses `call_block.id` (string) vs `entry.id` (int)

`_notification_block_id` is typed as `int | None` for the wire's `InterfaceEntry.id`, but `remove_pair_by_call_id` takes a `str` (the `ToolCallBlock.id`).  These are different types.  We need to decide: track by `InterfaceEntry.id` (int) and use a new `remove_entry_by_id` helper, or track by `ToolCallBlock.id` (str) and use the existing `remove_pair_by_call_id`.

**Proposed:** Track by `ToolCallBlock.id` (str) — it's what `remove_pair_by_call_id` already expects, and it's unique per synthetic pair.  Change `_notification_block_id: int | None` to `_notification_block_id: str | None`.

### Q6: Interaction with existing `_tc_inbox` drain during Phase 1+2

During the migration window (Phase 2), both mechanisms coexist.  After Phase 2, no producer enqueues on `tc_inbox`, so `_drain_tc_inbox` and `_handle_tc_wake` effectively no-op.  But `_install_drain_hook` still sets `pre_request_hook` every turn.

**Question:** Should we disable `_install_drain_hook` as part of Phase 2 (since nothing enqueues), or leave it running as a no-op until Phase 3 removes it entirely?  Disabling early saves a few microseconds per turn but adds a code change that's later reversed by Phase 3.

---

## Appendix: Diff summary

### New files
- `src/lingtai_kernel/notifications.py` — ~60 lines
- `tests/test_notification_sync.py` — ~200 lines

### Modified files
- `src/lingtai_kernel/base_agent/__init__.py` — +80 lines (new attrs, `_sync_notifications`, `_inject_notification_pair`, `_inject_notification_meta`, `notification_inject_fn` callback)
- `src/lingtai_kernel/base_agent/lifecycle.py` — +8 lines (notification poll in heartbeat loop)
- `src/lingtai_kernel/base_agent/messaging.py` — rewrite `_rerender_unread_digest` (~30 lines changed), rewrite `_enqueue_system_notification` (~30 lines changed), simplify `_on_normal_mail` (~5 lines removed)
- `src/lingtai_kernel/session.py` — +8 lines (`notification_inject_fn` callback in `__init__` + call in `send()`)
- `src/lingtai_kernel/intrinsics/system/__init__.py` — +5 lines (notification action handler)
- `src/lingtai_kernel/intrinsics/system/notification.py` — rewrite `_dismiss` (10 lines changed)
- `src/lingtai_kernel/intrinsics/system/schema.py` — +1 line (add `notification` to enum)
- `src/lingtai_kernel/intrinsics/soul/flow.py` — rewrite tail of `_run_consultation_fire` (~20 lines changed), add `_build_soul_notification_payload` (~20 lines)
- `src/lingtai_kernel/intrinsics/psyche/_molt.py` — +12 lines (notification dir clearing × 2 functions)

### Not modified (still functional, awaiting Phase 3)
- `src/lingtai_kernel/tc_inbox.py` — kept as-is, never enqueued after Phase 2
- `src/lingtai_kernel/llm/base.py` — `pre_request_hook` attribute kept
- `src/lingtai_kernel/base_agent/__init__.py` — `_tc_inbox`, `_drain_tc_inbox`, `_install_drain_hook`, `_drain_tc_inbox_for_hook`, `_appendix_ids_by_source` all kept
- All adapter files — no changes

### Estimated implementation effort

**~2–3 days of focused work:**
- Day 1: `notifications.py` module + BaseAgent sync logic + heartbeat integration + tests for primitives and IDLE sync.
- Day 2: ACTIVE-state injection (SessionManager callback) + ASLEEP wake + system intrinsic handler + tests for ACTIVE/ASLEEP/notification action.
- Day 3: Producer migrations (email, soul, system) + molt clearing + remaining tests + i18n updates.
