---
name: bash-debugging-cleanup
description: >
  Nested bash-manual reference for debugging silent scheduled jobs and retiring
  cron jobs safely: scheduler fired, script ran, work landed, agent saw mail,
  worked launchd diagnosis, cleanup, and bash work footprint hygiene.
version: 1.0.0
last_changed_at: "2026-06-01T01:47:09-07:00"
---

# Debugging and Cleanup Reference

Nested bash-manual reference. Open this when a scheduled job goes silent, fires
incorrectly, or needs to be retired or cleaned up.

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

## Future debugging topics

This section is empty. As more operational knowledge accumulates (debugging pipelines, working with binary data, locale handling), it gets added here.

## Cleanup / Footprint for bash work

`bash` can create anything the command creates: scripts, logs, downloads,
virtualenvs, cron/launchd/systemd units, and arbitrary build artifacts. Because
ownership is command-specific, every non-trivial bash workflow should document
its own cleanup path near the script it creates. Never run a destructive shell
cleanup from a manual without first showing a dry-run and getting explicit user
consent.

Generic footprint check (read-only, records the audit from the agent directory):

```bash
python3 - <<'PY'
import json, time
from pathlib import Path
agent = Path.cwd()
roots = [p for p in [agent / "tmp", agent / "logs", agent / "scripts"] if p.exists()]
def size(p): return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
rows = [(p, size(p)) for p in roots]
total = sum(s for _, s in rows)
print(f"bash-adjacent roots: {len(rows)}; bytes: {total}")
for p, s in rows: print(f"{s:>12}  {p}")
log = agent / "logs" / "cleanup.jsonl"; log.parent.mkdir(parents=True, exist_ok=True)
log.open("a", encoding="utf-8").write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": "bash", "dry_run": True, "candidates": len(rows), "bytes": total, "human_approved": False, "summary": "bash-adjacent footprint audit"}) + "\n")
PY
```

Recommended cadence: when retiring cron jobs, after large downloads/builds, and
whenever a shell workflow writes outside a short-lived temp directory. Cleanup
records belong in `logs/cleanup.jsonl`; cron/launchd/systemd retirement should
also record the scheduler unit name that was unloaded.
