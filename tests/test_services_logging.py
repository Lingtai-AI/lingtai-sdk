"""Tests for lingtai.services.logging."""
import json
import sqlite3
import threading
from pathlib import Path

from lingtai_kernel.services.logging import (
    CompositeLoggingService,
    JSONLLoggingService,
    LoggingService,
    SQLiteEventIndex,
    doctor_sqlite_event_index,
    query_sqlite_event_index,
    rebuild_sqlite_event_index,
)
from tests._service_helpers import make_tool_result_mock_service as make_mock_service


class TestJSONLLoggingService:

    def test_log_writes_jsonl(self, tmp_path):
        """Events are written as JSON lines."""
        log_file = tmp_path / "test.jsonl"
        svc = JSONLLoggingService(log_file)
        svc.log({"type": "test", "value": 42})
        svc.log({"type": "test", "value": 99})
        svc.close()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"type": "test", "value": 42}
        assert json.loads(lines[1]) == {"type": "test", "value": 99}

    def test_log_default_str_for_non_serializable(self, tmp_path):
        """Non-JSON-serializable values are converted via str()."""
        log_file = tmp_path / "test.jsonl"
        svc = JSONLLoggingService(log_file)
        svc.log({"path": Path("/tmp/foo")})
        svc.close()

        line = json.loads(log_file.read_text().strip())
        assert line["path"] == "/tmp/foo"

    def test_close_is_idempotent(self, tmp_path):
        """Calling close() twice does not raise."""
        log_file = tmp_path / "test.jsonl"
        svc = JSONLLoggingService(log_file)
        svc.close()
        svc.close()  # should not raise

    def test_log_after_close_is_noop(self, tmp_path):
        """Logging after close does not raise or write."""
        log_file = tmp_path / "test.jsonl"
        svc = JSONLLoggingService(log_file)
        svc.close()
        svc.log({"type": "test"})  # should not raise
        assert log_file.read_text().strip() == ""

    def test_creates_parent_dirs(self, tmp_path):
        """Parent directories are created if they don't exist."""
        log_file = tmp_path / "nested" / "dir" / "test.jsonl"
        svc = JSONLLoggingService(log_file)
        svc.log({"type": "test"})
        svc.close()
        assert log_file.exists()

    def test_append_mode(self, tmp_path):
        """Opening an existing file appends, does not truncate."""
        log_file = tmp_path / "test.jsonl"
        log_file.write_text('{"existing": true}\n')

        svc = JSONLLoggingService(log_file)
        svc.log({"type": "new"})
        svc.close()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["existing"] is True
        assert json.loads(lines[1])["type"] == "new"

    def test_thread_safety(self, tmp_path):
        """Concurrent writes don't corrupt the file."""
        log_file = tmp_path / "test.jsonl"
        svc = JSONLLoggingService(log_file)

        def writer(thread_id):
            for i in range(50):
                svc.log({"thread": thread_id, "i": i})

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        svc.close()

        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 200  # 4 threads * 50 writes
        # Every line must be valid JSON
        for line in lines:
            json.loads(line)

    def test_abc_cannot_instantiate(self):
        """LoggingService ABC cannot be instantiated directly."""
        try:
            LoggingService()
            assert False, "Should have raised TypeError"
        except TypeError:
            pass


# ---------------------------------------------------------------------------
# BaseAgent + LoggingService integration
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock
from lingtai_kernel import BaseAgent, AgentState
from lingtai_kernel.llm import ToolCall
from lingtai_kernel.loop_guard import LoopGuard




