#!/usr/bin/env python3
"""Benchmark: rebuild the current agent session from the trajectory.

Proves the optimized rebuild (Tier 1, indexed ``log.sqlite`` query) avoids a
full ``events.jsonl`` scan (Tier 3) and is dramatically faster on a large,
many-molt trajectory. Also times Tier 2 (bounded reverse scan) for reference.

Usage:

    # Synthetic source (default): N total events, a molt boundary every K events.
    python tools/bench_agent_session_rebuild.py --events 200000 --molt-every 4000

    # Against a real agent dir (uses its existing logs/events.jsonl + log.sqlite):
    python tools/bench_agent_session_rebuild.py --agent-dir /path/to/agent

The synthetic generator writes a temp agent dir with ``logs/events.jsonl`` and a
matching ``logs/log.sqlite`` built via the kernel's own SQLiteEventIndex, so the
indexed path is measured exactly as production would run it.

Determinism note: this is a benchmark, not a test — it uses wall-clock timing.
It intentionally avoids ``random``; event token values are derived from the
event index so runs are reproducible.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

# Ensure the in-tree kernel is importable when run from the repo root.
_REPO_SRC = Path(__file__).resolve().parents[1] / "src"
if _REPO_SRC.is_dir():
    sys.path.insert(0, str(_REPO_SRC))

from lingtai_kernel.agent_session import (  # noqa: E402
    MOLT_BOUNDARY_EVENT,
    TOKEN_EVENT,
    _rebuild_via_full_scan,
    _rebuild_via_reverse_scan,
    rebuild_agent_session_from_events,
)


def _gen_synthetic_agent_dir(
    dest: Path, *, total_events: int, molt_every: int
) -> tuple[int, int]:
    """Write logs/events.jsonl and logs/log.sqlite. Returns (molt_count, boundary_line)."""
    from lingtai_kernel.services.logging import SQLiteEventIndex

    logs = dest / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    events_path = logs / "events.jsonl"
    sqlite_path = logs / "log.sqlite"

    index = SQLiteEventIndex(sqlite_path)
    conn = index._ensure_open()

    molt_count = 0
    boundary_line = 0
    ts = 1_700_000_000.0
    with open(events_path, "wb") as f:
        with index._lock:
            conn.execute("BEGIN")
            for i in range(total_events):
                ts += 1.0
                if molt_every > 0 and i > 0 and i % molt_every == 0:
                    molt_count += 1
                    boundary_line = i
                    ev = {
                        "type": MOLT_BOUNDARY_EVENT,
                        "ts": ts,
                        "molt_count": molt_count,
                        "before_tokens": 120000,
                        "after_tokens": 8000,
                        "initiator": "agent",
                    }
                else:
                    # A token-carrying provider round. Values derived from i so
                    # the run is reproducible.
                    ev = {
                        "type": TOKEN_EVENT,
                        "ts": ts,
                        "input_tokens": 1000 + (i % 500),
                        "output_tokens": 200 + (i % 100),
                        "thinking_tokens": 50,
                        "cached_tokens": 700 + (i % 300),
                    }
                line = json.dumps(ev, ensure_ascii=False)
                offset = f.tell()
                f.write((line + "\n").encode("utf-8"))
                conn.execute(
                    "INSERT OR IGNORE INTO events(ts, type, agent_address, "
                    "agent_name_snapshot, fields_json, source_file, source_offset, "
                    "source_line, source_kind, scope, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    SQLiteEventIndex.event_row(
                        dict(ev),
                        source_file=str(events_path),
                        source_offset=offset,
                        source_line=i + 1,
                        source_kind="agent_events",
                        scope="agent",
                    ),
                )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    index.close()
    # Drop WAL sidecars so the read path uses the immutable fast path.
    for suffix in ("-wal", "-shm"):
        p = sqlite_path.with_name(sqlite_path.name + suffix)
        if p.exists():
            p.unlink()
    return molt_count, boundary_line


def _time(fn, *, repeats: int) -> tuple[float, object]:
    """Return (best_ms, last_result) over ``repeats`` runs (best = min)."""
    best = float("inf")
    result = None
    for _ in range(repeats):
        t0 = time.perf_counter()
        result = fn()
        dt = (time.perf_counter() - t0) * 1000.0
        best = min(best, dt)
    return best, result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agent-dir", type=Path, default=None,
                    help="Benchmark a real agent dir instead of a synthetic source.")
    ap.add_argument("--events", type=int, default=200_000,
                    help="Synthetic total event count.")
    ap.add_argument("--molt-every", type=int, default=4000,
                    help="Synthetic molt boundary cadence (events per molt).")
    ap.add_argument("--repeats", type=int, default=5,
                    help="Timed repeats per tier (best time reported).")
    args = ap.parse_args()

    tmp = None
    if args.agent_dir is not None:
        agent_dir = args.agent_dir.resolve()
        events_path = agent_dir / "logs" / "events.jsonl"
        if not events_path.is_file():
            print(f"error: {events_path} not found", file=sys.stderr)
            return 2
        # molt_count: best-effort from the last psyche_molt in the file tail.
        molt_count = _last_molt_count(events_path)
        boundary_line = None
        line_count = sum(1 for _ in open(events_path, "rb"))
    else:
        tmp = Path(tempfile.mkdtemp(prefix="bench-agent-session-"))
        agent_dir = tmp
        molt_count, boundary_line = _gen_synthetic_agent_dir(
            agent_dir, total_events=args.events, molt_every=args.molt_every
        )
        events_path = agent_dir / "logs" / "events.jsonl"
        line_count = args.events

    events_bytes = events_path.stat().st_size
    print("=" * 72)
    print("agent-session rebuild benchmark")
    print("=" * 72)
    print(f"agent_dir       : {agent_dir}")
    print(f"events.jsonl    : {events_bytes/1e6:.2f} MB, {line_count} lines")
    print(f"current molt    : {molt_count}"
          + (f" (boundary at line ~{boundary_line})" if boundary_line else ""))
    print(f"repeats         : {args.repeats} (reporting best/min)")
    print("-" * 72)

    # Tier 1 — indexed sqlite (the optimized normal path).
    t1_ms, t1 = _time(
        lambda: rebuild_agent_session_from_events(agent_dir, molt_count=molt_count),
        repeats=args.repeats,
    )
    # Tier 2 — bounded reverse scan (sidecar-absent fallback).
    t2_ms, t2 = _time(
        lambda: _rebuild_via_reverse_scan(events_path, molt_count),
        repeats=args.repeats,
    )
    # Tier 3 — full forward scan (explicit last resort).
    t3_ms, t3 = _time(
        lambda: _rebuild_via_full_scan(events_path, molt_count),
        repeats=args.repeats,
    )

    def _row(name: str, ms: float, sess) -> None:
        scanned = getattr(sess, "rebuild_events_scanned", "?") if sess else "?"
        tier = getattr(sess, "rebuild_tier", "?") if sess else "?"
        print(f"{name:<26} {ms:9.3f} ms   events_scanned={scanned:<8} tier={tier}")

    _row("Tier 1 (sqlite indexed)", t1_ms, t1)
    _row("Tier 2 (reverse scan)", t2_ms, t2)
    _row("Tier 3 (full scan)", t3_ms, t3)
    print("-" * 72)
    if t1_ms > 0:
        print(f"speedup Tier1 vs Tier3 : {t3_ms / t1_ms:.1f}x faster")
    if t2_ms > 0:
        print(f"speedup Tier2 vs Tier3 : {t3_ms / t2_ms:.1f}x faster")
    print("-" * 72)

    # Correctness cross-check: all three tiers must agree on the aggregate.
    agg1 = t1.token_usage() if t1 else None
    agg2 = t2.token_usage() if t2 else None
    agg3 = t3.token_usage() if t3 else None
    agree = agg1 == agg2 == agg3
    print(f"aggregates agree across tiers : {agree}")
    if not agree:
        print(f"  tier1={agg1}")
        print(f"  tier2={agg2}")
        print(f"  tier3={agg3}")
    print("=" * 72)

    if tmp is not None:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    return 0 if agree else 1


def _last_molt_count(events_path: Path) -> int:
    """Scan the tail of a real events file for the latest psyche_molt molt_count."""
    try:
        with open(events_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - 8 * 1024 * 1024)
            f.seek(start)
            data = f.read()
    except OSError:
        return 0
    latest = 0
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict) and (ev.get("type") == MOLT_BOUNDARY_EVENT):
            return int(ev.get("molt_count") or 0)
    return latest


if __name__ == "__main__":
    raise SystemExit(main())
