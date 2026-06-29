---
name: bash-notification-reminders
description: >
  Nested bash-manual reference for one-shot wakeup reminders using
  `.notification/cron.json`: payload shape, atomic writer, shell example, and the
  rest checklist for agents leaving work pending.
version: 1.0.0
last_changed_at: "2026-06-20T13:05:49-07:00"
---

# Notification Reminder Reference

Nested bash-manual reference. Open this when you need a one-shot reminder or a
lightweight wakeup nudge rather than a full recurring host scheduler.

## One-shot wakeup reminders via `.notification/cron.json`

Sometimes you do **not** want a recurring cron job and you do **not** want to self-send mail. You only need a lightweight alarm for your future self: "something is still pending; wake later and check it." Use a cron notification reminder for that.

Typical example:

```text
⏺ Codex active. Plot regenerated (362KB, 23:38) — visibly more polished than my crude version. Waiting for caption + commit. Polling at 23:42.
```

That sentence has the right shape: current state, what changed, what remains, and the next check time. The mechanism is a scheduled script that writes one file:

```text
<agent-workdir>/.notification/cron.json
```

The kernel's notification sync reads `.notification/*.json`, injects the `cron` channel into the agent's wire context, and wakes the agent to act. After handling it, clear it with:

```text
notification(action="dismiss_channel", channel="cron")
```

Use this pattern when:

- you are going to sleep/rest but a daemon, CLI coding agent, CI job, PR, render, download, or external process may need a follow-up;
- the reminder is for **you**, not for the human;
- a single check is enough, or the repeated cadence is purely mechanical;
- self-email would add mailbox latency/state and does not buy anything.

Do **not** use it when:

- the human needs to be notified — use the channel the human used, or an external addon if appropriate;
- the reminder must survive process death and machine reboot but you only used a detached `sleep`; use launchd/systemd/crontab for persistence;
- you are tempted to poll every few seconds. Set one sane reminder, then rest.

### Payload shape

A producer that cannot import the kernel helper should still write the full notification envelope, not a bare message. Minimal valid shape:

```json
{
  "header": "Cron reminder: check Codex plot run",
  "icon": "⏰",
  "priority": "normal",
  "published_at": "2026-05-18T06:42:00Z",
  "data": {
    "source": "cron-reminder",
    "message": "Codex active. Plot regenerated (362KB, 23:38) — waiting for caption + commit.",
    "todo": "Check Codex status, inspect caption, commit if ready.",
    "reminder_id": "plot-caption-2026-05-17T23-42"
  }
}
```

Fields the agent will care about:

- `header` — short visible summary.
- `priority` — usually `normal`; use `high` only when the check is time-sensitive.
- `published_at` — UTC ISO timestamp; useful when the agent wakes late.
- `data.message` — what changed since the agent rested.
- `data.todo` — the concrete next action.
- `data.reminder_id` — stable enough to recognize duplicate fires.

### Atomic writer

External scripts should write via `tmp + rename` so the kernel never sees truncated JSON:

```bash
export AGENT_DIR="/Users/<you>/work/<project>/.lingtai/<agent>"
export REMINDER_ID="task-followup-$(date +%Y%m%d-%H%M%S)"

/usr/bin/python3 - <<'PY'
import json, os, pathlib, time
from datetime import datetime, timezone

agent = pathlib.Path(os.environ["AGENT_DIR"])
notif = agent / ".notification"
notif.mkdir(exist_ok=True)
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
payload = {
    "header": "Cron reminder: check pending task",
    "icon": "⏰",
    "priority": "normal",
    "published_at": now,
    "data": {
        "source": "cron-reminder",
        "message": "Background job still running — waiting for output + commit.",
        "todo": "Check job status, inspect output, commit if ready.",
        "reminder_id": os.environ.get("REMINDER_ID", "cron-reminder"),
        "epoch": int(time.time()),
    },
}
target = notif / "cron.json"
tmp = target.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(target)
PY
```

### One-shot reminder from the shell

For a short local reminder where the machine is expected to stay awake, a detached sleeper is enough:

```bash
DELAY_SECONDS=240
nohup /bin/bash -lc 'sleep '"$DELAY_SECONDS"'; export AGENT_DIR="/Users/<you>/work/<project>/.lingtai/<agent>"; /usr/bin/python3 - <<"PY"
import json, os, pathlib, time
from datetime import datetime, timezone
notif = pathlib.Path(os.environ["AGENT_DIR"]) / ".notification"
notif.mkdir(exist_ok=True)
payload = {
  "header": "Cron reminder: check pending daemon",
  "icon": "⏰",
  "priority": "normal",
  "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
  "data": {
    "source": "cron-reminder",
    "message": "I rested while a daemon/CLI job was still active.",
    "todo": "Read pad, then run daemon(list) or inspect the named job/PR.",
    "reminder_id": "daemon-check-" + str(int(time.time())),
  },
}
target = notif / "cron.json"
tmp = target.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(target)
PY' >/tmp/lingtai-cron-reminder.log 2>&1 &
```

This is not a replacement for OS scheduling: a detached sleeper can be lost if the shell/process tree is killed or the machine sleeps through the interval. For long delays or repeated checks, put the same writer into launchd/systemd/crontab using the sections above.

### Rest checklist

Before resting with pending work:

1. Update pad with the current state and what the reminder should inspect.
2. Set one `cron` notification reminder at a sensible check time.
3. Include a concise state sentence and a concrete `todo`.
4. Rest (`system(action="sleep")`) or end the turn.
5. On wake: handle the `cron` reminder, then `notification(action="dismiss_channel", channel="cron")`.
