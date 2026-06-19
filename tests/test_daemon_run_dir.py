"""Pure FS unit tests for DaemonRunDir — no threads, no LLM mocks."""
import json
import os
import re
import time
from pathlib import Path

from lingtai.core.daemon.run_dir import DaemonRunDir


def _make_run_dir(tmp_path: Path, **overrides) -> DaemonRunDir:
    """Helper: construct a DaemonRunDir with sensible defaults."""
    parent_wd = tmp_path / "parent"
    parent_wd.mkdir(exist_ok=True)
    kwargs = dict(
        parent_working_dir=parent_wd,
        handle="em-3",
        task="find todos",
        tools=["file"],
        model="mock-model",
        max_turns=30,
        timeout_s=300.0,
        parent_addr="parent",
        parent_pid=12345,
        system_prompt="You are a daemon emanation.",
    )
    kwargs.update(overrides)
    return DaemonRunDir(**kwargs)


def test_construct_creates_folder_structure(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.path.is_dir()
    assert (rd.path / "history").is_dir()
    assert (rd.path / "logs").is_dir()
    assert rd.daemon_json_path.is_file()
    assert rd.prompt_path.is_file()
    assert rd.heartbeat_path.is_file()


def test_run_id_format(tmp_path):
    """run_id is em-<N>-<YYYYMMDD-HHMMSS>-<6 hex chars>."""
    rd = _make_run_dir(tmp_path, handle="em-7")
    assert re.fullmatch(r"em-7-\d{8}-\d{6}-[0-9a-f]{6}", rd.run_id)
    assert rd.path.name == rd.run_id


def test_folder_lives_under_parent_daemons_dir(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.path.parent == tmp_path / "parent" / "daemons"


def test_initial_daemon_json_fields(tmp_path):
    rd = _make_run_dir(tmp_path)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["handle"] == "em-3"
    assert data["run_id"] == rd.run_id
    assert data["group_id"] is None
    assert data["parent_addr"] == "parent"
    assert data["parent_pid"] == 12345
    assert data["task"] == "find todos"
    assert data["tools"] == ["file"]
    assert data["model"] == "mock-model"
    assert data["max_turns"] == 30
    assert data["timeout_s"] == 300.0
    assert data["state"] == "running"
    assert data["finished_at"] is None
    assert data["turn"] == 0
    assert data["current_tool"] is None
    assert data["tool_call_count"] == 0
    assert data["tokens"] == {"input": 0, "output": 0, "thinking": 0, "cached": 0}
    assert data["result_preview"] is None
    assert data["error"] is None
    # started_at is ISO 8601 UTC
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", data["started_at"])




def test_initial_daemon_json_records_group_id(tmp_path):
    group_id = "dg-20260614-145500-abcdef"
    rd = _make_run_dir(tmp_path, group_id=group_id)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["group_id"] == group_id


def test_new_group_id_format():
    group_id = DaemonRunDir.new_group_id()
    assert re.fullmatch(r"dg-\d{8}-\d{6}-[0-9a-f]{6}", group_id)


def test_prompt_written_verbatim(tmp_path):
    prompt = "You are a daemon emanation.\nYour task is X.\nUse tools wisely."
    rd = _make_run_dir(tmp_path, system_prompt=prompt)
    assert rd.prompt_path.read_text() == prompt


def test_daemon_start_event_logged(tmp_path):
    rd = _make_run_dir(tmp_path)
    events_path = rd.path / "logs" / "events.jsonl"
    assert events_path.is_file()
    line = events_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["event"] == "daemon_start"
    assert "ts" in entry


def test_two_constructions_same_handle_no_collision(tmp_path):
    """Two run_dirs with the same handle in the same second get distinct folders."""
    rd1 = _make_run_dir(tmp_path, handle="em-1")
    rd2 = _make_run_dir(tmp_path, handle="em-1")
    assert rd1.run_id != rd2.run_id
    assert rd1.path != rd2.path
    assert rd1.path.is_dir()
    assert rd2.path.is_dir()


def test_path_properties_consistent(tmp_path):
    rd = _make_run_dir(tmp_path)
    assert rd.daemon_json_path == rd.path / "daemon.json"
    assert rd.prompt_path == rd.path / ".prompt"
    assert rd.heartbeat_path == rd.path / ".heartbeat"
    assert rd.chat_path == rd.path / "history" / "chat_history.jsonl"
    assert rd.events_path == rd.path / "logs" / "events.jsonl"
    assert rd.token_ledger_path == rd.path / "logs" / "token_ledger.jsonl"


def test_record_user_send_task_kind(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("find todos", kind="task")
    line = rd.chat_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["role"] == "user"
    assert entry["text"] == "find todos"
    assert entry["kind"] == "task"
    assert entry["turn"] == 0
    assert "ts" in entry


def test_record_user_send_tool_results_verbatim(tmp_path):
    """Tool result payloads written verbatim — no truncation."""
    rd = _make_run_dir(tmp_path)
    big = "x" * 50_000
    rd.record_user_send(big, kind="tool_results")
    line = rd.chat_path.read_text().splitlines()[-1]
    entry = json.loads(line)
    assert entry["text"] == big
    assert entry["kind"] == "tool_results"


def test_record_user_send_followup_kind(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("also check tests/", kind="followup")
    entry = json.loads(rd.chat_path.read_text().splitlines()[0])
    assert entry["kind"] == "followup"


def test_bump_turn_updates_daemon_json(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.bump_turn(turn=1, response_text="Scanning...")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["turn"] == 1
    assert data["current_tool"] is None
    assert data["elapsed_s"] >= 0.0
    assert data["state"] == "running"  # unchanged


def test_bump_turn_appends_assistant_chat_entry(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.bump_turn(turn=1, response_text="Scanning files...")
    line = rd.chat_path.read_text().splitlines()[-1]
    entry = json.loads(line)
    assert entry["role"] == "assistant"
    assert entry["text"] == "Scanning files..."
    assert entry["turn"] == 1


def test_bump_turn_advances_heartbeat(tmp_path):
    rd = _make_run_dir(tmp_path)
    initial_mtime = rd.heartbeat_path.stat().st_mtime
    time.sleep(0.05)
    rd.bump_turn(turn=1, response_text="ok")
    assert rd.heartbeat_path.stat().st_mtime > initial_mtime


def test_record_user_send_uses_current_turn(tmp_path):
    """user-send entries record the current turn (so tool_results land at turn=1
    after the first assistant response, not turn=0)."""
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("task", kind="task")
    rd.bump_turn(turn=1, response_text="response 1")
    rd.record_user_send("tool result", kind="tool_results")
    entries = [json.loads(line) for line in rd.chat_path.read_text().splitlines()]
    assert entries[0]["turn"] == 0  # initial task at turn 0
    assert entries[1]["turn"] == 1  # assistant response
    assert entries[2]["turn"] == 1  # tool result, fed into turn-1 send


def test_set_current_tool_updates_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "src/main.py"})
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["current_tool"] == "read"
    assert data["tool_call_count"] == 1


def test_set_current_tool_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "src/main.py"})
    # daemon_start was line 1; tool_call should be the next line
    lines = rd.events_path.read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["event"] == "tool_call"
    assert entry["name"] == "read"
    assert "args_preview" in entry
    assert "ts" in entry


def test_set_current_tool_args_preview_truncated(tmp_path):
    """args_preview is bounded — full args could be huge (e.g., write())."""
    rd = _make_run_dir(tmp_path)
    big_content = "x" * 10_000
    rd.set_current_tool("write", {"path": "out.txt", "content": big_content})
    entry = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert len(entry["args_preview"]) <= 500


def test_set_current_tool_advances_heartbeat(tmp_path):
    rd = _make_run_dir(tmp_path)
    initial = rd.heartbeat_path.stat().st_mtime
    time.sleep(0.05)
    rd.set_current_tool("read", {})
    assert rd.heartbeat_path.stat().st_mtime > initial


def test_clear_current_tool_resets_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "x"})
    rd.clear_current_tool(result_status="ok")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["current_tool"] is None
    assert data["tool_call_count"] == 1  # unchanged


