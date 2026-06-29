---
name: daemon-inspection
description: >
  Nested daemon-manual reference for polling cadence, stall heuristics, anti-
  patterns, backend-specific polling notes, and setting reminders before resting
  while daemon work remains pending.
version: 1.1.0
last_changed_at: "2026-06-24T16:24:16-07:00"
---

# Daemon Inspection Reference

Nested daemon-manual reference. Open this before deciding that a daemon is stuck,
before reclaiming it, or before going to rest with daemon work pending.

## Polling cadence — when, how often, and which call

**First principle: completion is push-notified, not polled.** When an emanation reaches a terminal state (`done`, `failed`, `cancelled`, `timeout`), the kernel publishes a compact entry to `.notification/system.json` naming the em-id and pointing at `daemon(action="check", id="em-N")`. You do **not** need to poll to discover completion. If you find yourself running `check` repeatedly waiting for a `state` transition, stop — you're duplicating work the kernel is already doing.

You *do* need to poll when:

- You suspect an emanation is **stuck** (long elapsed wall-clock, no recent activity signals).
- You need a **mid-flight progress update** for your own planning (e.g., before dispatching a second emanation that depends on the first's partial finding).
- A CLI-backend emanation has been silent and you want to read `last_output` to gauge progress.

### Cadence decision tree

Use `elapsed_s` (from `daemon.json` or `list`) to pick an interval. These are starting points, not laws — adjust for task type and your own expected duration.

| `elapsed_s` | Default interval between checks | Rationale |
|---|---|---|
| 0 – 60 s | **Don't check.** Trust the dispatch; do other work. | Emanations almost never hang in the first minute. Polling here is pure noise. |
| 60 – 300 s | If you genuinely need progress, **one check** around 120 s, then back off. | Most one-shot tasks (file scan, focused research) finish in this band. |
| 300 – 900 s | Check at most every **2–3 minutes**. | Long synthesis or multi-file work. Notification will fire on completion. |
| 900 s + | Check every **5 minutes** *only* if you suspect a stall; otherwise wait for the notification. | If you've gone 15+ minutes with no `last_output` change AND `current_tool=null` AND `tool_call_count` unchanged across two consecutive checks → apply the stall heuristic below. |

**Never poll at sub-30-second intervals.** Each `check` returns up to `last` events plus a `daemon.json` snapshot; under 30 s of activity you'll see at most one new event, and the call costs tokens in both the request and the result. If you want a tighter loop, the task is probably wrong for an emanation — run it inline.

### Which call to use, in order

1. **`daemon(action="list")` first** when multiple emanations are in flight and you want a status sweep. Cheap; one line per emanation with `elapsed_s` and `state`. Use it to decide *which* (if any) to investigate.
2. **`daemon(action="check", id="em-N", last=20, truncate=500)`** when one emanation looks suspicious. `last=20` covers ~10 tool dispatches; bump to `last=50` for wider history. Keep `truncate=500` unless you specifically need full tool I/O. The response also carries an `artifacts` block (the run's artifact manifest: relative path/size/mtime/role per important file, plus `result_path`/`error_path`) — read it to learn which files exist and how big they are before opening any of them, instead of `ls`-ing the run folder by hand.
3. **Direct `Read` of `daemon.json`** — only when you need a field `check` doesn't surface (rare). Prefer `check`.
4. **`tail` of `history/chat_history.jsonl`** — when `check` events don't tell you what the LLM is *thinking*. The last assistant text shows the current line of reasoning. (lingtai backend only — CLI backends don't write the LLM transcript here.)

### Stall heuristic (before you `reclaim`)

Before reclaiming, confirm **all** of:

- `state == "running"` and `elapsed_s` exceeds ~2× what you'd reasonably expect for the task.
- `current_tool` is the same value (or `null`) across two `check` calls ≥ 3 minutes apart.
- `tool_call_count` is unchanged across those two checks.
- For CLI backends: `last_output_at` is older than 5 minutes.
- For lingtai backend: no new entries in `chat_history.jsonl` between the two checks.

If any of those is false, the emanation is making progress — wait. Reclaim is destructive: the work in flight is gone (folders persist but the process is killed).

### Backend-specific polling notes

**`lingtai` backend (default).** Rich introspection: `current_tool`, `turn`, `tool_call_count`, `tokens`, and `chat_history.jsonl` all update in real time. Lean on these. You almost never need to look more often than once every 2–3 minutes.

**`claude-code` and `codex` backends.** `turn` is not incremented, `tokens` stays at 0, and the LLM transcript is not in `chat_history.jsonl` (it lives in the external CLI's own session store). Your live signals are:

- `last_output` / `last_output_at` in `daemon.json` — updates per assistant turn for `claude-code`, per `item.completed` for `codex`.
- `current_tool` — tracks the CLI's own tool calls (set on `tool_use` / cleared on `tool_result`).
- `logs/events.jsonl` `cli_output` entries — stdout/stderr stream.

Because the only progress signal is `last_output_at`, the right cadence on CLI backends is **"compare `last_output_at` to `now()`"** rather than a fixed interval: if it advanced since your last look, the emanation is alive; if it hasn't advanced in 5+ minutes AND `current_tool` is unchanged, apply the stall heuristic. Don't confuse a slow tool (large bash, big file read) with a stall — check `current_tool` first.

### Anti-patterns

- **Poll-for-completion loops.** Wrong because completion is pushed. A `while state == "running": sleep` pattern is working against the system — do other work or yield; the notification will tell you when there's something to inspect. If `_advisory.type == "duplicate_tool_call"` appears on repeated `daemon(list/check)`, the result was still executed and not blocked; do not answer it by immediately making the same call again. Choose one owner (usually the parent) to coordinate daemon status, then wait for notification or set one future reminder.
- **`check` immediately after `emanate`.** The first 30 seconds are almost always model warmup + initial tool calls; nothing actionable. Save the call.
- **Reclaiming on a hunch.** See the stall heuristic. Default to "let it cook."
- **`check` with `last=1000, truncate=0`.** Dumps the full event log into your context. Use targeted `last=20` and only widen if needed.


### Resting while daemon work is pending: set a cron notification reminder

If the daemon is healthy but unfinished and you are about to rest, do **not** keep polling and do **not** rely on memory. Set a lightweight wakeup reminder through the `bash-manual` section **"One-shot wakeup reminders via `.notification/cron.json`"**.

**This is mandatory idle care, not an optimization.** Completion is push-notified, but a daemon can also die silently, stall, exit immediately, or get stuck on a provider/model error *without* producing a terminal-state notification — and a CLI-backend emanation gives you no live `tokens`/`turn` signal to fall back on. So before going IDLE with daemon work pending and **unverified-healthy**, arm at least one self-wake. Choose the delay from the task's *expected* duration, not a fixed value: a focused scan might warrant a 2-minute check; a long multi-file synthesis, 10–15 minutes.

When the reminder fires, **health-check before trusting it kept working**:

- `state` is still `running` (not silently `failed`/`timeout`);
- `last_output_at` (CLI backends) or `chat_history.jsonl` (lingtai) advanced since you slept;
- `current_tool` / `tool_call_count` changed, or events show fresh activity;
- the output file / worktree shows real progress;
- it is not blocked on an interactive prompt or a repeated provider/model error.

If there is **no** progress, do not re-arm and wait again — apply the stall heuristic and `reclaim`, downgrade the backend/scope, switch path, and **report to the human**. Indefinite waiting on a dead daemon is the failure this rule exists to prevent.

Use this when:

- a Claude Code / Codex backend task is still running but has enough context to finish alone;
- you opened a PR and want to check CI/mergeability later;
- a render/download/build/test run is expected to complete after you sleep;
- the human asked for a later follow-up and the reminder is for you, not for them.

The reminder should say what is pending and what to check, for example:

```text
⏺ Codex active. Plot regenerated (362KB, 23:38) — waiting for caption + commit. Polling at 23:42.
```

When the `cron` notification wakes you, read pad first, inspect the relevant daemon/job/PR, act, then dismiss the channel with `notification(action="dismiss_channel", channel="cron")`.

This complements the polling rule above: completion is push-notified, stalls are inspected sparingly, and deliberate rest gets one future reminder rather than a busy loop.

## Worked example: a daemon that's been running 5 minutes

You called `daemon(action="emanate", ...)` for `em-3`, asked it to "scan src/ for security issues", and it's been running 5 minutes. You're nervous.

```bash
# What's the live state?
read("daemons/em-3-20260427-094215-abc123/daemon.json")
# → state=running, turn=8, current_tool=null, tool_call_count=15, tokens.input=22000

# Last few lines of the transcript
bash("tail -n 20 daemons/em-3-20260427-094215-abc123/history/chat_history.jsonl")
# → assistant: "Found a potential SQL injection in db.py:42. Continuing..."

# Recent tool activity
bash("tail -n 10 daemons/em-3-20260427-094215-abc123/logs/events.jsonl")
# → series of read/grep events on src/db/, src/auth/
```

That's a healthy pattern: the LLM is between tool calls, has good progress narrative, and is steadily working through files. **Don't reclaim.** Let it cook.
