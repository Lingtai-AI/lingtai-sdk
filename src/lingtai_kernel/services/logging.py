"""LoggingService — structured event logging backing agent observability.

The primary durable event log remains ``logs/events.jsonl``.  SQLite support is
implemented as an additive, rebuildable sidecar index: JSONL is the source of
truth; ``logs/log.sqlite`` exists to make history queryable.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..workdir import WorkingDir


SCHEMA_VERSION = 2
DEFAULT_SQLITE_NAME = "log.sqlite"
DEFAULT_JSONL_NAME = "events.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_ts(raw_ts: Any) -> tuple[float, bool]:
    """Return ``(numeric_ts, ok)`` for epoch-ish numbers or ISO strings."""
    if raw_ts is None or raw_ts == "":
        return 0.0, True
    try:
        return float(raw_ts), True
    except (TypeError, ValueError):
        pass
    if isinstance(raw_ts, str):
        try:
            parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp(), True
        except ValueError:
            pass
    return 0.0, False


def _extract_content_text(value: Any) -> str | None:
    """Best-effort plain-text extraction from chat-history content shapes."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _extract_content_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        block_type = value.get("type")
        if block_type == "tool_call":
            parts = ["tool_call"]
            if value.get("name") is not None:
                parts.append(str(value["name"]))
            if value.get("args") is not None:
                parts.append(json.dumps(value["args"], ensure_ascii=False, default=str))
            return " ".join(parts)
        if block_type == "tool_result":
            parts = ["tool_result"]
            if value.get("name") is not None:
                parts.append(str(value["name"]))
            nested = _extract_content_text(value.get("content"))
            if nested:
                parts.append(nested)
            return " ".join(parts)

        parts: list[str] = []
        for key in ("text", "content", "system"):
            nested = _extract_content_text(value.get(key))
            if nested:
                parts.append(nested)
        return "\n".join(parts) if parts else None
    return str(value)


class LoggingService(ABC):
    """Abstract structured event logging service.

    Backs agent observability. Implementations provide the actual storage
    mechanism (JSONL file, database, network sink, etc.).
    """

    @abstractmethod
    def log(self, event: dict) -> dict | None:
        """Log a structured event. Must be thread-safe.

        Implementations may return storage metadata (for example JSONL source
        offsets). Callers should treat the return value as optional.
        """

    def close(self) -> None:
        """Flush and release resources. Default no-op."""


class JSONLLoggingService(LoggingService):
    """Append structured events as JSON lines to a file.

    Thread-safe via lock. Flushes after every write for real-time tailing.
    """

    def __init__(self, path: Path | str, *, ensure_ascii: bool = False) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "ab")
        self._lock = threading.RLock()
        self._ensure_ascii = ensure_ascii
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def log(self, event: dict) -> dict | None:
        """Append event as one JSON line and return JSONL source metadata."""
        if self._closed:
            return None
        line = json.dumps(event, ensure_ascii=self._ensure_ascii, default=str)
        payload = (line + "\n").encode("utf-8")
        with self._lock:
            source_offset = self._file.tell()
            self._file.write(payload)
            self._file.flush()
        return {"source_file": str(self._path), "source_offset": source_offset}

    def get_events(self) -> list[dict]:
        """Read all events from the JSONL file. Thread-safe."""
        with self._lock:
            if not self._path.exists():
                return []
            events = []
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return events

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._file.close()


