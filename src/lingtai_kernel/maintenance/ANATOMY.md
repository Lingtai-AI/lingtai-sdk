# maintenance

Kernel-owned maintenance reporters and future maintenance actions.

> **Maintenance:** see the `lingtai-kernel-anatomy` skill. **Coding agents** update this file in the same commit as code changes. **LingTai agents** report drift as issues.

## Components

- `retention.py` — dry-run retention reporter for issue `Lingtai-AI/lingtai#363`. It scans an agent workdir or direct `.lingtai` root and reports stale candidates for terminal daemon run dirs, historical `mailbox/sent/` copies, opt-in archive mail, and rebuildable `logs/log.sqlite` sidecar indexes. It has no delete/apply function.
- `__init__.py` — public exports for the maintenance package.

## Connections

- **Inbound:** `lingtai.cli` wires `lingtai-agent maintenance cleanup <target>` to `retention.scan_retention()`.
- **Outbound:** Uses only stdlib filesystem reads and JSON parsing. It deliberately does not import runtime services or mutate kernel state.

## Composition

Parent: `src/lingtai_kernel/`. Siblings include `services/` for authoritative log/mail services and `base_agent/` for lifecycle state.

## State

This package owns no durable state. The retention reporter reads these existing runtime paths:

- `daemons/<run_id>/daemon.json` and `.heartbeat`
- `mailbox/sent/`, `mailbox/archive/`
- `mailbox/inbox/`, `mailbox/outbox/`, `mailbox/schedules/` as protected operational mail state
- `logs/events.jsonl`, `logs/token_ledger.jsonl`, `logs/refresh_failed_permanent.json` as protected authoritative or recovery logs
- `logs/log.sqlite` as a rebuildable report candidate only
- `.agent.lock`, `.agent.heartbeat`, `.status.json` as lifecycle protection signals

## Key Invariants

- The package is report-only in PR1: no deletion, move, archive, rewrite, or truncation code path exists.
- `mailbox/outbox/` and `mailbox/schedules/` are pending-delivery/future-send queues and are never candidates.
- Inbox mail is never a candidate, regardless of read state; read does not prove handled.
- `logs/events.jsonl` and token ledgers are authoritative and protected. `logs/log.sqlite` is rebuildable and may be reported as a candidate when stale.
- Active agents, ASLEEP agents, SUSPENDED agents, held `.agent.lock`, and fresh `.agent.heartbeat` protect an agent's interior retention classes from becoming candidates.
- Daemon run dirs require terminal `daemon.json.state`, old age, and stale/missing daemon `.heartbeat`; mtime alone is not enough.

## Notes

- CLI output is intentionally stable JSON under `--json`; human output is a compact summary and reminds operators no files changed.
