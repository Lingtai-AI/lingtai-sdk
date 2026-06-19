# Patch: email unread-digest notification (replace per-arrival pairs)

## Summary

Replace the current per-arrival `system(action="notification")` mail notification model with a single coalescing **`email(action="unread")`** digest pair that always reflects the most recent arrival event. Triggered **only by mail arrival** — reads, archives, and deletes do **not** rerender. The notification is kernel-synthesized, replace-in-history, single-slot per source key (`"email.unread"`).

This mirrors the soul-flow voice pattern (`coalesce=True, replace_in_history=True`) and lets us delete the entire dismiss machinery — which was load-bearing only because old notifications used to linger as separate per-arrival pairs.

## Motivation

### What the wire looks like today

Every mail arrival enqueues its own `system(action="notification", source="email", ref_id=<mail_id>)` pair. On a busy mailbox, the wire grows linearly with arrivals; the only way to remove a stale pair is `email(action="read")` auto-dismissing it via `system._dismiss`. Per-mail bookkeeping (`_pending_mail_notifications: dict[ref_id → notif_id]`) exists solely to support that selective removal.

### What the wire looks like after this patch

At most one `email(action="unread")` pair in the wire at any time — the digest of *currently unread* mail as of the **latest arrival**. New arrival → digest re-rendered, prior pair removed via `replace_in_history=True`. Reads/archives/deletes don't touch the wire at all (the pair stays, possibly stale until the next arrival rerenders).

### Why arrival-only triggers (not read-triggered rerender)

User chose this explicitly: "we keep the notification until next notification triggered." The notification is a **what-arrived snapshot**, not a live unread mirror. Stale state in the wire is acceptable — the agent's actions on the mailbox don't echo back as wire mutations. Three benefits:

1. **No wire churn from agent-initiated actions.** The agent's read/archive/delete calls remain self-contained tool round-trips; they don't trigger a kernel-synthesized rerender that the agent didn't ask for.
2. **Cleaner producer surface.** Only one trigger point (`_on_normal_mail`); no instrumentation of read/archive/delete.
3. **Token cost is bounded by arrival rate, not by inbox size.** Unread-set size still bounds digest length, but the rerender cadence matches arrival cadence.

The cost: when the agent reads the only unread mail, the wire still contains the pre-read digest until the next arrival. The agent shouldn't re-act on it because the standard mail-tool prompt tells them to call `email(action="check")` if they want a fresh view. Documented inline.

## Design

### Wire shape

```python
ToolCallBlock(
    id=<call_id>,
    name="email",                          # was "system"
    args={
        "action": "unread",                # was "notification"
        "count": <int>,                    # current unread count at arrival time
        "received_at": <iso8601>,
    },
)
ToolResultBlock(
    id=<call_id>,
    name="email",                          # was "system"
    content=<rendered digest prose>,
)
```

The args **deliberately omit** `notif_id`, `ref_id`, `source` — those existed to support per-pair dismiss, which is gone. `count` is informational; `received_at` marks the arrival that produced this snapshot.

### Digest prose (rendered into `content`)

i18n key: `email.unread_digest`. Rendered by `intrinsics/email/primitives.py:_render_unread_digest(agent, unread_messages)`.

Format (en):
```
[email] {count} unread message(s) — most recent {recency}.

  1. From {addr1} — {subject1}
     Sent at: {sent_at1}
     {preview1}

  2. From {addr2} — {subject2}
     ...

(showing first {N_shown} of {N_total})    ← only if N_total > N_shown
```

- **Cap:** `N_shown = 10` newest-first.
- **Per-entry preview:** 200 chars, `\n` → space, suffixed `"... (K more chars)"` when truncated.
- **`recency`:** veiled timestamp of newest unread (uses existing `time_veil.veil()`).
- **Empty unread set is unreachable here** — rerender only fires *after* a new arrival, so by definition `count >= 1`.

### Trigger surface

Exactly one trigger: `base_agent/messaging.py:_on_normal_mail` (called by `services/mail.py` on every mailbox arrival). The function calls `_rerender_unread_digest(agent)` instead of the current `_enqueue_system_notification(...)` call.