class SQLiteEventIndex:
    """Best-effort SQLite index for JSONL traces.

    The index is a derived artifact.  It is safe to delete and rebuild from
    JSONL sources.  Runtime writes fail open by disabling the sidecar after
    the first sqlite error; they never raise into the agent turn.
    """

    def __init__(self, path: Path | str, *, ensure: bool = True, keep_open: bool = True) -> None:
        self.path = Path(path)
        self._keep_open = keep_open
        self._lock = threading.RLock()
        self._disabled_reason: str | None = None
        self._conn: sqlite3.Connection | None = None
        if ensure:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._ensure_open()
            except (OSError, sqlite3.Error) as exc:
                # Runtime sidecar creation must fail open.  JSONL remains the
                # source of truth, and callers can rebuild the sidecar later.
                self._disabled_reason = str(exc)
                self.close()

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    def _ensure_open(self, *, read_only: bool = False, ensure_schema: bool = True) -> sqlite3.Connection:
        if self._disabled_reason:
            raise sqlite3.Error(self._disabled_reason)
        if self._conn is None:
            if read_only:
                # Use a WAL-aware read-only connection for live sidecars, but keep
                # immutable inspection for checkpointed/offline sidecars so doctor/query
                # do not create empty -wal/-shm files as a read side effect.
                has_wal_sidecar = self.path.with_name(self.path.name + "-wal").exists() or self.path.with_name(self.path.name + "-shm").exists()
                suffix = "?mode=ro" if has_wal_sidecar else "?mode=ro&immutable=1"
                uri = f"{self.path.resolve().as_uri()}{suffix}"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._conn = conn
            if not read_only:
                self._configure(conn)
            if ensure_schema:
                self._ensure_schema(conn)
        return self._conn

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    @classmethod
    def _ensure_column(cls, conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        if name not in cls._table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @classmethod
    def _ensure_schema(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS import_cursors (
              source_file TEXT PRIMARY KEY,
              byte_offset INTEGER NOT NULL DEFAULT 0,
              line_no INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              type TEXT NOT NULL,
              agent_address TEXT,
              agent_name_snapshot TEXT,
              fields_json TEXT NOT NULL,
              source_file TEXT,
              source_offset INTEGER,
              source_line INTEGER,
              source_kind TEXT,
              scope TEXT,
              run_id TEXT,
              inserted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            """
        )
        # Existing v1 sidecars predate these trace-classification columns.
        for name, definition in (
            ("source_line", "INTEGER"),
            ("source_kind", "TEXT"),
            ("scope", "TEXT"),
            ("run_id", "TEXT"),
        ):
            cls._ensure_column(conn, "events", name, definition)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL DEFAULT 0,
              ts_text TEXT,
              role TEXT,
              kind TEXT,
              turn INTEGER,
              content_text TEXT,
              entry_json TEXT NOT NULL,
              source_file TEXT NOT NULL,
              source_offset INTEGER NOT NULL,
              source_line INTEGER,
              source_kind TEXT,
              scope TEXT,
              run_id TEXT,
              inserted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_source_kind_ts ON events(source_kind, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_events_run_id_ts ON events(run_id, ts DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source_offset
              ON events(source_file, source_offset)
              WHERE source_file IS NOT NULL AND source_offset IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_chat_entries_role_ts ON chat_entries(role, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_entries_source_kind_ts ON chat_entries(source_kind, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_entries_run_id_turn ON chat_entries(run_id, turn);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_entries_source_offset
              ON chat_entries(source_file, source_offset);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (1, "initial_events_index", _utc_now()),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
            (2, "trace_chat_and_daemon_index", _utc_now()),
        )
        conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def disable(self, reason: str) -> None:
        self._disabled_reason = reason
        self.close()

    @staticmethod
    def event_row(
        event: dict,
        *,
        source_file: str | None = None,
        source_offset: int | None = None,
        source_line: int | None = None,
        source_kind: str | None = None,
        scope: str | None = None,
        run_id: str | None = None,
    ) -> tuple[Any, ...]:
        fields = dict(event)
        event_type = fields.pop("type", None)
        if event_type is None:
            event_type = fields.pop("event", "")
        raw_ts = fields.pop("ts", 0.0)
        ts, ok = _coerce_ts(raw_ts)
        if not ok:
            fields["ts_raw"] = raw_ts
        agent_address = fields.pop("address", None)
        agent_name = fields.pop("agent_name", None)
        return (
            ts,
            str(event_type),
            str(agent_address) if agent_address is not None else None,
            str(agent_name) if agent_name is not None else None,
            json.dumps(fields, ensure_ascii=False, default=str),
            source_file,
            source_offset,
            source_line,
            source_kind,
            scope,
            run_id,
        )

    @staticmethod
    def chat_entry_row(
        entry: dict,
        *,
        source_file: str,
        source_offset: int,
        source_line: int,
        source_kind: str,
        scope: str,
        run_id: str | None,
    ) -> tuple[Any, ...]:
        role = entry.get("role")
        kind = entry.get("kind")
        turn_raw = entry.get("turn")
        try:
            turn = int(turn_raw) if turn_raw is not None else None
        except (TypeError, ValueError):
            turn = None
        if "ts" in entry:
            raw_ts = entry.get("ts")
        else:
            raw_ts = entry.get("timestamp")
        ts, _ = _coerce_ts(raw_ts)
        content_text = _extract_content_text(entry.get("text"))
        if not content_text:
            content_text = _extract_content_text(entry.get("content"))
        if not content_text:
            content_text = _extract_content_text(entry.get("system"))
        return (
            ts,
            str(raw_ts) if raw_ts is not None else None,
            str(role) if role is not None else None,
            str(kind) if kind is not None else None,
            turn,
            content_text,
            json.dumps(entry, ensure_ascii=False, default=str),
            source_file,
            source_offset,
            source_line,
            source_kind,
            scope,
            run_id,
        )

    def log_event(
        self,
        event: dict,
        *,
        source_file: str | None = None,
        source_offset: int | None = None,
        source_line: int | None = None,
        source_kind: str | None = "agent_events",
        scope: str | None = "agent",
        run_id: str | None = None,
    ) -> None:
        if self._disabled_reason:
            return
        try:
            with self._lock:
                conn = self._ensure_open()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO events(
                        ts, type, agent_address, agent_name_snapshot, fields_json,
                        source_file, source_offset, source_line, source_kind, scope, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self.event_row(
                        event,
                        source_file=source_file,
                        source_offset=source_offset,
                        source_line=source_line,
                        source_kind=source_kind,
                        scope=scope,
                        run_id=run_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            # The SQLite sidecar is derived from JSONL.  Any sidecar failure —
            # sqlite errors, path errors, or row-normalization surprises — must
            # fail open and never break the agent turn after JSONL succeeded.
            self.disable(str(exc))
        finally:
            if not self._keep_open:
                self.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        statement = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
        if statement not in {"select", "with", "explain"}:
            raise ValueError("log query only accepts read-only SELECT/WITH/EXPLAIN statements")
        with self._lock:
            conn = self._ensure_open(read_only=True, ensure_schema=False)
            conn.execute("PRAGMA query_only=ON")
            try:
                cur = conn.execute(sql, tuple(params))
                if cur.description is None:
                    return []
                return [dict(row) for row in cur.fetchall()]
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA query_only=OFF")

    def doctor(self) -> dict[str, Any]:
        if self._disabled_reason:
            return {"status": "disabled", "path": str(self.path), "reason": self._disabled_reason}
        with self._lock:
            conn = self._ensure_open(read_only=True, ensure_schema=False)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] if "events" in tables else 0
            chat_entry_count = conn.execute("SELECT COUNT(*) FROM chat_entries").fetchone()[0] if "chat_entries" in tables else 0
            cursor_count = conn.execute("SELECT COUNT(*) FROM import_cursors").fetchone()[0] if "import_cursors" in tables else 0
            schema_versions = [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] if "schema_migrations" in tables else []
        return {
            "status": "ok" if integrity == "ok" else "error",
            "path": str(self.path),
            "integrity_check": integrity,
            "event_count": event_count,
            "chat_entry_count": chat_entry_count,
            "cursor_count": cursor_count,
            "schema_versions": schema_versions,
            "schema_current": SCHEMA_VERSION in schema_versions,
        }


class CompositeLoggingService(LoggingService):
    """Primary JSONL logger plus best-effort sidecar indexes."""

    def __init__(self, primary: JSONLLoggingService, *, sqlite_index: SQLiteEventIndex | None = None) -> None:
        self.primary = primary
        self.sqlite_index = sqlite_index

    def log(self, event: dict) -> dict | None:
        metadata = self.primary.log(event)
        # If the primary JSONL write did not happen (for example after close),
        # do not create sidecar-only facts.  JSONL must remain the source of truth.
        if metadata is None:
            return None
        if self.sqlite_index is not None:
            source_file = metadata.get("source_file")
            source_offset = metadata.get("source_offset")
            self.sqlite_index.log_event(event, source_file=source_file, source_offset=source_offset)
        return metadata

    def get_events(self) -> list[dict]:
        return self.primary.get_events()

    def close(self) -> None:
        self.primary.close()
        if self.sqlite_index is not None:
            self.sqlite_index.close()


def _iter_jsonl_records_with_offsets(path: Path) -> Iterable[tuple[dict, int, int, int]]:
    """Yield ``(record, byte_offset, next_offset, line_no)`` from a JSONL file."""
    with open(path, "rb") as f:
        line_no = 0
        while True:
            offset = f.tell()
            raw = f.readline()
            if not raw:
                break
            line_no += 1
            next_offset = f.tell()
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield record, offset, next_offset, line_no


# Backward-compatible name used by older callers/tests.
_iter_jsonl_events_with_offsets = _iter_jsonl_records_with_offsets


def _classify_trace_source(agent_dir: Path, source: Path) -> tuple[str, str, str | None]:
    try:
        rel = source.resolve().relative_to(agent_dir.resolve())
        parts = rel.parts
    except ValueError:
        parts = source.parts
    if parts == ("logs", DEFAULT_JSONL_NAME):
        return "agent_events", "agent", None
    if parts == ("history", "chat_history.jsonl"):
        return "agent_chat", "agent", None
    if parts == ("history", "chat_history_archive.jsonl"):
        return "agent_chat_archive", "agent", None
    if len(parts) >= 4 and parts[0] == "daemons":
        run_id = parts[1]
        if parts[-2:] == ("logs", DEFAULT_JSONL_NAME):
            return "daemon_events", "daemon", run_id
        if parts[-2:] == ("history", "chat_history.jsonl"):
            return "daemon_chat", "daemon", run_id
    if source.name in {DEFAULT_JSONL_NAME, "event.jsonl"}:
        return "events", "unknown", None
    return "chat", "unknown", None


def _discover_trace_sources(agent_dir: Path, jsonl_path: Path | None = None) -> list[Path]:
    if jsonl_path is not None:
        if not jsonl_path.is_file():
            raise FileNotFoundError(f"events JSONL not found: {jsonl_path}")
        return [jsonl_path]
    candidates = [
        agent_dir / "logs" / DEFAULT_JSONL_NAME,
        agent_dir / "history" / "chat_history.jsonl",
        agent_dir / "history" / "chat_history_archive.jsonl",
    ]
    daemons_dir = agent_dir / "daemons"
    if daemons_dir.is_dir():
        candidates.extend(sorted(daemons_dir.glob("*/logs/events.jsonl")))
        candidates.extend(sorted(daemons_dir.glob("*/history/chat_history.jsonl")))
    return [path.resolve() for path in candidates if path.is_file()]


def _import_events_source(
    conn: sqlite3.Connection,
    source: Path,
    *,
    source_kind: str,
    scope: str,
    run_id: str | None,
) -> tuple[int, int, int]:
    count = 0
    last_offset = 0
    last_line = 0
    for event, offset, next_offset, line_no in _iter_jsonl_records_with_offsets(source):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO events(
                ts, type, agent_address, agent_name_snapshot, fields_json,
                source_file, source_offset, source_line, source_kind, scope, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SQLiteEventIndex.event_row(
                event,
                source_file=str(source),
                source_offset=offset,
                source_line=line_no,
                source_kind=source_kind,
                scope=scope,
                run_id=run_id,
            ),
        )
        count += max(cur.rowcount, 0)
        last_offset = next_offset
        last_line = line_no
    return count, last_offset, last_line


def _import_chat_source(
    conn: sqlite3.Connection,
    source: Path,
    *,
    source_kind: str,
    scope: str,
    run_id: str | None,
) -> tuple[int, int, int]:
    count = 0
    last_offset = 0
    last_line = 0
    for entry, offset, next_offset, line_no in _iter_jsonl_records_with_offsets(source):
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO chat_entries(
                ts, ts_text, role, kind, turn, content_text, entry_json,
                source_file, source_offset, source_line, source_kind, scope, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            SQLiteEventIndex.chat_entry_row(
                entry,
                source_file=str(source),
                source_offset=offset,
                source_line=line_no,
                source_kind=source_kind,
                scope=scope,
                run_id=run_id,
            ),
        )
        count += max(cur.rowcount, 0)
        last_offset = next_offset
        last_line = line_no
    return count, last_offset, last_line


def rebuild_sqlite_event_index(
    agent_dir: Path | str,
    *,
    jsonl_path: Path | str | None = None,
    sqlite_path: Path | str | None = None,
) -> dict[str, Any]:
    """Rebuild ``logs/log.sqlite`` from agent trace JSONL sources atomically."""
    agent_dir = Path(agent_dir).resolve()
    logs_dir = agent_dir / "logs"
    explicit_source = Path(jsonl_path).resolve() if jsonl_path is not None else None
    target = Path(sqlite_path).resolve() if sqlite_path is not None else (logs_dir / DEFAULT_SQLITE_NAME).resolve()
    if not agent_dir.is_dir():
        raise FileNotFoundError(f"agent directory not found: {agent_dir}")
    sources = _discover_trace_sources(agent_dir, explicit_source)
    if not sources:
        raise FileNotFoundError(f"no trace JSONL sources found under: {agent_dir}")

    workdir_lock = None
    lock_owner = agent_dir
    try:
        workdir_lock = WorkingDir(lock_owner)
        workdir_lock.acquire_lock(timeout=0)
    except Exception as exc:
        raise RuntimeError(
            "sqlite log rebuild requires the agent to be stopped/offline; "
            f"could not acquire rebuild lock for {lock_owner}: {exc}"
        ) from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="log-sqlite-rebuild-", dir=str(target.parent)))
    tmp_db = tmp_dir / target.name
    event_count = 0
    chat_entry_count = 0
    cursor_count = 0
    primary_source = explicit_source or (logs_dir / DEFAULT_JSONL_NAME).resolve()
    primary_offset = 0
    primary_line = 0
    last_offset = 0
    last_line = 0
    try:
        index = SQLiteEventIndex(tmp_db)
        conn = index._ensure_open()
        with index._lock:
            conn.execute("BEGIN")
            for source in sources:
                source_kind, scope, run_id = _classify_trace_source(agent_dir, source)
                if source_kind.endswith("events") or source_kind == "events":
                    count, offset, line = _import_events_source(
                        conn,
                        source,
                        source_kind=source_kind,
                        scope=scope,
                        run_id=run_id,
                    )
                    event_count += count
                else:
                    count, offset, line = _import_chat_source(
                        conn,
                        source,
                        source_kind=source_kind,
                        scope=scope,
                        run_id=run_id,
                    )
                    chat_entry_count += count
                last_offset = offset
                last_line = line
                if source == primary_source:
                    primary_offset = offset
                    primary_line = line
                conn.execute(
                    """
                    INSERT OR REPLACE INTO import_cursors(source_file, byte_offset, line_no, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(source), offset, line, _utc_now()),
                )
                cursor_count += 1
            conn.commit()
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        index.close()
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"rebuilt sqlite integrity_check={integrity}")
        for suffix in ("-wal", "-shm"):
            sidecar = target.with_name(target.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
        tmp_db.replace(target)
        return {
            "status": "ok",
            "source": str(primary_source if primary_source.is_file() else sources[0]),
            "target": str(target),
            "event_count": event_count,
            "chat_entry_count": chat_entry_count,
            "source_count": len(sources),
            "cursor_count": cursor_count,
            "line_no": primary_line or last_line,
            "byte_offset": primary_offset or last_offset,
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if workdir_lock is not None:
            with contextlib.suppress(Exception):
                workdir_lock.release_lock()


def doctor_sqlite_event_index(agent_dir: Path | str, *, sqlite_path: Path | str | None = None) -> dict[str, Any]:
    agent_dir = Path(agent_dir)
    target = Path(sqlite_path) if sqlite_path is not None else agent_dir / "logs" / DEFAULT_SQLITE_NAME
    if not target.is_file():
        return {"status": "missing", "path": str(target)}
    index = SQLiteEventIndex(target, ensure=False)
    try:
        return index.doctor()
    finally:
        index.close()


def query_sqlite_event_index(agent_dir: Path | str, sql: str, *, sqlite_path: Path | str | None = None) -> list[dict[str, Any]]:
    agent_dir = Path(agent_dir)
    target = Path(sqlite_path) if sqlite_path is not None else agent_dir / "logs" / DEFAULT_SQLITE_NAME
    if not target.is_file():
        raise FileNotFoundError(f"sqlite log index not found: {target}; run `lingtai-agent log rebuild {agent_dir}` first")
    index = SQLiteEventIndex(target, ensure=False)
    try:
        return index.query(sql)
    finally:
        index.close()
