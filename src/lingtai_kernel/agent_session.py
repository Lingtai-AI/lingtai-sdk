"""Explicit runtime-session and agent-session objects (spec prototype).

Two distinct notions of "session" live in the kernel; this module names them as
first-class objects so code stops confusing them. See the full contract in
``docs/references/runtime-vs-agent-session-objects.md``.

- **runtime session** — the current runtime lifecycle segment. Boundary is
  refresh / restart; on each boundary it is simply a *new empty in-memory
  object*. It has no id, is not rebuilt from events, and grows no product state.
  Its since-refresh token deltas already exist on ``SessionManager`` as the
  ``_session_baseline_*`` view (``get_runtime_session_token_usage``); this object
  only *names* the segment.

- **agent session** — the agent mind segment bounded by ``molt_count`` (the
  existing counter — NO new id). It survives refresh/restart: on start the
  current agent session is *rebuilt* for the current ``molt_count`` from the
  durable trajectory. ``logs/events.jsonl`` is the source of truth for that
  rebuild.

The rebuild MUST NOT full-scan a large ``events.jsonl`` in the normal case. It
uses a three-tier strategy (indexed sqlite → bounded reverse JSONL scan → full
scan last resort). See :func:`rebuild_agent_session_from_events`.

This module is a self-contained prototype: it is unit-testable and benchmarkable
offline (it takes an ``agent_dir`` and an optional injected sqlite-query fn) and
is *not yet* wired into ``base_agent`` lifecycle. See the spec's follow-ups.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "RuntimeSession",
    "AgentSession",
    "MOLT_BOUNDARY_EVENT",
    "TOKEN_EVENT",
    "rebuild_agent_session_from_events",
    "new_runtime_session",
]

# The event type both molt paths emit at a molt boundary, carrying ``molt_count``
# (post-increment). See src/lingtai_kernel/intrinsics/psyche/_molt.py:434,702.
MOLT_BOUNDARY_EVENT = "psyche_molt"

# The event type carrying per-provider-round token usage. See
# src/lingtai_kernel/session.py:608-616 (``llm_response`` with input_tokens/
# output_tokens/thinking_tokens/cached_tokens).
TOKEN_EVENT = "llm_response"

# Bounded reverse-scan limits for Tier 2, mirroring tool_result_recovery.py.
DEFAULT_MAX_SCAN_BYTES = 8 * 1024 * 1024
_REVERSE_READ_CHUNK = 64 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Objects
# ---------------------------------------------------------------------------


@dataclass
class RuntimeSession:
    """The current runtime lifecycle segment.

    No id; a fresh empty object per process start / refresh / restart. It holds
    no token totals of its own — the since-refresh deltas are owned by
    ``SessionManager`` baselines and read via ``get_runtime_session_token_usage``.
    This object only names and time-stamps the current runtime segment.
    """

    started_at: str = field(default_factory=_utc_now_iso)


def new_runtime_session() -> RuntimeSession:
    """Return a fresh empty runtime session (called at each runtime boundary)."""
    return RuntimeSession()


@dataclass
class AgentSession:
    """The agent mind segment bounded by ``molt_count``.

    Identity is ``molt_count`` (NOT a new id). The token aggregate is the
    since-molt sum rebuilt from the durable trajectory; its shape matches the
    injected ``_meta.tool_meta.token_usage.session`` half so that consumer can
    read this object instead of recomputing.
    """

    molt_count: int
    started_at: str | None = None
    boundary_source: str = "boot"  # "agent" | "system" | "boot"
    boundary_offset: int | None = None
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    thinking_tokens: int = 0
    rebuild_tier: str = "none"  # "sqlite" | "reverse_scan" | "full_scan" | "none"
    rebuild_events_scanned: int = 0

    @property
    def cache_miss_tokens(self) -> int:
        return max(self.input_tokens - self.cached_tokens, 0)

    @property
    def cache_rate(self) -> float:
        if self.input_tokens <= 0:
            return 0.0
        return round(min(self.cached_tokens / self.input_tokens, 1.0), 5)

    @property
    def avg_input_tokens_per_api_call(self) -> int:
        if self.api_calls <= 0:
            return 0
        return int(round(self.input_tokens / self.api_calls))

    def token_usage(self) -> dict[str, Any]:
        """Since-molt aggregate in the injected ``token_usage.session`` shape."""
        return {
            "session_cache_rate": self.cache_rate,
            "api_calls": self.api_calls,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "avg_input_tokens_per_api_call": self.avg_input_tokens_per_api_call,
        }


# ---------------------------------------------------------------------------
# Rebuild — three tiers
# ---------------------------------------------------------------------------

SqliteQueryFn = Callable[[str], list[dict[str, Any]]]


def rebuild_agent_session_from_events(
    agent_dir: Path | str,
    *,
    molt_count: int,
    sqlite_query_fn: SqliteQueryFn | None = None,
    events_path: Path | str | None = None,
    sqlite_path: Path | str | None = None,
    allow_full_scan: bool = True,
    logger_fn: Callable[..., None] | None = None,
) -> AgentSession:
    """Rebuild the current agent session for ``molt_count`` from the trajectory.

    Preference order (see spec §4):

    1. **sqlite** — indexed queries against the live ``logs/log.sqlite`` sidecar.
       Two indexed lookups (latest boundary, then aggregate since boundary), no
       scan. Used when a query fn is available (injected or resolved from
       ``sqlite_path``/``agent_dir``) and the sidecar exists.
    2. **reverse_scan** — bounded tail-first scan of ``events.jsonl`` back to the
       current molt boundary. Reads only the current molt generation, not the
       whole file.
    3. **full_scan** — forward scan of the whole ``events.jsonl``. Explicit last
       resort only; logs ``agent_session_rebuild_fullscan``.

    Returns an :class:`AgentSession`. Never raises for missing sources — a
    fresh/empty trajectory yields a zeroed session at ``molt_count``.
    """
    agent_dir = Path(agent_dir)
    events_file = (
        Path(events_path)
        if events_path is not None
        else agent_dir / "logs" / "events.jsonl"
    )

    resolved_query = sqlite_query_fn or _resolve_sqlite_query_fn(
        agent_dir, sqlite_path
    )
    if resolved_query is not None:
        session = _rebuild_via_sqlite(resolved_query, molt_count)
        if session is not None:
            return session

    if events_file.is_file():
        session = _rebuild_via_reverse_scan(events_file, molt_count)
        if session is not None:
            return session
        if allow_full_scan:
            if logger_fn is not None:
                logger_fn(
                    "agent_session_rebuild_fullscan",
                    molt_count=molt_count,
                    reason="no_boundary_found_in_bounded_reverse_scan",
                )
            return _rebuild_via_full_scan(events_file, molt_count)

    # Nothing to rebuild from — a brand-new agent.
    return AgentSession(molt_count=molt_count, boundary_source="boot")


def _resolve_sqlite_query_fn(
    agent_dir: Path, sqlite_path: Path | str | None
) -> SqliteQueryFn | None:
    """Return a read-only query fn bound to the agent's sqlite sidecar, or None.

    Uses the kernel's existing ``query_sqlite_event_index`` read-only path
    (services/logging.py), which opens the sidecar read-only and only accepts
    SELECT/WITH/EXPLAIN. Returns None when the sidecar file is absent.
    """
    target = (
        Path(sqlite_path)
        if sqlite_path is not None
        else agent_dir / "logs" / "log.sqlite"
    )
    if not target.is_file():
        return None
    try:
        from .services.logging import query_sqlite_event_index
    except Exception:
        return None

    def _query(sql: str) -> list[dict[str, Any]]:
        return query_sqlite_event_index(agent_dir, sql, sqlite_path=target)

    return _query


def _rebuild_via_sqlite(
    query: SqliteQueryFn, molt_count: int
) -> AgentSession | None:
    """Tier 1: two indexed queries against ``log.sqlite``. No scan."""
    try:
        boundary_rows = query(
            "SELECT ts, source_offset, fields_json FROM events "
            f"WHERE type = '{MOLT_BOUNDARY_EVENT}' "
            "ORDER BY ts DESC LIMIT 1"
        )
    except Exception:
        return None

    boundary_ts = 0.0
    boundary_offset = None
    boundary_source = "boot"
    if boundary_rows:
        row = boundary_rows[0]
        boundary_ts = float(row.get("ts") or 0.0)
        boundary_offset = row.get("source_offset")
        boundary_source = _boundary_source_from_fields(row.get("fields_json"))

    try:
        agg = query(
            "SELECT "
            "COUNT(*) AS n, "
            "COALESCE(SUM(CAST(json_extract(fields_json,'$.input_tokens') AS INTEGER)),0) AS input_tokens, "
            "COALESCE(SUM(CAST(json_extract(fields_json,'$.output_tokens') AS INTEGER)),0) AS output_tokens, "
            "COALESCE(SUM(CAST(json_extract(fields_json,'$.cached_tokens') AS INTEGER)),0) AS cached_tokens, "
            "COALESCE(SUM(CAST(json_extract(fields_json,'$.thinking_tokens') AS INTEGER)),0) AS thinking_tokens "
            "FROM events "
            f"WHERE type = '{TOKEN_EVENT}' AND ts >= {boundary_ts}"
        )
    except Exception:
        return None

    row = agg[0] if agg else {}
    return AgentSession(
        molt_count=molt_count,
        started_at=_iso_from_ts(boundary_ts) if boundary_ts else None,
        boundary_source=boundary_source,
        boundary_offset=boundary_offset,
        api_calls=int(row.get("n") or 0),
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        cached_tokens=int(row.get("cached_tokens") or 0),
        thinking_tokens=int(row.get("thinking_tokens") or 0),
        rebuild_tier="sqlite",
        rebuild_events_scanned=int(row.get("n") or 0),
    )


def _rebuild_via_reverse_scan(
    events_file: Path, molt_count: int, *, max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES
) -> AgentSession | None:
    """Tier 2: bounded tail-first scan back to the current molt boundary.

    Returns None (so the caller can fall through to Tier 3) when the boundary is
    not found within ``max_scan_bytes`` of the tail — this can only happen when
    the current molt generation itself is larger than the bound, which is the
    rare case Tier 3 exists for.
    """
    try:
        lines, hit_start = _read_tail_lines(events_file, max_scan_bytes)
    except OSError:
        return None

    input_tokens = output_tokens = cached_tokens = thinking_tokens = 0
    api_calls = 0
    scanned = 0
    boundary_ts = None
    boundary_source = "boot"
    found_boundary = False

    # Walk newest → oldest, accumulate token events, stop at the boundary.
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        scanned += 1
        etype = ev.get("type") or ev.get("event")
        if etype == MOLT_BOUNDARY_EVENT:
            found_boundary = True
            boundary_ts = ev.get("ts")
            boundary_source = ev.get("initiator") or "agent"
            break
        if etype == TOKEN_EVENT:
            api_calls += 1
            input_tokens += _int(ev.get("input_tokens"))
            output_tokens += _int(ev.get("output_tokens"))
            cached_tokens += _int(ev.get("cached_tokens"))
            thinking_tokens += _int(ev.get("thinking_tokens"))

    # If we consumed the whole readable tail without a boundary AND we did not
    # reach the start of the file, the boundary is deeper than the bound: defer
    # to Tier 3. If we DID reach file start with no boundary, this agent has
    # simply never molted (molt_count 0) — that's a valid boot session.
    if not found_boundary and not hit_start:
        return None

    return AgentSession(
        molt_count=molt_count,
        started_at=str(boundary_ts) if boundary_ts is not None else None,
        boundary_source=boundary_source,
        boundary_offset=None,
        api_calls=api_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        thinking_tokens=thinking_tokens,
        rebuild_tier="reverse_scan",
        rebuild_events_scanned=scanned,
    )


def _rebuild_via_full_scan(events_file: Path, molt_count: int) -> AgentSession:
    """Tier 3: forward scan the whole file, keeping the last molt generation.

    Explicit last resort. Resets the running aggregate at every boundary so the
    final aggregate reflects only the current (last) molt generation.
    """
    input_tokens = output_tokens = cached_tokens = thinking_tokens = 0
    api_calls = 0
    scanned = 0
    boundary_ts = None
    boundary_source = "boot"
    try:
        with open(events_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                scanned += 1
                etype = ev.get("type") or ev.get("event")
                if etype == MOLT_BOUNDARY_EVENT:
                    # New generation begins here — reset the aggregate.
                    input_tokens = output_tokens = cached_tokens = thinking_tokens = 0
                    api_calls = 0
                    boundary_ts = ev.get("ts")
                    boundary_source = ev.get("initiator") or "agent"
                elif etype == TOKEN_EVENT:
                    api_calls += 1
                    input_tokens += _int(ev.get("input_tokens"))
                    output_tokens += _int(ev.get("output_tokens"))
                    cached_tokens += _int(ev.get("cached_tokens"))
                    thinking_tokens += _int(ev.get("thinking_tokens"))
    except OSError:
        pass

    return AgentSession(
        molt_count=molt_count,
        started_at=str(boundary_ts) if boundary_ts is not None else None,
        boundary_source=boundary_source,
        boundary_offset=None,
        api_calls=api_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        thinking_tokens=thinking_tokens,
        rebuild_tier="full_scan",
        rebuild_events_scanned=scanned,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_tail_lines(path: Path, max_bytes: int) -> tuple[list[str], bool]:
    """Read up to ``max_bytes`` from the tail of ``path``, split into lines.

    Returns ``(lines, hit_start)`` where ``hit_start`` is True when the read
    reached byte 0 (i.e. the whole file fit in ``max_bytes``). The first
    (partial) line is dropped when we did not reach the start, since a tail read
    may begin mid-line.
    """
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    hit_start = start == 0
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not hit_start and lines:
        # Drop the possibly-truncated first line.
        lines = lines[1:]
    return lines, hit_start


def _boundary_source_from_fields(fields_json: Any) -> str:
    if not fields_json:
        return "agent"
    try:
        fields = json.loads(fields_json) if isinstance(fields_json, str) else fields_json
    except (json.JSONDecodeError, ValueError):
        return "agent"
    if isinstance(fields, dict):
        return str(fields.get("initiator") or "agent")
    return "agent"


def _iso_from_ts(ts: float) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
