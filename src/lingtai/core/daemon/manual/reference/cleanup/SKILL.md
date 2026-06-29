---
name: daemon-cleanup
description: >
  Nested daemon-manual reference for scope boundaries and daemon footprint
  cleanup: what the manual does not cover, reclaim persistence, and safe cleanup
  of old daemon artifacts.
version: 1.0.0
last_changed_at: "2026-06-01T01:56:31-07:00"
---

# Daemon Cleanup Reference

Nested daemon-manual reference. Open this for daemon footprint audits, old
artifact cleanup, and scope boundaries.

## What the manual does NOT cover

- Provider routing / LLM presets — deferred to a separate spec.
- Cross-process recovery — if your kernel restarted mid-daemon, the folder may show `state=running` indefinitely. Compare `now()` vs `.heartbeat` mtime to detect orphans.
- Folder cleanup — there is none. Molts wipe the working dir. For non-molting agents, you may eventually want to `rm -rf daemons/em-*-2026-04-*` manually.

## Cleanup / Footprint

Daemon runs are intentionally persistent forensic records. Each emanation leaves
`daemons/em-*` under the parent agent, including `daemon.json`, events,
transcript/history, result files, and token ledgers. Do not delete an active
run, and do not delete a run you still need for a report, review, or cost audit.

Footprint check (read-only, records the audit):

```bash
python3 - <<'PY'
import json, time
from pathlib import Path
agent = Path.cwd()
daemons = agent / "daemons"
items = [p for p in daemons.glob("em-*") if p.is_dir()] if daemons.is_dir() else []
def size(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
rows = [(p, size(p)) for p in items]
total = sum(s for _, s in rows)
print(f"daemon runs: {len(rows)}; bytes: {total}")
for p, s in sorted(rows, key=lambda r: r[1], reverse=True)[:20]:
    print(f"{s:>12}  {p}")
log = agent / "logs" / "cleanup.jsonl"; log.parent.mkdir(parents=True, exist_ok=True)
log.open("a", encoding="utf-8").write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": "daemon", "dry_run": True, "candidates": len(rows), "bytes": total, "human_approved": False, "summary": "daemon footprint audit"}) + "\n")
PY
```

Recommended cadence: after daemon-heavy debugging sessions, before molt if a
large review generated many runs, and monthly for always-on orchestrators.
Cleanup is optional. Before deleting old completed `daemons/em-*` folders, show
the dry-run output to the user and get explicit consent; then append an `apply`
record to `logs/cleanup.jsonl` with the deleted paths/bytes.
