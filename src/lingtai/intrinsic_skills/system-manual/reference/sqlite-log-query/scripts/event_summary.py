#!/usr/bin/env python3
"""
event_summary.py — Summarize a LingTai log.sqlite sidecar file.

Read-only, safe, no network requests, no side effects, no secrets required.
Queries the SQLite sidecar to produce a structured summary of runtime events.

Usage:
    python3 event_summary.py <log.sqlite> [options]

Examples:
    python3 event_summary.py .lingtai/agent/logs/log.sqlite
    python3 event_summary.py .lingtai/agent/logs/log.sqlite --hours 24
    python3 event_summary.py .lingtai/agent/logs/log.sqlite --source-kind daemon_events
    python3 event_summary.py .lingtai/agent/logs/log.sqlite --format json
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path


# --- Redaction helpers ---

SECRET_RE = re.compile(
    r'(token|key|secret|password|credential|oauth|bearer)[":=\s]+[^\s",]{8,}',
    re.IGNORECASE,
)
PATH_RE = re.compile(r'/Users/[^/]+/')
IP_RE = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')


def redact(text: str) -> str:
    """Redact obvious secrets, user paths, and IP addresses."""
    text = SECRET_RE.sub(r'\1=[REDACTED]', text)
    text = PATH_RE.sub('/Users/[USER]/', text)
    text = IP_RE.sub('[HOST]', text)
    return text


# --- SQL queries ---

_Q_EVENT_TYPE_COUNTS = """
SELECT type, COUNT(*) AS n
FROM events
{where}
GROUP BY type
ORDER BY n DESC
LIMIT 30;
"""

_Q_SOURCE_KIND_BREAKDOWN = """
SELECT source_kind, scope, COUNT(*) AS n,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
{where}
GROUP BY source_kind, scope
ORDER BY n DESC;
"""

_Q_TOOL_CALLS = """
SELECT
    json_extract(fields_json, '$.tool') AS tool,
    json_extract(fields_json, '$.name') AS tool_name,
    type,
    COUNT(*) AS n
FROM events
{where}
  AND type LIKE 'tool_%'
GROUP BY tool, tool_name, type
ORDER BY n DESC
LIMIT 20;
"""

_Q_ERROR_CLUSTERS = """
SELECT
    substr(json_extract(fields_json, '$.error'), 1, 120) AS error,
    COUNT(*) AS n
FROM events
{where}
  AND (fields_json LIKE '%"error"%')
GROUP BY error
ORDER BY n DESC
LIMIT 20;
"""

_Q_LATENCY_GAPS = """
WITH ordered AS (
    SELECT ts, type,
           ts - LAG(ts) OVER (ORDER BY ts) AS gap_seconds
    FROM events
    WHERE ts > 0 {extra_where}
)
SELECT ts, type, ROUND(gap_seconds, 1) AS gap_seconds
FROM ordered
WHERE gap_seconds > 30
ORDER BY gap_seconds DESC
LIMIT 30;
"""

_Q_CONTEXT_PRESSURE = """
SELECT COUNT(*) AS n
FROM events
{where}
  AND (type LIKE '%context%'
       OR type LIKE '%pressure%'
       OR type LIKE '%molt%'
       OR type LIKE '%spill%'
       OR type LIKE '%overflow%');
"""

_Q_DAEMON_LIFECYCLE = """
SELECT run_id, type, COUNT(*) AS n,
       MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM events
{where}
  AND source_kind = 'daemon_events'