def test_clear_current_tool_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"file_path": "x"})
    rd.clear_current_tool(result_status="ok")
    lines = rd.events_path.read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "tool_result"
    assert last["name"] == "read"
    assert last["status"] == "ok"


def test_multiple_tool_dispatches_increment_count(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {})
    rd.clear_current_tool(result_status="ok")
    rd.set_current_tool("write", {})
    rd.clear_current_tool(result_status="ok")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["tool_call_count"] == 2



def test_record_cli_output_updates_state_and_event(tmp_path):
    rd = _make_run_dir(tmp_path, backend="codex")
    initial_mtime = rd.heartbeat_path.stat().st_mtime
    time.sleep(0.05)

    rd.record_cli_output("working on step 1", stream="combined")

    data = json.loads(rd.daemon_json_path.read_text())
    assert data["last_output"] == "working on step 1"
    assert data["last_output_at"] is not None
    assert data["elapsed_s"] >= 0.0
    assert rd.heartbeat_path.stat().st_mtime > initial_mtime

    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "cli_output"
    assert last["stream"] == "combined"
    assert last["text"] == "working on step 1"
    assert "elapsed_s" in last


def test_record_cli_output_bounds_large_event_and_last_output(tmp_path):
    rd = _make_run_dir(tmp_path)
    big = "x" * 5000

    rd.record_cli_output(big, stream="stderr")

    data = json.loads(rd.daemon_json_path.read_text())
    assert len(data["last_output"]) == rd._LAST_OUTPUT_MAX

    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "cli_output"
    assert last["stream"] == "stderr"
    assert last["truncated"] is True
    assert len(last["text"]) <= rd._CLI_OUTPUT_EVENT_MAX + len("...[truncated]")