### Coalesce / replace semantics

```python
InvoluntaryToolCall(
    call=<call_block>,
    result=<result_block>,
    source="email.unread",
    enqueued_at=time.time(),
    coalesce=True,            # in-queue: replaces prior queued unread digest
    replace_in_history=True,  # at drain: removes prior unread pair from wire
)
```

This piggybacks on existing `tc_inbox` infrastructure. `appendix_tracker` (already on `BaseAgent`) tracks `source → call_id` for `replace_in_history` lookup. No new infrastructure needed.

## What gets deleted

Because the wire never accumulates per-mail notifications, the entire dismiss machinery for email goes away:

1. **`_pending_mail_notifications: dict[str, str]`** on `BaseAgent` (`base_agent/__init__.py:356`). Used only to thread `mail_id → notif_id` for `email.read`'s auto-dismiss.
2. **Auto-dismiss block in `email/manager.py:_read`** (`manager.py:780-799`). The 20-line block that pops `_pending_mail_notifications`, builds `dismissed_notif_ids`, calls `_system._dismiss(...)`, logs `system_notification_auto_dismissed`. **Delete entirely.**
3. **`system.notification` action rejection** (`intrinsics/system/__init__.py:79-88`). Stays — `system(action="notification")` is still kernel-synthesized for non-email producers (soul, daemon, future MCP).
4. **`system(action="dismiss")` and `_dismiss` impl** (`intrinsics/system/notification.py:9-73`). **Stays** — soul-flow / daemon / MCP notifications may still need it. Out of scope.
5. **Bounce notification** (`intrinsics/email/primitives.py:280` enqueueing `source="email.bounce"`). Decision: keep as-is on `system(action="notification")`. Bounces are infrequent, distinct events (failure reports, not arrival snapshots) — they don't fit the "current unread" digest model. They remain dismissable via `system(action="dismiss")`. **Out of scope for this patch.**

## File-by-file changes

### 1. `src/lingtai_kernel/intrinsics/email/primitives.py`

Add `_render_unread_digest(agent)` helper near the bottom of the existing display helpers section (around `_message_summary`):

```python
def _render_unread_digest(agent, *, max_entries: int = 10, preview_chars: int = 200) -> tuple[str, int, str | None]:
    """Compute and render the current unread mail digest.

    Returns ``(body, count, newest_received_at)``:
      - ``body`` is the rendered prose for the ToolResultBlock.
      - ``count`` is total unread count (may exceed ``max_entries``).
      - ``newest_received_at`` is the ISO timestamp of the most recent
        unread message, or None if count == 0.

    Caller uses ``count`` to short-circuit (don't enqueue when 0) and
    ``newest_received_at`` for the call_block args.
    """
    from ..i18n import t as _t
    from ..time_veil import veil

    read_ids = _read_ids(agent)
    inbox = _list_inbox(agent)  # already newest-first per existing semantics
    unread = [m for m in inbox if m.get("id") not in read_ids]
    count = len(unread)
    if count == 0:
        return ("", 0, None)

    shown = unread[:max_entries]
    newest = shown[0]
    newest_ts = newest.get("received_at") or newest.get("sent_at") or ""

    lang = agent._config.language
    lines = []
    for i, m in enumerate(shown, start=1):
        addr = m.get("from", "unknown")
        identity = m.get("identity") or {}
        name = identity.get("agent_name") or addr
        subj_raw = m.get("subject")
        subject = subj_raw if subj_raw else _t(lang, "email.unread_digest.no_subject")
        ts = m.get("sent_at") or m.get("time") or m.get("received_at") or ""
        sent_at = veil(agent, ts)
        body = m.get("message", "")
        if len(body) > preview_chars:
            preview = body[:preview_chars].replace("\n", " ") + f"... ({len(body) - preview_chars} more chars)"
        else:
            preview = body.replace("\n", " ")
        lines.append(_t(
            lang, "email.unread_digest.entry",
            n=i, address=addr, name=name, subject=subject,
            sent_at=sent_at, preview=preview,
        ))

    more_line = ""
    if count > max_entries:
        more_line = _t(lang, "email.unread_digest.more", shown=max_entries, total=count)

    body = _t(
        lang, "email.unread_digest",
        count=count,
        recency=veil(agent, newest_ts),
        entries="\n".join(lines),
        more=more_line,
        tool=getattr(agent, "_mailbox_tool", "email"),
    )
    return (body, count, newest_ts)
```

