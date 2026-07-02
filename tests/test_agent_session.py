"""Tests for the explicit runtime-session / agent-session objects (spec prototype).

See docs/references/runtime-vs-agent-session-objects.md. These cover:
- RuntimeSession has no id and is a fresh object each call.
- AgentSession is keyed by molt_count (no new id) and its derived token views.
- rebuild_agent_session_from_events across all three tiers, agreeing on the
  since-molt aggregate, and demonstrably NOT full-scanning in the normal case.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lingtai_kernel.agent_session import (
    MOLT_BOUNDARY_EVENT,
    TOKEN_EVENT,
    AgentSession,
    RuntimeSession,
    _rebuild_via_full_scan,
    _rebuild_via_reverse_scan,
    new_runtime_session,
    rebuild_agent_session_from_events,
)


# ---------------------------------------------------------------------------
# Object contracts
# ---------------------------------------------------------------------------


def test_runtime_session_has_no_id_and_is_fresh_each_time():
    a = new_runtime_session()
    b = new_runtime_session()
    assert isinstance(a, RuntimeSession)
    # No id attribute exists on the object — the contract forbids one.
    assert not hasattr(a, "id")
    assert not hasattr(a, "session_id")
    # Distinct objects per boundary.
    assert a is not b
    assert a.started_at  # timestamped


def test_agent_session_keyed_by_molt_count_no_new_id():
    s = AgentSession(molt_count=7)
    assert s.molt_count == 7
    assert not hasattr(s, "id")
    assert not hasattr(s, "agent_session_id")


def test_agent_session_derived_token_views():
    s = AgentSession(
        molt_count=3, api_calls=4, input_tokens=1000, cached_tokens=600,
        output_tokens=200,
    )
    assert s.cache_miss_tokens == 400
    assert s.cache_rate == 0.6
    assert s.avg_input_tokens_per_api_call == 250
    usage = s.token_usage()
    assert usage["api_calls"] == 4
    assert usage["input_tokens"] == 1000
    assert usage["cached_tokens"] == 600
    assert usage["cache_miss_tokens"] == 400
    assert usage["session_cache_rate"] == 0.6


def test_agent_session_zero_division_safe():
    s = AgentSession(molt_count=0)
    assert s.cache_rate == 0.0
    assert s.avg_input_tokens_per_api_call == 0
    assert s.cache_miss_tokens == 0


# ---------------------------------------------------------------------------
# Rebuild fixtures
# ---------------------------------------------------------------------------


def _write_events(dest: Path, events: list[dict]) -> Path:
    logs = dest / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "events.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return path


def _token_ev(ts: float, inp: int, out: int, cached: int) -> dict:
    return {
        "type": TOKEN_EVENT, "ts": ts,
        "input_tokens": inp, "output_tokens": out,
        "thinking_tokens": 10, "cached_tokens": cached,
    }


def _molt_ev(ts: float, molt_count: int, initiator: str = "agent") -> dict:
    return {
        "type": MOLT_BOUNDARY_EVENT, "ts": ts,
        "molt_count": molt_count, "initiator": initiator,
    }


# ---------------------------------------------------------------------------
# Tier 2 & 3 (JSONL only)
# ---------------------------------------------------------------------------


def test_reverse_and_full_scan_agree_after_molt(tmp_path):
    # Two generations; current (molt_count=2) has 3 token events after boundary.
    events = [
        _token_ev(1.0, 500, 100, 300),   # gen 1
        _token_ev(2.0, 500, 100, 300),   # gen 1
        _molt_ev(3.0, 1),                # boundary -> gen 2 (molt_count 1)
        _token_ev(4.0, 1000, 200, 700),  # gen 2
        _molt_ev(5.0, 2),                # boundary -> gen 3 (molt_count 2)
        _token_ev(6.0, 1000, 200, 700),  # current
        _token_ev(7.0, 1000, 200, 700),  # current
        _token_ev(8.0, 1000, 200, 700),  # current
    ]
    events_path = _write_events(tmp_path, events)

    rev = _rebuild_via_reverse_scan(events_path, molt_count=2)
    full = _rebuild_via_full_scan(events_path, molt_count=2)

    assert rev.api_calls == 3
    assert rev.input_tokens == 3000
    assert rev.cached_tokens == 2100
    assert rev.boundary_source == "agent"
    # Only the current generation was aggregated, not the earlier ones.
    assert rev.token_usage() == full.token_usage()
    assert rev.rebuild_tier == "reverse_scan"
    assert full.rebuild_tier == "full_scan"


def test_never_molted_agent_yields_boot_session(tmp_path):
    events = [
        _token_ev(1.0, 500, 100, 300),
        _token_ev(2.0, 500, 100, 300),
    ]
    events_path = _write_events(tmp_path, events)
    rev = _rebuild_via_reverse_scan(events_path, molt_count=0)
    assert rev is not None
    assert rev.api_calls == 2
    assert rev.input_tokens == 1000
    assert rev.boundary_source == "boot"


def test_system_forced_molt_boundary_source(tmp_path):
    events = [
        _molt_ev(1.0, 1, initiator="system"),
        _token_ev(2.0, 1000, 200, 700),
    ]
    events_path = _write_events(tmp_path, events)
    rev = _rebuild_via_reverse_scan(events_path, molt_count=1)
    assert rev.boundary_source == "system"
    assert rev.api_calls == 1


# ---------------------------------------------------------------------------
# Tier 1 (sqlite) end-to-end, and cross-tier agreement
# ---------------------------------------------------------------------------


def _build_sqlite(agent_dir: Path, events: list[dict]) -> None:
    """Build logs/log.sqlite from the events, matching production wiring."""
    from lingtai_kernel.services.logging import SQLiteEventIndex

    events_path = agent_dir / "logs" / "events.jsonl"
    sqlite_path = agent_dir / "logs" / "log.sqlite"
    index = SQLiteEventIndex(sqlite_path)
    conn = index._ensure_open()
    with index._lock:
        conn.execute("BEGIN")
        offset = 0
        for i, ev in enumerate(events):
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
            offset += len(json.dumps(ev, ensure_ascii=False)) + 1
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    index.close()
    for suffix in ("-wal", "-shm"):
        p = sqlite_path.with_name(sqlite_path.name + suffix)
        if p.exists():
            p.unlink()


def test_all_three_tiers_agree(tmp_path):
    events = [
        _token_ev(1.0, 500, 100, 300),
        _molt_ev(2.0, 1),
        _token_ev(3.0, 1000, 200, 700),
        _molt_ev(4.0, 2),
        _token_ev(5.0, 1234, 200, 800),
        _token_ev(6.0, 1234, 200, 800),
    ]
    events_path = _write_events(tmp_path, events)
    _build_sqlite(tmp_path, events)

    # Tier 1 via the public entry (sidecar present -> sqlite path chosen).
    t1 = rebuild_agent_session_from_events(tmp_path, molt_count=2)
    assert t1.rebuild_tier == "sqlite"

    t2 = _rebuild_via_reverse_scan(events_path, molt_count=2)
    t3 = _rebuild_via_full_scan(events_path, molt_count=2)

    assert t1.token_usage() == t2.token_usage() == t3.token_usage()
    assert t1.api_calls == 2
    assert t1.input_tokens == 2468
    assert t1.cached_tokens == 1600


def test_public_entry_falls_back_to_reverse_scan_without_sidecar(tmp_path):
    events = [
        _molt_ev(1.0, 1),
        _token_ev(2.0, 1000, 200, 700),
    ]
    _write_events(tmp_path, events)  # no sqlite built
    session = rebuild_agent_session_from_events(tmp_path, molt_count=1)
    assert session.rebuild_tier == "reverse_scan"
    assert session.api_calls == 1


def test_missing_trajectory_yields_boot_session(tmp_path):
    session = rebuild_agent_session_from_events(tmp_path, molt_count=0)
    assert session.rebuild_tier == "none"
    assert session.boundary_source == "boot"
    assert session.api_calls == 0


def test_reverse_scan_defers_to_full_scan_when_boundary_beyond_bound(tmp_path):
    # A current generation larger than the tiny byte bound: the boundary is not
    # reachable in the bounded reverse scan, so Tier 2 returns None (defer).
    events = [_molt_ev(1.0, 1)] + [
        _token_ev(float(i + 2), 1000, 200, 700) for i in range(50)
    ]
    events_path = _write_events(tmp_path, events)
    # Bound smaller than the generation forces the defer path.
    deferred = _rebuild_via_reverse_scan(events_path, molt_count=1, max_scan_bytes=200)
    assert deferred is None
    # Full scan still recovers the correct aggregate.
    full = _rebuild_via_full_scan(events_path, molt_count=1)
    assert full.api_calls == 50
