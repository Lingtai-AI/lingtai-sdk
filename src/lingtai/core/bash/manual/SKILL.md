---
name: bash-manual
description: >
  **You can learn how to set up cron here.** If the human asks you to do
  something on a schedule ("write a poem every hour," "remind me at 9am
  weekdays," "every 30 minutes check X"), this manual tells you how to
  wire it up using the host OS scheduler (macOS launchd / Linux systemd /
  crontab) — including the wake-by-mailbox-drop contract that lets a
  cron job invoke YOU, the hygiene rules that keep the script alive past
  its first failure, and a debugging walkthrough for when a scheduled
  job goes silent.

  Read this manual when:
    - The human asks for any time-driven recurring work and you don't yet
      know how to set up cron on this machine. **Read this BEFORE writing
      a plist or asking the human to do it manually.**
    - You are debugging a scheduled job that has gone silent, fires twice,
      kills its child process on exit, or otherwise misbehaves.
    - You are about to write the cron prompt body and want to know what
      belongs in the cron script versus what belongs in a custom skill
      the script invokes.
    - You need the macOS launchd "process-tree reaping" gotcha and the
      double-fork helper that fixes it (the #1 reason "my agent gets
      killed when launchd's script exits").

  Covers: when to reach for cron vs an event watcher vs an in-turn poll;
  the kernel's mailbox-drop wake contract (write JSON → outbox → kernel
  delivers it to the recipient on next cycle); seven hygiene rules
  (idempotent, audit-on-fire, log conventions, `set -euo pipefail`,
  absolute paths for binaries, refresh-vs-queue semantics, no silent
  janitors); full launchd plist template + load/unload commands;
  systemd `.service` + `.timer` template + `Persistent=true` semantics;
  crontab fallback; the launchd process-tree reaping gotcha with a
  working Python double-fork daemonizer; a four-step debugging tree
  (scheduler fired? script ran? work landed? agent saw mail?) with a
  worked "silent hourly cron" diagnostic session.

  Does NOT cover: the bash tool's basic input/output (the tool's own
  schema description handles that), the kernel's mailbox internals
  (see `core/mailbox/ANATOMY.md`), or the LingTai TUI's own cron
  settings. Other bash topics (debugging pipelines, locale handling,
  binary data idioms) will accumulate here as written.
version: 1.1.0
---

# Bash Manual

The `bash` tool's schema-level description covers the happy path. This manual is the place for depth the description can't carry: symptoms → causes → fixes, non-obvious idioms, and worked patterns.

The dominant topic right now is **scheduling** — using the host OS's cron facility (launchd on macOS, systemd timers or crontab on Linux) to wake an agent on a fixed schedule. Everything else is a thinner section or a future addition.

---

# Scheduled / cron-driven work

## When to use scheduled work

Scheduled work is for things that should happen *because time has passed*, not because someone sent a message. Three patterns to distinguish:

1. **Time-driven, agent-acts** — "every hour, write one poem and ship it." Time is the trigger; the agent does the substantive work. **This is what cron is for.**
2. **Event-driven, time-tolerant** — "when an email arrives, reply within an hour." The event is the trigger; time is just a deadline. Use the event source (IMAP poller, webhook, mailbox watch), not cron.
3. **Inside-the-turn periodic** — "while you're already in a turn, also check Z if 30 minutes have passed since last check." This is a turn-loop idiom (compare `time.time()` against a stored timestamp), not external scheduling.

If the human says "do X every hour" and X is substantive, you want pattern 1. If they say "be quick when Y happens," pattern 2. If they say "while you're at it, also Z," pattern 3.

**Don't reach for cron when a `Monitor`/watch will do.** A poll loop fires whether or not anything changed and will burn tokens on empty cycles. Cron is appropriate when the work is unconditional ("write a poem regardless") or when the polling-vs-events tradeoff genuinely favors polling (cheap check, source has no event channel).

## The wake-by-mailbox-drop contract