Notes:
- Uses **existing** `_list_inbox` (already returns newest-first) and `_read_ids`. No new I/O primitives.
- Per-entry rendering reuses the same fallback chain as `_on_normal_mail` (`sent_at | time | received_at`) and the same `time_veil.veil()` time-blindness gate.
- `_t(...)` for `email.unread_digest.no_subject` — same fallback as `system.new_mail.no_subject`.

### 2. `src/lingtai_kernel/base_agent/messaging.py`

#### 2a. Replace `_on_normal_mail` body (lines 21-78)

Strip the per-arrival prose rendering and the `_enqueue_system_notification` call. Replace with a thin call to `_rerender_unread_digest(agent)` (new helper, see 2b). Keep `agent._wake_nap("mail_arrived")` and the `mail_received` log line.

```python
def _on_normal_mail(agent, payload: dict) -> None:
    """Handle a normal mail — rerender the unread digest in the wire chat.

    The message is already persisted to mailbox/inbox/ by MailService.
    Mail arrival triggers a single splice of an ``email(action="unread")``
    digest pair (replacing any prior pair for source="email.unread").
    Reads, archives, and deletes do NOT trigger a rerender — the wire
    notification is a snapshot of what was unread at the latest arrival,
    not a live unread mirror. Stale-after-read is acceptable; the agent
    can call ``email(action="check")`` for a fresh view.

    Capabilities still set ``_mailbox_name`` / ``_mailbox_tool`` for
    digest rendering.
    """
    address = payload.get("from", "unknown")
    subject = payload.get("subject") or "(no subject)"

    agent._wake_nap("mail_arrived")
    agent._log("mail_received", address=address, subject=subject,
               message=payload.get("message", ""))

    _rerender_unread_digest(agent)
```

#### 2b. Replace `_enqueue_system_notification` with `_rerender_unread_digest`

The old helper is still used by `intrinsics/email/primitives.py` for bounce notifications, so **don't delete it** — keep it for bounce + future non-email producers. Add a new helper for the unread digest:

```python
def _rerender_unread_digest(agent) -> str | None:
    """Splice the current-unread digest into the wire chat.

    Computes the unread set, renders the digest prose, builds a synthetic
    ``email(action="unread")`` tool-call pair, and enqueues it on
    ``tc_inbox`` with ``coalesce=True, replace_in_history=True`` and
    ``source="email.unread"``. The drain replaces any prior digest pair
    in the wire with this one.

    Returns the call_id of the enqueued pair, or None if there's nothing
    unread (no enqueue happens — caller's responsibility to know whether
    that means "leave prior digest stale" or "explicitly clear it").

    The current trigger point (``_on_normal_mail``) only fires after a
    mail has been persisted to the inbox, so by construction count >= 1
    when this is called from arrival. The ``count == 0`` short-circuit
    is defensive for future non-arrival callers.
    """
    import secrets
    from datetime import datetime, timezone
    from ..llm.interface import ToolCallBlock, ToolResultBlock
    from ..tc_inbox import InvoluntaryToolCall
    from ..intrinsics.email.primitives import _render_unread_digest

    body, count, newest_ts = _render_unread_digest(agent)
    if count == 0:
        return None

    call_id = f"un_{int(time.time()*1000):x}_{secrets.token_hex(2)}"
    received_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    call = ToolCallBlock(
        id=call_id,
        name="email",
        args={
            "action": "unread",
            "count": count,
            "received_at": received_at,
        },
    )
    result = ToolResultBlock(id=call_id, name="email", content=body)
    item = InvoluntaryToolCall(
        call=call,
        result=result,
        source="email.unread",
        enqueued_at=time.time(),
        coalesce=True,
        replace_in_history=True,
    )
    agent._tc_inbox.enqueue(item)

    agent._log(
        "email_unread_digest_enqueued",
        call_id=call_id,
        count=count,
        newest_received_at=newest_ts,
    )
    return call_id
```