class TestBaseAgentLoggingIntegration:

    def test_tool_call_logged(self, tmp_path):
        """Executing a tool logs tool_call and tool_result events."""
        from lingtai_kernel.tool_executor import ToolExecutor

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent.add_tool("greet", schema={"type": "object", "properties": {}}, handler=lambda args: {"status": "ok"})

        guard = LoopGuard()
        errors = []
        tc = ToolCall(name="greet", args={})
        executor = ToolExecutor(
            dispatch_fn=agent._dispatch_tool,
            make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
                name, result, provider=agent._config.provider, **kw
            ),
            guard=guard,
            known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
            logger_fn=agent._log,
        )
        executor.execute([tc], collected_errors=errors)

        # Log file should exist in working dir
        log_file = agent.working_dir / "logs" / "events.jsonl"
        assert log_file.is_file()
        events = agent._log_service.get_events()
        types = [e["type"] for e in events]
        assert "tool_call" in types
        assert "tool_result" in types
        # Verify agent_name is injected
        assert all(e["agent_name"] == "test" for e in events)
        # Verify ts is present
        assert all("ts" in e for e in events)

    def test_auto_logging_to_working_dir(self, tmp_path):
        """Agent always creates JSONL log in working dir."""
        from lingtai_kernel.tool_executor import ToolExecutor

        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent.add_tool("greet", schema={"type": "object", "properties": {}}, handler=lambda args: {"status": "ok"})

        guard = LoopGuard()
        errors = []
        tc = ToolCall(name="greet", args={})
        executor = ToolExecutor(
            dispatch_fn=agent._dispatch_tool,
            make_tool_result_fn=lambda name, result, **kw: agent.service.make_tool_result(
                name, result, provider=agent._config.provider, **kw
            ),
            guard=guard,
            known_tools=set(agent._intrinsics) | set(agent._tool_handlers),
            logger_fn=agent._log,
        )
        executor.execute([tc], collected_errors=errors)

        # Log file should exist in working dir
        log_file = agent.working_dir / "logs" / "events.jsonl"
        assert log_file.is_file()
        events = agent._log_service.get_events()
        types = [e["type"] for e in events]
        assert "tool_call" in types

    def test_state_change_logged(self, tmp_path):
        """State transitions are logged."""
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._set_state(AgentState.ACTIVE, reason="test")

        events = agent._log_service.get_events()
        state_events = [e for e in events if e["type"] == "agent_state"]
        assert len(state_events) >= 1


