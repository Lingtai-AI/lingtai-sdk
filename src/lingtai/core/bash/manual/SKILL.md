---
name: bash-manual
description: >
  **Read this before setting up cron, launchd, systemd timers, crontab jobs, or
  scheduled reminders.** Router for Bash-related operational depth beyond the
  bash tool schema: host-scheduler setup, LingTai wake-by-mailbox-drop, script
  hygiene, one-shot `.notification/cron.json` reminders, debugging silent jobs,
  and safe cleanup. Start here for any time-driven recurring work ("every hour",
  "weekdays at 9", "remind me later") or when a scheduled job misbehaves.
version: 1.2.0
---

# Bash Manual — Router

The `bash` tool schema covers one-off command execution. This manual routes to
operational depth that is too long for the schema: host scheduling, mailbox-drop
wakeups, reminder files, debugging, and cleanup.

For ordinary one-off shell commands, use the tool schema. For anything involving
time, recurring work, external schedulers, or a silent scheduled job, start here.

## Nested reference catalog

`bash-manual` owns these nested references. They are parent-owned drill-down
files, not standalone top-level skills.

```yaml
- name: bash-scheduled-work
  location: reference/scheduled-work/SKILL.md
  description: |
    Cron-driven scheduled work: when to use host schedulers, the LingTai
    wake-by-mailbox-drop contract, prompt boundaries, script hygiene, macOS
    launchd, Linux systemd timers, crontab fallback, and the launchd
    process-tree reaping gotcha.
- name: bash-notification-reminders
  location: reference/notification-reminders/SKILL.md
  description: |
    One-shot wakeup reminders via `.notification/cron.json`: payload shape,
    atomic writer, shell example, and the rest checklist for agents leaving work
    pending.
- name: bash-debugging-cleanup
  location: reference/debugging-cleanup/SKILL.md
  description: |
    Debugging and cleanup for scheduled jobs: scheduler fired, script ran, work
    landed, agent saw mail, worked launchd diagnosis, retiring cron jobs, and
    bash work footprint hygiene.
```

## Router table

| Need / keywords | Read |
|---|---|
| Human asks for time-driven recurring work: "every hour", "daily", "weekdays at 9", "write/check/send on a schedule"; choose cron vs event watcher; create launchd/systemd/crontab wiring; understand wake-by-mailbox-drop; write scheduler prompt/script hygiene | `reference/scheduled-work/SKILL.md` |
| Need a one-shot reminder or wakeup nudge while work is pending; `.notification/cron.json`; atomic reminder writer; rest checklist | `reference/notification-reminders/SKILL.md` |
| Scheduled job is silent, fires twice, exits immediately, gets killed by launchd, fails to deliver mail, or must be retired/cleaned up | `reference/debugging-cleanup/SKILL.md` |

## Quick decision tree

1. **One-off deterministic host work?** Use `bash` directly; this manual is not
   needed unless the command is risky, scheduled, or failing mysteriously.
2. **Time itself is the trigger?** Read `reference/scheduled-work/SKILL.md`.
3. **You only need a single future nudge?** Read
   `reference/notification-reminders/SKILL.md`.
4. **A scheduled job already exists and is misbehaving?** Read
   `reference/debugging-cleanup/SKILL.md` before editing blindly.

## Core rules to keep resident

- LingTai has no built-in recurring scheduler. Host schedulers wake agents by
  producing channel input, usually a mailbox-drop or notification file.
- Prefer event watchers/webhooks when an external event is the real trigger;
  prefer cron/launchd/systemd only when time is the trigger or polling is truly
  the right tradeoff.
- Scheduler scripts must be idempotent, audited, logged, absolute-path based,
  and explicit about how they wake the agent.
- On macOS, remember launchd process-tree reaping; use the documented
  double-fork pattern when a child process must outlive the launchd job.
- Do not leave silent janitors or hidden recurring jobs behind. Document and
  clean them up when the human no longer needs them.

## Maintenance

Keep this top-level router short. Add detailed examples, platform recipes, and
troubleshooting trees to nested references so agents can load only the section
needed for the current task.