### 3. `src/lingtai_kernel/base_agent/__init__.py`

#### 3a. Remove `_pending_mail_notifications` initialization (line 356)

```python
# DELETE:
self._pending_mail_notifications: dict[str, str] = {}
```

The dict is unused after this patch. Email arrivals don't track per-notif IDs anymore (no per-arrival pair to address); reads don't dismiss anything.

#### 3b. Keep `_enqueue_system_notification` pass-through (line 579-581)

Unchanged. Still used by bounce + future producers.

### 4. `src/lingtai_kernel/intrinsics/email/manager.py`

#### 4a. Delete the auto-dismiss block in `_read` (lines 780-799)

```python
# DELETE the entire block:
# Auto-dismiss any pending notifications for mails we just read.
dismissed_notif_ids: list[str] = []
for mail_id in matched_ids:
    notif_id = self._agent._pending_mail_notifications.pop(mail_id, None)
    if notif_id is not None:
        dismissed_notif_ids.append(notif_id)
if dismissed_notif_ids:
    from .. import system as _system
    _system._dismiss(
        self._agent,
        {"ids": dismissed_notif_ids, "_invoked_by": "email.read"},
    )
    for notif_id in dismissed_notif_ids:
        self._agent._log(
            "system_notification_auto_dismissed",
            notif_id=notif_id,
            invoked_by="email.read",
        )
```

(Exact line range to verify against current file — comment markers preserved.)

### 5. i18n catalogs

Add three new keys in `src/lingtai_kernel/i18n/{en,zh,wen}.json`:

#### 5a. `email.unread_digest`
```
en: "[email] {count} unread message(s) — most recent {recency}.\n\n{entries}\n{more}"
zh: "[邮件] 共有 {count} 封未读消息——最近一封于 {recency}。\n\n{entries}\n{more}"
wen: "[邮] 未阅之书凡 {count} 封，新者发于 {recency}。\n\n{entries}\n{more}"
```

#### 5b. `email.unread_digest.entry`
```
en: "  {n}. From {address}{name_suffix} — {subject}\n     Sent at: {sent_at}\n     {preview}"
zh: "  {n}. 来自 {address}{name_suffix} —— {subject}\n     发送时间：{sent_at}\n     {preview}"
wen: "  {n}. 自 {address}{name_suffix} —— {subject}\n     发于：{sent_at}\n     {preview}"
```
(See note below on `name_suffix` — simpler to inline `name` directly; pick one approach in implementation.)

#### 5c. `email.unread_digest.more`
```
en: "(showing first {shown} of {total})"
zh: "（仅显示前 {shown} 封，共 {total} 封）"
wen: "（仅显前 {shown} 封，共 {total}）"
```

#### 5d. `email.unread_digest.no_subject`
```
en: "(no subject)"
zh: "（无主题）"
wen: "（无题）"
```

#### 5e. (Optional) Mark `system.new_mail` as deprecated

The `system.new_mail` and `system.new_mail.no_subject` keys are now unused on the email-arrival path. **Do not delete them** — `lingtai/core/mcp/inbox.py` and any addon code may still reference `[system]`-style notification rendering. Leave them, optionally add a comment or move to a `_deprecated` section in a follow-up.

### 6. Tests

#### 6a. New test file: `tests/test_email_unread_digest.py`

Cover:

1. **Single arrival** → one `email.unread` pair in wire, `name="email"`, `args.action="unread"`, `args.count=1`, body contains sender + subject + preview.
2. **Two arrivals back-to-back** (mid-turn or sequential) → still exactly one pair in wire after drain. Content reflects both unread mails (count=2, both listed).
3. **Three arrivals** → after drain, exactly one pair, count=3.
4. **N+1 arrivals where N > max_entries** → digest body contains "showing first 10 of N" line, only 10 entries listed.
5. **Read after arrival** → wire still contains the original digest pair (NOT mutated by read). count remains the pre-read count.
6. **Read after arrival, then new arrival** → after the new arrival's drain, the pair reflects the new unread set (1 unread, since the prior was read).
7. **Mark-read of all unread, then new arrival** → digest pair reflects only the new arrival (count=1).
8. **Coalesce in queue (two arrivals before drain)** → only one pair spliced, content reflects both.
9. **Time-blind agent** → `recency` and per-entry `sent_at` blank.
10. **Truncated long preview** → preview suffixed with `"... (K more chars)"`, where K is correct.
11. **Empty subject** → `email.unread_digest.no_subject` placeholder used.
12. **`_pending_mail_notifications` no longer referenced anywhere** (grep-style assertion in test, or just delete tests that referenced it).

#### 6b. Tests to delete or update

- Any test asserting `system(action="notification")` on a mail arrival → update to assert `email(action="unread")`.
- Any test exercising `email.read`'s auto-dismiss path → delete (auto-dismiss is gone).
- Any test that builds `_pending_mail_notifications` fixture → delete.

Search for offending tests:
```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
grep -rn "system_notification\|_pending_mail_notifications\|action=\"notification\".*source=\"email\"" tests/ 2>/dev/null
```

### 7. ANATOMY.md updates (must ship in same commit)

#### 7a. `src/lingtai_kernel/ANATOMY.md`

In "Producers in the kernel today" table (around line 100):
- **Replace** `Mail arrival | system.notification:<notif_id> | base_agent/messaging.py:_on_normal_mail`
- **With** `Mail arrival | email.unread (coalesce + replace) | base_agent/messaging.py:_on_normal_mail`
- **Keep** `Mail bounce | system.notification:<notif_id> | intrinsics/email/primitives.py:280`
- **Keep** `Soul flow | soul.flow | intrinsics/soul/flow.py:216 (uses coalesce+replace_in_history)`

In "Involuntary tool-call pairs" section: add a paragraph noting that the email-arrival path uses the soul-flow-style replace-in-history pattern (single slot, always reflects latest arrival snapshot, no dismiss path).

#### 7b. `src/lingtai_kernel/intrinsics/email/ANATOMY.md`

- **Notification format** section (lines 49-92): rewrite. The wire shape is now `email(action="unread")` not `system(action="notification")`. Document: single-slot, replace-on-arrival, body is digest of current unread, list digest fields and i18n keys.
- **Outbound (notification producer)** line (line 37): rewrite to point at `_rerender_unread_digest` and note that bounce notifications still use `_enqueue_system_notification` with `source="email.bounce"`.
- **Outbound (system dismiss)** line (line 36): **delete entirely** — `_read()` no longer calls `system._dismiss`.
- **Key invariants** section: delete the `_read auto-dismisses pending system notification pairs` bullet.

#### 7c. `src/lingtai_kernel/intrinsics/system/ANATOMY.md`

Update the `_dismiss` cross-module note (around line 45): `email/manager.py` no longer calls `_dismiss` on read. The remaining callers are the agent itself (voluntary `system(action="dismiss")`) and bounce notification handlers. Reword to drop the email/manager.py reference.

#### 7d. `src/lingtai_kernel/intrinsics/ANATOMY.md`

Around line 49 (the `notification` action explanation): keep the existing text — `system(action="notification")` is still kernel-synthesized for soul / daemon / bounce / MCP. The change here is only that *email arrival* no longer uses that surface; it uses `email(action="unread")` instead. Add a one-line clarification.

#### 7e. `src/lingtai_kernel/base_agent/ANATOMY.md`

The `messaging.py` description (line 17 area) currently says:

> `_enqueue_system_notification` … *kernel-wide producer hook for surfacing out-of-band events as synthetic tool-call pairs; called by mail, soul, and any new daemon/MCP/scheduler producer*

Update to: *called by mail bounce, soul, and any new daemon/MCP/scheduler producer*. Add a sibling line for `_rerender_unread_digest` — the email-arrival-specific producer that uses replace-in-history single-slot semantics.

## Out of scope (explicit)

These are NOT touched by this patch — list them so reviewers don't expect them:

1. **Bounce notifications** — still surface as `system(action="notification", source="email.bounce")`. They're infrequent, semantically distinct (failures, not arrivals), and don't fit the unread-digest model.
2. **MCP inbox notifications** (`lingtai/core/mcp/inbox.py`) — still surface as `system(action="notification")`. Per-MCP tool-name routing is a separate per-addon patch.
3. **`system(action="dismiss")`** — stays. Used by agents for bounce / soul-consultation / MCP notifications.
4. **Soul-flow voice / consultation** — unchanged.
5. **Email tool surface** (`email.send`, `email.check`, `email.read`, etc.) — unchanged. The `unread` action does NOT appear in `email/schema.py`'s action enum because it's kernel-synthesized only (same pattern as `system.notification`). If we want belt-and-suspenders, add a rejection guard in `email/__init__.py:handle()` mirroring `system/__init__.py:79-88`. Recommended.

### 7f. Email tool dispatch guard (recommended)

Add to `src/lingtai_kernel/intrinsics/email/__init__.py:handle()`:

```python
def handle(agent, args: dict) -> dict:
    action = args.get("action")
    if action == "unread":
        return {
            "status": "error",
            "message": (
                "email(action='unread', ...) is reserved for kernel-"
                "synthesized unread-mail digests and cannot be invoked "
                "directly. Use email(action='check') to view your inbox."
            ),
        }
    mgr = getattr(agent, "_email_manager", None)
    ...
```

## Verification

After applying:

```bash
cd /Users/huangzesen/Documents/GitHub/lingtai-kernel
python -c "import lingtai_kernel.intrinsics.email; import lingtai_kernel.base_agent.messaging"
python -m pytest tests/test_email_unread_digest.py -xvs
python -m pytest tests/ -x  # full suite (modulo pre-existing failures)
```

Manual smoke test (TUI):

1. Start an agent via TUI, send it a mail.
2. Inspect `chat_history.jsonl` — confirm one `email(action="unread")` pair.
3. Send another mail (without agent reading the first) — confirm still one pair, count=2.
4. Have agent run `email(action="read", ...)` — confirm pair NOT mutated.
5. Send a third mail — confirm pair now reflects only the *unread* tail (count=2 if read'd one of the prior, count=3 if didn't).

## Commit message (suggested)

```
feat(email): replace per-arrival notifications with single unread-digest pair

Email arrivals now surface as a single coalescing email(action="unread")
tool-call pair in the wire, replacing the per-arrival
system(action="notification") model. Pattern mirrors soul-flow:
coalesce=True, replace_in_history=True, source="email.unread".

The pair contains a digest of all currently-unread mail (newest-first,
capped at 10 entries × 200 char preview each). Triggered only by mail
arrival — reads/archives/deletes do not rerender. The notification is
a snapshot of "what was unread at the latest arrival," not a live mirror.

Removes the auto-dismiss path on email.read and the
_pending_mail_notifications dict, since there's no per-mail notification
to dismiss. system(action="dismiss") stays for soul / bounce / MCP.

Bounce notifications and MCP inbox notifications continue to use
system(action="notification") — they're distinct event semantics.

See discussions/email-unread-digest-notification-patch.md.
```

## Why this is safe

- **No new infrastructure.** `tc_inbox.InvoluntaryToolCall.replace_in_history` and the `appendix_tracker` are already wired and exercised by soul-flow.
- **Wire-history backward compat.** Old agents with `system(action="notification", source="email")` pairs in their `chat_history.jsonl` keep them — those are immutable history. New arrivals after this patch produce the new shape. Mixed history is fine; each pair is well-formed individually.
- **No on-disk migration.** `mailbox/`, `read.json`, `contacts.json`, schedules — all untouched.
- **Smaller surface than the old model.** Net deletion of code (`_pending_mail_notifications`, the auto-dismiss block in `_read`, the per-arrival prose-build logic in `_on_normal_mail`).
- **Bounded token cost.** Worst case: digest of N unread mails, capped at 10 × 200 chars + headers ≈ 2.5KB per pair. Single pair in wire at a time. No accumulation.