GROUP BY run_id, type
ORDER BY run_id, n DESC
LIMIT 50;
"""

_Q_SCHEMA_KEYS = """
SELECT json_each.key, COUNT(*) AS n
FROM events,
     json_each(events.fields_json)
{where}
GROUP BY json_each.key
ORDER BY n DESC
LIMIT 30;
"""

_Q_TOTAL_COUNT = "SELECT COUNT(*) FROM events {where};"

_Q_TIME_RANGE = "SELECT MIN(ts), MAX(ts) FROM events {where} AND ts > 0;"


def _build_where(source_kind: str | None, hours: float | None) -> tuple[str, str]:
    """Return (where_clause, extra_where_clause) with optional filters.

    The where clause always starts with ``WHERE 1=1`` so query templates can
    safely append ``AND ...`` conditions. The extra_where clause starts with
    ``AND`` for embedding inside already-filtered subqueries.
    """
    clauses: list[str] = ["1=1"]
    extra: list[str] = []
    if source_kind:
        source_kind_sql = source_kind.replace("'", "''")
        clauses.append(f"source_kind = '{source_kind_sql}'")
        extra.append(f"AND source_kind = '{source_kind_sql}'")
    if hours is not None:
        import time
        cutoff = time.time() - hours * 3600
        clauses.append(f"ts >= {cutoff}")
        extra.append(f"AND ts >= {cutoff}")
    where = "WHERE " + " AND ".join(clauses)
    extra_where = " ".join(extra)
    return where, extra_where


def _query(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """Execute a query and return all rows."""
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as e:
        return [("QUERY_ERROR", str(e))]


def summarize(db_path: str, source_kind: str | None = None,
              hours: float | None = None) -> dict:
    """Produce a structured summary of a LingTai log.sqlite sidecar."""
    if not os.path.isfile(db_path):
        return {"error": f"File not found: {db_path}"}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        return {"error": f"Cannot open database: {e}"}

    conn.row_factory = sqlite3.Row
    where, extra_where = _build_where(source_kind, hours)

    result: dict = {
        "db_path": db_path,
        "source_kind_filter": source_kind,
        "hours_filter": hours,
    }

    # Total count
    rows = _query(conn, _Q_TOTAL_COUNT.format(where=where))
    result["total_events"] = rows[0][0] if rows else 0

    # Time range
    rows = _query(conn, _Q_TIME_RANGE.format(where=where))
    if rows and rows[0][0] is not None:
        result["time_range"] = {"earliest": rows[0][0], "latest": rows[0][1]}
    else:
        result["time_range"] = None

    # Event type counts
    rows = _query(conn, _Q_EVENT_TYPE_COUNTS.format(where=where))
    result["event_type_counts"] = [
        {"type": r[0], "count": r[1]} for r in rows if len(r) == 2
    ]

    # Source kind breakdown
    rows = _query(conn, _Q_SOURCE_KIND_BREAKDOWN.format(where=where))
    result["source_kind_breakdown"] = [
        {"source_kind": r[0], "scope": r[1], "count": r[2],
         "first_ts": r[3], "last_ts": r[4]}
        for r in rows if len(r) == 5
    ]

    # Tool calls
    rows = _query(conn, _Q_TOOL_CALLS.format(where=where))
    result["tool_calls"] = [
        {"tool": r[0], "tool_name": r[1], "type": r[2], "count": r[3]}
        for r in rows if len(r) == 4
    ]

    # Error clusters
    rows = _query(conn, _Q_ERROR_CLUSTERS.format(where=where))
    result["error_clusters"] = [
        {"error": redact(str(r[0])) if r[0] else None, "count": r[1]}
        for r in rows if len(r) == 2
    ]

    # Latency gaps
    rows = _query(conn, _Q_LATENCY_GAPS.format(extra_where=extra_where))
    result["latency_gaps"] = [
        {"ts": r[0], "type": r[1], "gap_seconds": r[2]}
        for r in rows if len(r) == 3
    ]

    # Context pressure count
    rows = _query(conn, _Q_CONTEXT_PRESSURE.format(where=where))
    result["context_pressure_events"] = rows[0][0] if rows else 0

    # Daemon lifecycle (if daemon events exist)
    rows = _query(conn, _Q_DAEMON_LIFECYCLE.format(where=where))
    result["daemon_lifecycle"] = [
        {"run_id": r[0], "type": r[1], "count": r[2],
         "first_ts": r[3], "last_ts": r[4]}
        for r in rows if len(r) == 5
    ]

    # Schema key discovery
    rows = _query(conn, _Q_SCHEMA_KEYS.format(where=where))
    result["schema_keys"] = [
        {"key": r[0], "count": r[1]}
        for r in rows if len(r) == 2
    ]

    conn.close()
    return result


def _print_text(summary: dict) -> None:
    """Pretty-print the summary in human-readable text."""
    if "error" in summary:
        print(f"ERROR: {summary['error']}", file=sys.stderr)
        return

    print(f"=== Event Summary: {summary['db_path']} ===")
    if summary["source_kind_filter"]:
        print(f"  Filter: source_kind = {summary['source_kind_filter']}")
    if summary["hours_filter"]:
        print(f"  Filter: last {summary['hours_filter']} hours")
    print(f"  Total events: {summary['total_events']}")

    tr = summary.get("time_range")
    if tr:
        print(f"  Time range: {tr['earliest']} — {tr['latest']}")

    print("\n--- Source kind breakdown ---")
    for item in summary["source_kind_breakdown"]:
        print(f"  {item['source_kind']} ({item['scope']}): {item['count']} events")

    print("\n--- Event type counts ---")
    for item in summary["event_type_counts"]:
        print(f"  {item['count']:>6}  {item['type']}")

    if summary["tool_calls"]:
        print("\n--- Tool calls ---")
        for item in summary["tool_calls"]:
            label = item["tool_name"] or item["tool"] or "?"
            print(f"  {item['count']:>6}  {label}  ({item['type']})")

    if summary["error_clusters"]:
        print("\n--- Error clusters ---")
        for item in summary["error_clusters"]:
            err = (item["error"] or "")[:100]
            print(f"  {item['count']:>6}  {err}")

    if summary["latency_gaps"]:
        print("\n--- Latency gaps (> 30s) ---")
        for item in summary["latency_gaps"][:10]:
            print(f"  {item['gap_seconds']:>8.1f}s gap  at ts={item['ts']}  ({item['type']})")

    print(f"\n--- Context/pressure events: {summary['context_pressure_events']} ---")

    if summary["daemon_lifecycle"]:
        print("\n--- Daemon lifecycle ---")
        for item in summary["daemon_lifecycle"]:
            print(f"  {item['run_id']}  {item['type']}: {item['count']}")

    if summary["schema_keys"]:
        print("\n--- Top schema keys in fields_json ---")
        for item in summary["schema_keys"][:15]:
            print(f"  {item['count']:>6}  {item['key']}")


def main():
    parser = argparse.ArgumentParser(
        description="Summarize a LingTai log.sqlite sidecar (read-only, safe).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "db_path",
        help="Path to the log.sqlite file",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Only include events from the last N hours",
    )
    parser.add_argument(
        "--source-kind",
        default=None,
        help="Filter to a specific source_kind (e.g., daemon_events, agent_events)",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )

    args = parser.parse_args()

    summary = summarize(
        db_path=args.db_path,
        source_kind=args.source_kind,
        hours=args.hours,
    )

    if args.format == "json":
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_text(summary)


if __name__ == "__main__":
    main()