The LingTai kernel has **no built-in scheduler**. Cron jobs interact with you the same way humans and other agents do: by writing a `message.json` to your outbox-side mailbox.

The full contract:

1. The cron script generates a UUID and writes one file:
   `<project>/.lingtai/human/mailbox/outbox/<uuid>/message.json` (when the human is the sender).
   Human is a pseudo-agent, so the file goes to the **human outbox**, not directly to your inbox. Your kernel polls every active human outbox and claims messages addressed to you on the next cycle.
2. The kernel sees the message addressed to you, atomically renames the folder to `human/mailbox/sent/<uuid>/`, and copies it into `<your-agent>/mailbox/inbox/<uuid>/`.
3. On your next turn, you read the inbox, see the new message, and act.

That's it. **Anything that can write a JSON file to the outbox can wake you on a schedule.** launchd, systemd, crontab, `at`, an IFTTT webhook, a different agent's behavior — all the same to you.

Message template (the cron script generates this, fills in `${UUID}`, `${SUBJECT}`, `${BODY}`, `${TIMESTAMP}`):

```json
{
  "id": "${UUID}",
  "_mailbox_id": "${UUID}",
  "from": "human",
  "to": ["<your-address>"],
  "cc": [],
  "subject": "${SUBJECT}",
  "message": ${BODY_AS_JSON_STRING},
  "type": "normal",
  "received_at": "${TIMESTAMP}",
  "identity": {
    "address": "human",
    "agent_name": "human",
    "via": "<scheduler-name>-cron"
  }
}
```

Use `via: "<scheduler-name>-cron"` (e.g. `"launchd-cron"`, `"systemd-cron"`) so you can tell scheduled mail apart from interactive mail in your audit log.

## When to write the prompt — short, not long

A common anti-pattern: stuffing the full operational recipe ("write a poem, then run mmx with these flags, then commit, then push, then trigger the workflow…") into the cron script's prompt body. This is wrong on two axes:

- **The prompt is replayed every hour.** Updating the recipe means editing the cron script, redeploying, often touching launchd or systemd. Friction.
- **The recipe IS knowledge that belongs to YOU.** Encode it in a custom skill at `.library/custom/<recipe-name>/SKILL.md`. The prompt then says "use your `<recipe-name>` skill" and is one sentence. The skill is editable in-place, version-controlled, and discoverable to other agents on the same network.

Rule: **cron prompts wake you and supply the time-bound context (which hour, what just changed). Skills supply the procedure.**