def test_append_tokens_writes_daemon_ledger(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    line = rd.token_ledger_path.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["input"] == 100
    assert entry["output"] == 20
    assert entry["thinking"] == 5
    assert entry["cached"] == 10
    assert "ts" in entry
    # Daemon's own ledger carries the same source/em_id/run_id tags as the
    # parent's ledger — every entry self-describes regardless of file.
    assert entry["source"] == "daemon"
    assert entry["em_id"] == "em-3"
    assert entry["run_id"] == rd.run_id


def test_append_tokens_writes_parent_ledger_tagged(tmp_path):
    rd = _make_run_dir(tmp_path, parent_addr="researcher")
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    line = parent_ledger.read_text().splitlines()[0]
    entry = json.loads(line)
    assert entry["input"] == 100
    assert entry["source"] == "daemon"
    assert entry["em_id"] == "em-3"
    assert entry["run_id"] == rd.run_id


def test_append_tokens_updates_running_totals(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    rd.append_tokens(input=50, output=15, thinking=3, cached=5)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["tokens"] == {"input": 150, "output": 35, "thinking": 8, "cached": 15}


def test_append_tokens_skipped_when_all_zero(tmp_path):
    """Don't write a noise entry if the LLM call returned zero tokens."""
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=0, output=0, thinking=0, cached=0)
    assert not rd.token_ledger_path.exists() or rd.token_ledger_path.read_text() == ""
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    assert not parent_ledger.exists() or parent_ledger.read_text() == ""


def test_summing_parent_ledger_includes_daemon_spend(tmp_path):
    """sum_token_ledger on parent's ledger sums daemon and parent calls together."""
    from lingtai_kernel.token_ledger import append_token_entry, sum_token_ledger
    rd = _make_run_dir(tmp_path)
    parent_ledger = tmp_path / "parent" / "logs" / "token_ledger.jsonl"
    # Parent's own call
    append_token_entry(parent_ledger, input=200, output=40, thinking=10, cached=20)
    # Daemon call
    rd.append_tokens(input=100, output=20, thinking=5, cached=10)
    totals = sum_token_ledger(parent_ledger)
    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 60
    assert totals["api_calls"] == 2


def test_mark_done_writes_terminal_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("Task done. Found 3 TODOs.")
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "done"
    assert data["finished_at"] is not None
    assert data["result_preview"] == "Task done. Found 3 TODOs."
    assert data["result_path"] == str(rd.result_path)
    assert rd.result_path.read_text() == "Task done. Found 3 TODOs."
    assert data["error"] is None


def test_mark_done_truncates_result_preview(tmp_path):
    rd = _make_run_dir(tmp_path)
    long_text = "a" * 500
    rd.mark_done(long_text)
    data = json.loads(rd.daemon_json_path.read_text())
    assert len(data["result_preview"]) <= 200
    assert rd.result_path.read_text() == long_text


def test_mark_done_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_done("ok")
    lines = rd.events_path.read_text().splitlines()
    last = json.loads(lines[-1])
    assert last["event"] == "daemon_done"
    assert last["result_path"] == str(rd.result_path)
    assert "elapsed_s" in last


def test_mark_failed_records_error(tmp_path):
    rd = _make_run_dir(tmp_path)
    exc = RuntimeError("boom")
    rd.mark_failed(exc)
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "failed"
    assert data["finished_at"] is not None
    assert data["error"]["type"] == "RuntimeError"
    assert data["error"]["message"] == "boom"
    assert data["result_preview"] is None


def test_mark_failed_logs_event(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_failed(ValueError("bad"))
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_error"
    assert last["exception"] == "ValueError"


def test_mark_cancelled_writes_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_cancelled()
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "cancelled"
    assert data["finished_at"] is not None
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_cancelled"


def test_mark_timeout_writes_state(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.mark_timeout()
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "timeout"
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_timeout"


def test_terminal_markers_idempotent_safe(tmp_path):
    """Calling a terminal marker twice does not crash (defensive)."""
    rd = _make_run_dir(tmp_path)
    rd.mark_done("first")
    rd.mark_done("second")  # should not raise
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["result_preview"] == "second"  # last write wins


def test_atomic_write_no_partial_state_on_replace_failure(tmp_path, monkeypatch):
    """If os.replace raises mid-flight, the prior daemon.json remains valid."""
    rd = _make_run_dir(tmp_path)
    initial_data = json.loads(rd.daemon_json_path.read_text())

    # Simulate replace failure on next bump_turn
    real_replace = os.replace
    call_count = [0]

    def failing_replace(src, dst):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated")
        return real_replace(src, dst)

    monkeypatch.setattr("os.replace", failing_replace)
    rd.bump_turn(turn=99, response_text="should not land")

    # daemon.json must still be valid JSON with prior contents
    data = json.loads(rd.daemon_json_path.read_text())
    assert data == initial_data
    assert data["turn"] == 0  # prior value preserved


def test_oserror_in_mutation_does_not_raise(tmp_path):
    """Best-effort policy: OSError swallowed, run continues."""
    rd = _make_run_dir(tmp_path)
    # Make logs/ unwritable
    logs_dir = rd.path / "logs"
    logs_dir.chmod(0o500)
    try:
        # Should not raise
        rd.set_current_tool("read", {})
        rd.clear_current_tool(result_status="ok")
        rd.append_tokens(input=10, output=5, thinking=2, cached=1)
    finally:
        logs_dir.chmod(0o700)


def test_chat_history_jsonl_lines_parseable(tmp_path):
    """All lines in chat_history.jsonl are valid JSON."""
    rd = _make_run_dir(tmp_path)
    rd.record_user_send("task", kind="task")
    rd.bump_turn(turn=1, response_text="response")
    rd.record_user_send("more", kind="followup")
    rd.bump_turn(turn=2, response_text="another")
    for line in rd.chat_path.read_text().splitlines():
        assert json.loads(line)  # parses without error


def test_events_jsonl_lines_parseable(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.set_current_tool("read", {"a": 1})
    rd.clear_current_tool(result_status="ok")
    rd.mark_done("ok")
    for line in rd.events_path.read_text().splitlines():
        assert json.loads(line)


def test_token_ledger_lines_parseable(tmp_path):
    rd = _make_run_dir(tmp_path)
    rd.append_tokens(input=10, output=5, thinking=2, cached=1)
    rd.append_tokens(input=20, output=8, thinking=3, cached=4)
    for line in rd.token_ledger_path.read_text().splitlines():
        assert json.loads(line)


def test_mark_failed_handles_pathological_str_exc(tmp_path):
    """If exc.__str__ raises, mark_failed still records terminal state."""
    class BadStr(Exception):
        def __str__(self):
            raise RuntimeError("buggy __str__")

    rd = _make_run_dir(tmp_path)
    rd.mark_failed(BadStr())
    # daemon.json must reach state=failed despite the buggy __str__
    data = json.loads(rd.daemon_json_path.read_text())
    assert data["state"] == "failed"
    assert data["error"]["type"] == "BadStr"
    assert "<unrenderable" in data["error"]["message"] or data["error"]["message"]


def test_mark_failed_event_includes_elapsed_s(tmp_path):
    """daemon_error events include elapsed_s for timeline reconstruction parity
    with daemon_done/daemon_cancelled/daemon_timeout."""
    rd = _make_run_dir(tmp_path)
    rd.mark_failed(ValueError("boom"))
    last = json.loads(rd.events_path.read_text().splitlines()[-1])
    assert last["event"] == "daemon_error"
    assert "elapsed_s" in last


def test_log_callback_invoked_on_oserror(tmp_path, monkeypatch):
    """When _safe swallows an OSError, the optional log_callback is invoked."""
    captured = []
    rd = _make_run_dir(tmp_path, log_callback=lambda event, **fields: captured.append((event, fields)))

    # Force the atomic-write step to raise so _safe catches an OSError
    def failing_replace(src, dst):
        raise OSError("simulated disk full")
    monkeypatch.setattr("os.replace", failing_replace)

    rd.set_current_tool("read", {})

    assert len(captured) >= 1
    event, fields = captured[0]
    assert event == "daemon_fs_error"
    assert fields["op"] == "set_current_tool"
    assert fields["em_id"] == rd.handle
    assert fields["run_id"] == rd.run_id
    assert "error" in fields


def test_log_callback_optional_no_op(tmp_path, monkeypatch):
    """When log_callback is None, _safe stays silent on OSError (prior behavior)."""
    rd = _make_run_dir(tmp_path)  # no log_callback

    def failing_replace(src, dst):
        raise OSError("simulated")
    monkeypatch.setattr("os.replace", failing_replace)

    # Should not raise even with no callback
    rd.set_current_tool("read", {})


def test_log_callback_failures_swallowed(tmp_path, monkeypatch):
    """If the log_callback itself raises, _safe still returns cleanly."""
    def bad_callback(event, **fields):
        raise RuntimeError("callback exploded")

    rd = _make_run_dir(tmp_path, log_callback=bad_callback)

    def failing_replace(src, dst):
        raise OSError("simulated")
    monkeypatch.setattr("os.replace", failing_replace)

    # Should not raise — secondary failure (callback exception) is silent
    rd.set_current_tool("read", {})