class TestSQLiteEventIndex:

    def test_composite_writes_jsonl_and_sqlite(self, tmp_path):
        log_file = tmp_path / "logs" / "events.jsonl"
        sqlite_file = tmp_path / "logs" / "log.sqlite"
        svc = CompositeLoggingService(
            JSONLLoggingService(log_file),
            sqlite_index=SQLiteEventIndex(sqlite_file, keep_open=False),
        )
        svc.log({"type": "agent_state", "address": "agent", "agent_name": "agent", "ts": 1.25, "new": "IDLE"})
        svc.close()

        assert log_file.is_file()
        rows = query_sqlite_event_index(tmp_path, "SELECT type, agent_address, agent_name_snapshot FROM events")
        assert rows == [{"type": "agent_state", "agent_address": "agent", "agent_name_snapshot": "agent"}]


    def test_composite_after_close_does_not_create_sidecar_only_event(self, tmp_path):
        log_file = tmp_path / "logs" / "events.jsonl"
        sqlite_file = tmp_path / "logs" / "log.sqlite"
        svc = CompositeLoggingService(
            JSONLLoggingService(log_file),
            sqlite_index=SQLiteEventIndex(sqlite_file, keep_open=False),
        )
        svc.close()
        svc.log({"type": "after_close", "ts": 1})

        assert log_file.read_text() == ""
        rows = query_sqlite_event_index(tmp_path, "SELECT type FROM events WHERE type='after_close'")
        assert rows == []

    def test_sqlite_sidecar_fail_open(self, tmp_path):
        log_file = tmp_path / "events.jsonl"
        index = SQLiteEventIndex(tmp_path / "log.sqlite")
        index.disable("simulated")
        svc = CompositeLoggingService(JSONLLoggingService(log_file), sqlite_index=index)

        svc.log({"type": "test", "ts": 1})
        svc.close()

        assert json.loads(log_file.read_text().strip())["type"] == "test"
        assert index.disabled_reason == "simulated"

    def test_sqlite_sidecar_coerces_invalid_timestamp(self, tmp_path):
        log_file = tmp_path / "events.jsonl"
        index = SQLiteEventIndex(tmp_path / "log.sqlite")
        svc = CompositeLoggingService(JSONLLoggingService(log_file), sqlite_index=index)

        svc.log({"type": "bad_ts", "ts": "not-a-float"})
        svc.close()

        assert json.loads(log_file.read_text().strip())["type"] == "bad_ts"
        rows = index.query("SELECT ts, fields_json FROM events WHERE type='bad_ts'")
        assert rows[0]["ts"] == 0.0
        assert json.loads(rows[0]["fields_json"]) == {"ts_raw": "not-a-float"}

    def test_query_rejects_mutating_sql(self, tmp_path):
        index = SQLiteEventIndex(tmp_path / "log.sqlite")
        try:
            try:
                index.query("DELETE FROM events")
                assert False, "mutating query should be rejected"
            except ValueError:
                pass
        finally:
            index.close()


    def test_query_rejects_mutating_select_function(self, tmp_path):
        sqlite_file = tmp_path / "log.sqlite"
        index = SQLiteEventIndex(sqlite_file)
        try:
            raw = index._ensure_open()
            raw.create_function(
                "danger",
                0,
                lambda: raw.execute("DELETE FROM events") and 1,
            )
            try:
                index.query("SELECT danger()")
                assert False, "mutating select function should be rejected"
            except Exception as exc:
                message = str(exc).lower()
                assert (
                    "not authorized" in message
                    or "must not modify" in message
                    or "user-defined function raised exception" in message
                    or "attempt to write a readonly database" in message
                )
            index.log_event({"type": "after_query", "ts": 1})
            assert index.query("SELECT type FROM events") == [{"type": "after_query"}]
        finally:
            index.close()

    def test_rebuild_doctor_and_query(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        events = logs / "events.jsonl"
        events.write_text(
            json.dumps({"type": "alpha", "ts": 1, "address": "a", "agent_name": "n", "x": 1}) + "\n"
            + json.dumps({"type": "beta", "ts": 2, "address": "a", "agent_name": "n", "x": 2}) + "\n",
            encoding="utf-8",
        )

        result = rebuild_sqlite_event_index(tmp_path)
        assert result["status"] == "ok"
        assert result["event_count"] == 2

        doctor = doctor_sqlite_event_index(tmp_path)
        assert doctor["status"] == "ok"
        assert doctor["event_count"] == 2
        assert doctor["chat_entry_count"] == 0
        assert doctor["token_entry_count"] == 0
        assert doctor["schema_current"] is True
        assert 2 in doctor["schema_versions"]
        assert 3 in doctor["schema_versions"]

        rows = query_sqlite_event_index(tmp_path, "SELECT type, agent_address, agent_name_snapshot, json_extract(fields_json, '$.x') AS x FROM events ORDER BY ts")
        assert rows == [
            {"type": "alpha", "agent_address": "a", "agent_name_snapshot": "n", "x": 1},
            {"type": "beta", "agent_address": "a", "agent_name_snapshot": "n", "x": 2},
        ]

        stored_fields = query_sqlite_event_index(tmp_path, "SELECT fields_json FROM events WHERE type='alpha'")[0]
        assert json.loads(stored_fields["fields_json"]) == {"x": 1}

    def test_base_agent_creates_sqlite_sidecar(self, tmp_path):
        agent = BaseAgent(
            service=make_mock_service(),
            agent_name="test",
            working_dir=tmp_path / "test_agent",
        )
        agent._log("custom", value=123)
        agent._log_service.close()

        sqlite_file = agent.working_dir / "logs" / "log.sqlite"
        assert sqlite_file.is_file()
        rows = query_sqlite_event_index(agent.working_dir, "SELECT type FROM events WHERE type='custom'")
        assert rows == [{"type": "custom"}]

    def test_query_missing_sqlite_requires_explicit_rebuild(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text(json.dumps({"type": "alpha", "ts": 1}) + "\n", encoding="utf-8")

        try:
            query_sqlite_event_index(tmp_path, "SELECT type FROM events")
            assert False, "query should require explicit rebuild when sqlite sidecar is missing"
        except FileNotFoundError as exc:
            assert "rebuild" in str(exc)
        assert not (logs / "log.sqlite").exists()

    def test_doctor_missing_sqlite_is_read_only(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        result = doctor_sqlite_event_index(tmp_path)
        assert result["status"] == "missing"
        assert not (logs / "log.sqlite").exists()

    def test_doctor_existing_sqlite_does_not_create_wal_or_mutate_mtime(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text(json.dumps({"type": "alpha", "ts": 1}) + "\n", encoding="utf-8")
        rebuild_sqlite_event_index(tmp_path)
        sqlite_file = logs / "log.sqlite"
        before_mtime = sqlite_file.stat().st_mtime_ns
        for suffix in ("-wal", "-shm"):
            (logs / ("log.sqlite" + suffix)).unlink(missing_ok=True)

        result = doctor_sqlite_event_index(tmp_path)
        assert result["status"] == "ok"
        assert sqlite_file.stat().st_mtime_ns == before_mtime
        assert not (logs / "log.sqlite-wal").exists()
        assert not (logs / "log.sqlite-shm").exists()


    def test_read_only_query_sees_live_wal_rows(self, tmp_path):
        sqlite_file = tmp_path / "logs" / "log.sqlite"
        index = SQLiteEventIndex(sqlite_file, keep_open=True)
        try:
            index.log_event({"type": "live_wal", "ts": 1})
            rows = query_sqlite_event_index(tmp_path, "SELECT type FROM events WHERE type='live_wal'")
            assert rows == [{"type": "live_wal"}]

            doctor = doctor_sqlite_event_index(tmp_path)
            assert doctor["status"] == "ok"
            assert doctor["event_count"] == 1
        finally:
            index.close()

    def test_query_accepts_read_only_cte_and_explain(self, tmp_path):
        index = SQLiteEventIndex(tmp_path / "logs" / "log.sqlite")
        try:
            index.log_event({"type": "cte", "ts": 1})
            rows = query_sqlite_event_index(
                tmp_path,
                "WITH recent AS (SELECT type FROM events) SELECT type FROM recent",
            )
            assert rows == [{"type": "cte"}]

            plan = query_sqlite_event_index(tmp_path, "EXPLAIN QUERY PLAN SELECT type FROM events")
            assert plan
        finally:
            index.close()

    def test_rebuild_missing_agent_dir_does_not_materialize_path(self, tmp_path):
        missing = tmp_path / "missing-agent"
        try:
            rebuild_sqlite_event_index(missing)
            assert False, "missing agent dir should fail before lock setup"
        except FileNotFoundError as exc:
            assert "agent directory not found" in str(exc)
        assert not missing.exists()

    def test_rebuild_indexes_chat_history_and_daemon_jsonl_sources(self, tmp_path):
        logs = tmp_path / "logs"
        history = tmp_path / "history"
        daemon_logs = tmp_path / "daemons" / "em-1-20260531-030000-abcdef" / "logs"
        daemon_history = tmp_path / "daemons" / "em-1-20260531-030000-abcdef" / "history"
        logs.mkdir(parents=True)
        history.mkdir()
        daemon_logs.mkdir(parents=True)
        daemon_history.mkdir()
        (logs / "events.jsonl").write_text(json.dumps({"type": "agent_event", "ts": 1}) + "\n", encoding="utf-8")
        (history / "chat_history.jsonl").write_text(
            json.dumps({"role": "user", "content": [{"type": "text", "text": "hello agent"}], "ts": "2026-05-31T03:00:00Z"}) + "\n",
            encoding="utf-8",
        )
        (history / "chat_history_archive.jsonl").write_text(
            json.dumps({"role": "assistant", "text": "archived reply", "ts": "2026-05-31T03:01:00Z"}) + "\n",
            encoding="utf-8",
        )
        (daemon_logs / "events.jsonl").write_text(json.dumps({"event": "tool_call", "name": "bash", "ts": "2026-05-31T03:02:00Z"}) + "\n", encoding="utf-8")
        (daemon_history / "chat_history.jsonl").write_text(
            json.dumps({"role": "assistant", "text": "daemon answer", "turn": 1, "ts": "2026-05-31T03:03:00Z"}) + "\n",
            encoding="utf-8",
        )

        result = rebuild_sqlite_event_index(tmp_path)
        assert result["event_count"] == 2
        assert result["chat_entry_count"] == 3
        assert result["source_count"] == 5

        event_rows = query_sqlite_event_index(tmp_path, "SELECT type, source_kind, scope, run_id FROM events ORDER BY id")
        assert event_rows == [
            {"type": "agent_event", "source_kind": "agent_events", "scope": "agent", "run_id": None},
            {"type": "tool_call", "source_kind": "daemon_events", "scope": "daemon", "run_id": "em-1-20260531-030000-abcdef"},
        ]
        chat_rows = query_sqlite_event_index(
            tmp_path,
            "SELECT role, kind, turn, content_text, source_kind, scope, run_id FROM chat_entries ORDER BY id",
        )
        assert chat_rows == [
            {"role": "user", "kind": None, "turn": None, "content_text": "hello agent", "source_kind": "agent_chat", "scope": "agent", "run_id": None},
            {"role": "assistant", "kind": None, "turn": None, "content_text": "archived reply", "source_kind": "agent_chat_archive", "scope": "agent", "run_id": None},
            {"role": "assistant", "kind": None, "turn": 1, "content_text": "daemon answer", "source_kind": "daemon_chat", "scope": "daemon", "run_id": "em-1-20260531-030000-abcdef"},
        ]

    def test_rebuild_indexes_canonical_chat_history_shapes(self, tmp_path):
        history = tmp_path / "history"
        history.mkdir()
        rows = [
            {"role": "system", "system": "system prompt text", "timestamp": 0},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_call", "id": "tc-1", "name": "bash", "args": {"command": "echo hi"}},
                    {"type": "tool_result", "id": "tc-1", "name": "bash", "content": {"stdout": "hi"}},
                ],
                "timestamp": 0,
            },
        ]
        (history / "chat_history.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

        result = rebuild_sqlite_event_index(tmp_path)
        assert result["event_count"] == 0
        assert result["chat_entry_count"] == 2

        chat_rows = query_sqlite_event_index(
            tmp_path,
            "SELECT role, ts, ts_text, content_text FROM chat_entries ORDER BY id",
        )
        assert chat_rows[0] == {
            "role": "system",
            "ts": 0.0,
            "ts_text": "0",
            "content_text": "system prompt text",
        }
        assert chat_rows[1]["role"] == "assistant"
        assert chat_rows[1]["ts"] == 0.0
        assert chat_rows[1]["ts_text"] == "0"
        assert "tool_call bash" in chat_rows[1]["content_text"]
        assert "echo hi" in chat_rows[1]["content_text"]
        assert "tool_result bash" in chat_rows[1]["content_text"]
        assert "hi" in chat_rows[1]["content_text"]

    def test_existing_v1_sidecar_migrates_to_trace_schema(self, tmp_path):
        sqlite_file = tmp_path / "logs" / "log.sqlite"
        sqlite_file.parent.mkdir()
        conn = sqlite3.connect(sqlite_file)
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
            CREATE TABLE import_cursors(source_file TEXT PRIMARY KEY, byte_offset INTEGER NOT NULL DEFAULT 0, line_no INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
            CREATE TABLE events(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              type TEXT NOT NULL,
              agent_address TEXT,
              agent_name_snapshot TEXT,
              fields_json TEXT NOT NULL,
              source_file TEXT,
              source_offset INTEGER,
              inserted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            INSERT INTO schema_migrations(version, name, applied_at) VALUES (1, 'initial_events_index', 'now');
            INSERT INTO events(ts, type, fields_json) VALUES (1, 'old', '{}');
            """
        )
        conn.commit()
        conn.close()

        index = SQLiteEventIndex(sqlite_file)
        index.close()

        doctor = doctor_sqlite_event_index(tmp_path)
        assert doctor["schema_current"] is True
        assert 2 in doctor["schema_versions"]
        assert 3 in doctor["schema_versions"]
        rows = query_sqlite_event_index(tmp_path, "SELECT type, source_kind FROM events")
        assert rows == [{"type": "old", "source_kind": None}]
        assert query_sqlite_event_index(tmp_path, "SELECT COUNT(*) AS n FROM chat_entries") == [{"n": 0}]
        assert query_sqlite_event_index(tmp_path, "SELECT COUNT(*) AS n FROM token_entries") == [{"n": 0}]

    def test_rebuild_indexes_token_ledger_sources(self, tmp_path):
        logs = tmp_path / "logs"
        daemon_logs = tmp_path / "daemons" / "em-1-20260531-030000-abcdef" / "logs"
        logs.mkdir(parents=True)
        daemon_logs.mkdir(parents=True)
        (logs / "token_ledger.jsonl").write_text(
            json.dumps({
                "ts": "2026-05-31T03:00:00Z",
                "input": 100,
                "output": 20,
                "thinking": 5,
                "cached": 40,
                "source": "main",
                "api_call_id": "api_agent",
                "model": "gpt-agent",
                "endpoint": "https://agent.example",
            }) + "\n",
            encoding="utf-8",
        )
        (daemon_logs / "token_ledger.jsonl").write_text(
            json.dumps({
                "ts": "2026-05-31T03:01:00Z",
                "input": 11,
                "output": 4,
                "thinking": 0,
                "cached": 3,
                "source": "daemon",
                "em_id": "em-1",
                "run_id": "em-1-20260531-030000-abcdef",
                "api_call_id": "api_daemon",
            }) + "\n",
            encoding="utf-8",
        )

        result = rebuild_sqlite_event_index(tmp_path)
        assert result["event_count"] == 0
        assert result["chat_entry_count"] == 0
        assert result["token_entry_count"] == 2
        assert result["source_count"] == 2

        rows = query_sqlite_event_index(
            tmp_path,
            """
            SELECT input_tokens, output_tokens, thinking_tokens, cached_tokens,
                   source, api_call_id, model, endpoint, source_kind, scope, run_id
            FROM token_entries ORDER BY ts
            """,
        )
        assert rows == [
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "thinking_tokens": 5,
                "cached_tokens": 40,
                "source": "main",
                "api_call_id": "api_agent",
                "model": "gpt-agent",
                "endpoint": "https://agent.example",
                "source_kind": "agent_token_ledger",
                "scope": "agent",
                "run_id": None,
            },
            {
                "input_tokens": 11,
                "output_tokens": 4,
                "thinking_tokens": 0,
                "cached_tokens": 3,
                "source": "daemon",
                "api_call_id": "api_daemon",
                "model": None,
                "endpoint": None,
                "source_kind": "daemon_token_ledger",
                "scope": "daemon",
                "run_id": "em-1-20260531-030000-abcdef",
            },
        ]
        doctor = doctor_sqlite_event_index(tmp_path)
        assert doctor["token_entry_count"] == 2

    def test_rebuild_preserves_runtime_rows_without_duplicates(self, tmp_path):
        log_file = tmp_path / "logs" / "events.jsonl"
        sqlite_file = tmp_path / "logs" / "log.sqlite"
        svc = CompositeLoggingService(
            JSONLLoggingService(log_file),
            sqlite_index=SQLiteEventIndex(sqlite_file, keep_open=False),
        )
        svc.log({"type": "first", "ts": 1})
        svc.log({"type": "second", "ts": 2})
        svc.close()

        assert query_sqlite_event_index(tmp_path, "SELECT COUNT(*) AS n FROM events") == [{"n": 2}]
        result = rebuild_sqlite_event_index(tmp_path)
        assert result["event_count"] == 2
        rows = query_sqlite_event_index(tmp_path, "SELECT type FROM events ORDER BY ts")
        assert rows == [{"type": "first"}, {"type": "second"}]

    def test_rebuild_tolerates_invalid_timestamp_rows(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text(
            json.dumps({"type": "bad_ts", "ts": "not-a-float"}) + "\n"
            + json.dumps({"type": "ok", "ts": 1}) + "\n",
            encoding="utf-8",
        )

        result = rebuild_sqlite_event_index(tmp_path)
        assert result["event_count"] == 2
        rows = query_sqlite_event_index(
            tmp_path,
            "SELECT type, ts, fields_json FROM events ORDER BY id",
        )
        assert rows[0]["type"] == "bad_ts"
        assert rows[0]["ts"] == 0.0
        assert json.loads(rows[0]["fields_json"]) == {"ts_raw": "not-a-float"}
        assert rows[1]["type"] == "ok"

    def test_fail_open_continues_jsonl_logging_after_sidecar_disable(self, tmp_path):
        log_file = tmp_path / "events.jsonl"
        index = SQLiteEventIndex(tmp_path / "log.sqlite")
        index.disable("simulated")
        svc = CompositeLoggingService(JSONLLoggingService(log_file), sqlite_index=index)

        svc.log({"type": "after_disable_initial", "ts": 0})
        for i in range(3):
            svc.log({"type": f"after_disable_{i}", "ts": i})
        svc.close()

        events = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert [event["type"] for event in events] == [
            "after_disable_initial",
            "after_disable_0",
            "after_disable_1",
            "after_disable_2",
        ]
        assert index.disabled_reason == "simulated"

    def test_rebuild_requires_offline_agent_lock(self, tmp_path):
        from lingtai_kernel.workdir import WorkingDir

        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text(json.dumps({"type": "alpha", "ts": 1}) + "\n", encoding="utf-8")
        lock = WorkingDir(tmp_path)
        lock.acquire_lock(timeout=0)
        try:
            try:
                rebuild_sqlite_event_index(tmp_path)
                assert False, "rebuild should require offline lock"
            except RuntimeError as exc:
                assert "stopped/offline" in str(exc)
        finally:
            lock.release_lock()