Example (libai's hourly poem cron):

```
太白吾兄，又是一个时辰。
此刻乃${HOUR_NOTE}（${NOW_LOCAL}）。
请援用 `hourly-poem` 之技——观当世一事，作诗一首，配乐一曲，并刊于网。
所有步骤、路径、命令皆备于该技中，依之而行即可。
```

That's the entire prompt. Six lines. The 200-line recipe lives in the skill.

## Hygiene — the rules that keep scheduled scripts alive

### 1. Idempotent

A cron script must be safe to run **twice in a row** with no harm. Cron fires on a wall clock; nothing prevents two firings from racing (system clock changes, missed-then-caught-up firings, double-loaded launchd plists). Always check "did the work already happen for this cycle?" before doing it again.

For mail-drop scripts, idempotency comes for free if you generate a fresh UUID per fire — duplicate mail in the inbox is annoying but harmless. For scripts that DO work (e.g. running a generator), guard with a marker file:

```bash
MARK="$WORKDIR/.last-fire-$(date +%Y%m%d-%H)"
[ -f "$MARK" ] && exit 0     # already ran this hour
# ... do the work ...
touch "$MARK"
```

### 2. Audit the previous cycle on every fire

Every fire is also a chance to verify the *previous* fire actually completed. Add an audit block at the top of the script:

```bash
# Did anything land where it should have, in the last 75 minutes?
RECENT=$(git -C "$REPO" log origin/main --since="75 minutes ago" --oneline | wc -l | tr -d ' ')
if [ "$RECENT" = "0" ]; then
  echo "$(date -Iseconds) [audit] WARN: no commits in last 75min — last cron may have failed" >> "$LOG_FILE"
fi
```

Cron failures are silent by default. Audit-on-next-fire turns the silence into a log line you can grep for.

### 3. Append to a log file; never trust stdout/stderr

launchd and systemd capture stdout/stderr to the paths you configure, but those files often get rotated, cleared on system updates, or simply forgotten. Your script should always also write to its own log:

```bash
LOG_FILE="${HOME}/.lingtai-tui/cron/<job-name>.log"
log() { echo "$(date -Iseconds) $*" >> "$LOG_FILE"; }
log "[fire] starting cycle"
```

Tag each line with a category (`[send]`, `[audit]`, `[refresh]`, `[err]`) so you can grep specific events later. Use ISO 8601 timestamps with timezone (`date -Iseconds`) — relative timestamps lie when the system reboots.

### 4. `set -euo pipefail` always

Without this, a typo or a transient error mid-script silently continues, leaving partial state. With it, any failure aborts the script and you see the failure in the log.

```bash
#!/bin/bash
set -euo pipefail
```

If you genuinely need a command's failure to be ignored, opt in explicitly: `cmd || true`.

### 5. Absolute paths for binaries

launchd and systemd run with a sparse `PATH`. `git`, `gh`, `python3` may not be on `$PATH` even if they work fine in your shell. Use absolute paths:

```bash
GIT="/usr/bin/git"
GH="/opt/homebrew/bin/gh"
PYTHON="${HOME}/.lingtai-tui/runtime/venv/bin/python"
```

Or set `PATH` explicitly at the top of the script. Don't trust the inherited one.

### 6. Dropping mail does NOT wake the agent — it just queues

Writing to the outbox is the queue, not the doorbell. The agent will see the mail on its next turn cycle. If it's actively in a long-running turn or asleep, the mail waits until the next active turn.

If you need the agent to act on the mail *promptly* (within seconds), follow the mail-drop with `touch .refresh` and **stop there**. The kernel's `_perform_refresh` (`base_agent/lifecycle.py:_perform_refresh`) handles the rest: it spawns a deferred-relaunch watcher that waits for `.agent.lock` to release and then `Popen`s the new agent itself. The cron script does not need to wait, does not need to verify, does not need to relaunch.

```bash
# Mail-drop already done above (writing message.json under human/mailbox/outbox/<uuid>/).
# Now nudge the agent to pick it up immediately:
touch "$PROJECT_ROOT/.lingtai/<agent>/.refresh"
# Done. Exit. The kernel's refresh watcher handles shutdown + relaunch.
```

That's the entire refresh recipe. If the human just wants the work done eventually (within the next active turn), even the `touch .refresh` is overhead — drop the mail and exit.

#### Anti-pattern — DO NOT do any of these

The following pattern looks reasonable but causes **duplicate-agent accumulation** (multiple Python interpreters all running against the same workdir, observed in vivo as 6 stacked PIDs after 6 hourly fires):

```bash
# ❌ DANGEROUS — do not copy this pattern
touch "$LIBAI_DIR/.refresh"
WAIT_DEADLINE=$(($(date +%s) + 60))
while [ -e "$LIBAI_DIR/.agent.lock" ]; do
  [ $(date +%s) -gt $WAIT_DEADLINE ] && rm -f "$LIBAI_DIR/.agent.lock" && break
  sleep 0.5
done
"$VENV_PYTHON" "$RELAUNCH_SCRIPT" ...   # parallel relaunch
```

Two failure modes baked in:

1. **Path-existence check on `.agent.lock` is racy.** The kernel uses `fcntl.flock` for mutual exclusion, not the file's mere presence. The lockfile vanishes near the *end* of `_stop()`, but the Python interpreter can linger 30–60s after that doing HTTP teardown, mail-listener stop, and MCP child reaping. Polling for the path to disappear and then spawning a new agent races a still-living process.

2. **`rm -f .agent.lock` on timeout is destructive.** flock is invisible to `rm`; you delete the path while the kernel still considers itself the owner. The new agent then creates a fresh lockfile at the same path and acquires flock on that — so you have two agents, each holding flock on a different inode at the same path. When the old process finishes shutdown and calls its tail-end `unlink(.agent.lock, missing_ok=True)`, it can delete the **new** agent's lockfile.

3. **Parallel relaunch races the kernel's own watcher.** `touch .refresh` already triggers `_perform_refresh`, which spawns a deferred-relaunch process (see `base_agent/lifecycle.py:_perform_refresh`) that does the wait-for-lock-then-spawn dance correctly. Adding your own relaunch in the cron means two processes are racing to be "the new agent." Whichever loses the flock will sit in `acquire_lock(timeout=10)` for 10 seconds and then crash, but during those 10 seconds you have two Python processes visible in `ps`.

**Rule:** if you find yourself parsing `.agent.lock`, polling for it, or removing it from a script, stop. The lock is the kernel's. Touch `.refresh` and exit.

### 7. No janitors in the cron prompt unless the human asked

Cron scripts and the skills they invoke should never silently delete work products ("janitor old mp3s," "prune old logs"). Deletion is a design decision, not a hygiene step. If the human wants pruning, they will ask for it explicitly. Otherwise leave artifacts alone — disk is cheap, lost work isn't.

## macOS — launchd

On macOS, the right scheduler is **launchd** (not cron). cron exists on macOS but is deprecated; launchd is the system-managed equivalent and behaves correctly across sleep/wake, reboots, and login sessions.

### Plist template

Save to `~/Library/LaunchAgents/<reverse-domain-name>.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.example.my-job</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/yourname/.scripts/my-job.sh</string>
  </array>

  <!-- Pick ONE of StartCalendarInterval or StartInterval -->

  <!-- Fire at minute 0 every hour: -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <!-- OR fire every N seconds: -->
  <!-- <key>StartInterval</key> <integer>300</integer> -->

  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>/Users/yourname/.scripts/my-job.out</string>
  <key>StandardErrorPath</key>
  <string>/Users/yourname/.scripts/my-job.err</string>
</dict>
</plist>
```

### Loading

```bash
launchctl load ~/Library/LaunchAgents/ai.example.my-job.plist
launchctl list | grep ai.example.my-job   # verify it's loaded
launchctl start ai.example.my-job         # fire once for testing
```

### Unloading

```bash
launchctl unload ~/Library/LaunchAgents/ai.example.my-job.plist
```

A plist edit only takes effect after `unload` + `load` (or after a reboot).

### macOS gotcha: launchd process-tree reaping

If your cron script needs to **launch a long-running daemon as a side effect** (e.g. relaunching a LingTai agent after dropping mail + refreshing), launchd will kill that daemon when the script exits unless you fully detach it.

Symptom: the script's child process (your agent) starts, you see its log briefly, then it dies seconds after the script returns.

Cause: launchd reaps the entire process tree of a job when the job's `ProgramArguments` process exits. `&` and `disown` (which work in interactive shells) do nothing under launchd because there's no shell job-control table.

Fix: **double-fork the daemon** so it ends up with PPID=1 (init), fully detached:

```python
#!/usr/bin/env python3
# fork-exec helper — call from the cron script
import os, sys, subprocess

def daemonize():
    if os.fork() > 0: os._exit(0)   # parent exits
    os.setsid()                      # detach from controlling terminal
    if os.fork() > 0: os._exit(0)   # first child exits
    # grandchild: PPID is now 1
    os.chdir("/")
    sys.stdin = open("/dev/null", "r")

if __name__ == "__main__":
    target_cmd = sys.argv[1:]
    daemonize()
    log_path = os.environ.get("DAEMON_LOG", "/tmp/daemon.log")
    with open(log_path, "ab") as f:
        subprocess.Popen(target_cmd, stdout=f, stderr=f, start_new_session=True)
```

The cron script calls this helper and exits — the grandchild survives.

### Useful launchctl commands

```bash
launchctl list | grep <prefix>             # which of my jobs are loaded
launchctl list ai.example.my-job           # full status (PID, last exit code)
launchctl print gui/$(id -u)/ai.example.my-job   # newer macOS — full diagnostic
log show --predicate 'process == "launchd"' --last 1h | grep ai.example   # system log lines
```

`launchctl list <label>` shows `LastExitStatus`. **Non-zero ≠ broken** (your script may exit nonzero on intentional skip paths), but a sudden change from 0 to nonzero is worth investigating.

## Linux — systemd timer

On modern Linux, systemd timers are the right primitive. Two unit files: a `.service` (what to run) and a `.timer` (when to run).

`~/.config/systemd/user/my-job.service`:

```ini
[Unit]
Description=My hourly job

[Service]
Type=oneshot
ExecStart=/bin/bash /home/yourname/.scripts/my-job.sh
StandardOutput=append:/home/yourname/.scripts/my-job.out
StandardError=append:/home/yourname/.scripts/my-job.err
```

`~/.config/systemd/user/my-job.timer`:

```ini
[Unit]
Description=Run my-job every hour

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

Activation:

```bash
systemctl --user daemon-reload
systemctl --user enable --now my-job.timer
systemctl --user list-timers          # verify scheduled
systemctl --user status my-job.service
journalctl --user -u my-job.service   # logs
```

`Persistent=true` matters: if the machine was off when a fire was scheduled, the timer will fire on next boot to "catch up." Drop it if catch-up firings are unwanted (e.g., "post the morning poem" should not post 3 backed-up poems after a weekend power-out).

## Linux fallback — crontab

If systemd isn't available (containers, minimal distros), use crontab. Edit:

```bash
crontab -e
```

Add a line:

```
0 * * * * /bin/bash /home/yourname/.scripts/my-job.sh >> /home/yourname/.scripts/my-job.log 2>&1
```

5 fields: `minute hour day-of-month month day-of-week`. The default `PATH` for crontab is even sparser than launchd's — set `PATH=` at the top of the crontab file or use absolute paths everywhere in the script.


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
system(action="dismiss", channel="cron")
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
export AGENT_DIR="/Users/huangzesen/work/lingtai-dev/.lingtai/codex-gpt5.5"
export REMINDER_ID="plot-caption-$(date +%Y%m%d-%H%M%S)"

/usr/bin/python3 - <<'PY'
import json, os, pathlib, time
from datetime import datetime, timezone

agent = pathlib.Path(os.environ["AGENT_DIR"])
notif = agent / ".notification"
notif.mkdir(exist_ok=True)
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
payload = {
    "header": "Cron reminder: check Codex plot run",
    "icon": "⏰",
    "priority": "normal",
    "published_at": now,
    "data": {
        "source": "cron-reminder",
        "message": "Codex active. Plot regenerated (362KB, 23:38) — waiting for caption + commit.",
        "todo": "Check Codex status, inspect caption, commit if ready.",
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
nohup /bin/bash -lc 'sleep '"$DELAY_SECONDS"'; export AGENT_DIR="/Users/huangzesen/work/lingtai-dev/.lingtai/codex-gpt5.5"; /usr/bin/python3 - <<"PY"
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
5. On wake: handle the `cron` reminder, then `system(action="dismiss", channel="cron")`.

## Debugging cron — when things go silent

When a scheduled job stops working, the failure is almost always in one of these places. Walk the list in order.

### 1. Did the scheduler fire?

- macOS: `launchctl list <label>` — check `LastExitStatus` and the `PID` field. If `PID = -` and `LastExitStatus = 0` and you expect a recent fire, the schedule didn't trigger.
- Linux systemd: `systemctl --user list-timers` — shows last and next fire times. If "last" is older than expected, the timer didn't fire.
- crontab: check `/var/log/cron` (or `journalctl -u cron`) for "CMD" lines.

If the scheduler didn't fire, the culprit is usually:

- **Plist/timer file is wrong** — XML/INI parse error means the unit silently didn't load. macOS: `plutil -lint <plist>`. systemd: `systemctl --user status <timer>`.
- **Job was unloaded** — somebody (you, an installer, an OS update) called `launchctl unload` or `systemctl disable`.
- **Sleep/standby** — laptop was closed during the schedule. launchd handles this for `StartCalendarInterval` (catches up on wake) but not for `StartInterval`. systemd needs `Persistent=true`.
- **Clock skew** — system time was wrong at fire time, now correct. Look at `date` output and compare to expected fire time.

### 2. Did the script run?

- Check the script's own log file (the `LOG_FILE` you write to, not just stdout/stderr).
- If `LOG_FILE` has no entry from the expected time, but the scheduler claims it fired: the script crashed before its first `log` call. Check the launchd `.err` file or systemd journal for the bash error.
- If `LOG_FILE` has a `[fire]` entry but no completion entry: the script started but exited mid-way. `set -euo pipefail` should have made the failure visible — re-check that line is at the top.

### 3. Did the work land?

This is what audit blocks are for. If the script ran and logged success but the downstream artifact (commit, file, message) isn't there, the failure is in the script's logic, not in cron. Read the script's audit lines and the commands they wrap.

### 4. Did the agent see the mail?

If the cron drops mail and you (the agent) are debugging "why didn't I act":

- Is the message in `human/mailbox/sent/<uuid>/`? If yes: the kernel claimed it; you should have seen it in your inbox.
- Is it still in `human/mailbox/outbox/<uuid>/`? Then the kernel never claimed it. Check that you (the recipient) are running and your `to` address matches.
- Is the file there but malformed JSON? `python3 -c "import json; json.load(open('<path>'))"` — a JSON parse error means the kernel rejected it.

## Debugging session for a "silent hourly cron" (worked example)

Symptom: cron is supposed to fire hourly. Last poem on the website is from 5 hours ago. Nothing in the cron log between 5h ago and now.

```bash
# Step 1: did the scheduler fire?
launchctl list | grep ai.lingtai
# ai.lingtai.libai-hourly  -  0
# PID is "-" (not running) and LastExitStatus is 0 — so it's loaded but
# either never fired or fired and exited cleanly each time.

# Step 2: launchd's own logs
log show --predicate 'process == "launchd"' --last 6h | grep libai-hourly
# (no output) → launchd never fired the job in the last 6 hours.

# Step 3: did the plist get unloaded?
ls -la ~/Library/LaunchAgents/ai.lingtai.libai-hourly.plist
# (file exists)
plutil -lint ~/Library/LaunchAgents/ai.lingtai.libai-hourly.plist
# OK → plist parses fine

# Step 4: was the laptop asleep?
pmset -g log | grep -i 'sleep\|wake' | tail -20
# Sleep ... 5h ago, Wake ... just now → mystery solved.
# The Mac was asleep for the missed hours. launchd catches up at most one
# missed StartCalendarInterval fire on wake; longer outages drop the
# missed fires entirely.
```

Fix in this case: not a code fix — a "this is how launchd works, bring the machine out of sleep at the relevant times" fact. Document the limitation, optionally add a wake-from-sleep schedule via `pmset` if hourly accuracy across closed-laptop hours matters.

## Cleanup — when retiring a cron job

Reverse of setup, in this order:

1. `launchctl unload <plist>` (or `systemctl --user disable --now <timer>`).
2. Verify it's gone: `launchctl list | grep <prefix>` (or `systemctl --user list-timers`).
3. Delete the plist/unit files.
4. Delete the script and its log files (or archive them if the human wants the history).
5. Remove any `~/Library/LaunchAgents/<label>.plist` entry that wasn't caught above.

Don't delete the script first — if the unit is still loaded and tries to fire a missing executable, you get noisy error logs.

---

# Other bash topics

This section is empty. As more operational knowledge accumulates (debugging pipelines, working with binary data, locale handling), it gets added here.
